"""Entrypoint: python main.py --mode preopen|postclose

Orchestrates one run of the pipeline:
    trading-day check -> X ingest -> PDF ingest -> aggregate -> summarize -> email

Skips non-trading days (weekends / NYSE holidays) cleanly. Pre-open reuses the
prior trading day's recap PDF; post-close ingests today's recap.
"""

import argparse
import logging
import traceback
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

import config
from deliver import email_send
from ingest import pdf_email, x_list
from process import aggregate, extract
from store import db
from summarize import summarize

MARKET_TZ = ZoneInfo("America/New_York")
_NYSE = mcal.get_calendar("XNYS")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("signal_bot")

_DATA_BANNER = (
    "> ⚠️ **No data ingested this run — check provider credits and credentials.**\n\n"
)


def _is_trading_day(day: date) -> bool:
    valid = _NYSE.valid_days(start_date=day.isoformat(), end_date=day.isoformat())
    return len(valid) > 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trading-signal summary bot")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["preopen", "postclose"],
        help="Which run variant to execute.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even if today is not an NYSE trading day (testing).",
    )
    return parser.parse_args()


def run(mode: str, force: bool = False) -> None:
    """Top-level run wrapper: orchestrate, then alert + record on any failure.

    Any unhandled exception from the orchestration triggers a failure-alert
    email (best-effort), is written to the runs table with a traceback, and is
    re-raised so the GitHub Actions job goes red (the backstop).
    """
    run_id: int | None = None
    run_date: date | None = None
    try:
        db.init_db()
        run_date = datetime.now(MARKET_TZ).date()
        if not force and not _is_trading_day(run_date):
            logger.info("%s is not an NYSE trading day — no-op (%s).", run_date, mode)
            return
        run_id = db.start_run(mode)
        _orchestrate(mode, run_date, run_id)
    except Exception as exc:
        tb = traceback.format_exc()
        # ALWAYS record the failure to the runs table (if a run row exists).
        if run_id is not None:
            try:
                db.finish_run(run_id, "error", _error_notes(exc, tb))
            except Exception:
                logger.exception("Failed to record run error to the runs table.")
        # Best-effort alert; never let the alerter mask the original exception.
        if config.ALERT_ON_FAILURE:
            _send_failure_alert(mode, run_date, run_id, exc, tb)
        raise


def _orchestrate(mode: str, run_date: date, run_id: int) -> None:
    """Run the normal pipeline: ingest -> aggregate -> summarize -> deliver."""
    warnings: list[str] = []

    # X ingestion — isolated so a provider outage doesn't kill the run.
    new_posts: list = []
    try:
        new_posts = x_list.fetch_new(config.X_LIST_ID, run_id=run_id)
    except Exception as exc:
        warnings.append(f"X ingestion failed: {exc!r}")

    # PDF ingestion + recap-date selection.
    #   post-close: today's recap (run_date == recap_date). Posted at/after EOD,
    #               so a cron firing early can legitimately find it absent.
    #   pre-open:   today's recap does not exist yet — use the MOST RECENT
    #               available recap (the prior trading day's).
    if mode == "postclose":
        try:
            doc = pdf_email.fetch()
            if doc:
                db.insert_pdf_doc(doc)
        except Exception as exc:
            warnings.append(f"PDF ingestion failed: {exc!r}")
        recap_doc = db.get_pdf_doc_by_date(run_date.isoformat())
    else:  # preopen
        recap_doc = db.get_latest_pdf_doc(on_or_before=run_date.isoformat())

    pdf_date = recap_doc["doc_date"] if recap_doc else None
    pdf_present = recap_doc is not None

    # Classify the no-recap state so the brief words an EXPECTED absence calmly
    # and only alarms on a NOTABLE one (reuses the NYSE gate + run mode + an ET
    # cutoff; no new calendar). A post-close recap that hasn't posted yet (before
    # the cutoff) is 'pending', not an alarm — recaps sometimes arrive late.
    recap_status = _recap_status(
        mode, run_date, pdf_present, datetime.now(MARKET_TZ)
    )

    rows = aggregate.build(run_date.isoformat(), run_id=run_id, pdf_date=pdf_date)
    unknown_cashtags = _tally_unknown_cashtags(new_posts)
    brief = summarize.summarize(
        mode,
        rows,
        unknown_cashtags=unknown_cashtags,
        recap_status=recap_status,
        posts=new_posts,
    )
    brief = _append_warnings(brief, warnings)

    # A genuinely-missing recap on a trading day is a degraded run; expected
    # absences (weekend / not-yet-posted) are not.
    status = "partial" if warnings or recap_status == "missing" else "ok"

    # A run that ingested nothing usable from either bucket is a red flag
    # (e.g. provider returned empty on depleted credits). Keyed off ACTUAL
    # emptiness (no X posts AND no usable recap rows), NOT status — a missing
    # PDF flips status to "partial" and must not hide this banner.
    pdf_rows_present = any(r["bucket"] == aggregate.PDF_BUCKET for r in rows)
    if not new_posts and not pdf_rows_present:
        logger.warning(
            "No data ingested this run (0 posts, no usable recap rows) — "
            "check provider credits and credentials."
        )
        brief = _DATA_BANNER + brief

    subject = f"[signal-bot] {mode} brief — {run_date.isoformat()}"
    email_send.send(subject, brief)

    # Optional SMS digest (best-effort; never fail the run if the gateway is down).
    if email_send.sms_enabled():
        try:
            email_send.send_sms(summarize.sms_digest(mode, rows))
            logger.info("SMS digest sent.")
        except Exception as exc:
            logger.warning("SMS digest failed (suppressed): %r", exc)

    notes = (
        f"{len(new_posts)} new posts, {len(rows)} ticker rows, "
        f"pdf_date={pdf_date}, recap={recap_status}"
        + (f"; warnings: {' | '.join(warnings)}" if warnings else "")
    )
    db.finish_run(run_id, status, notes)
    logger.info("%s run complete (%s) — %s", mode, status, notes)


def _recap_status(
    mode: str, run_date: date, pdf_present: bool, now: datetime
) -> str:
    """Classify the EOD-recap availability for brief wording.

    Args:
        now: Current time in MARKET_TZ (ET) — used to tell a not-posted-yet
            recap from a genuinely overdue one on a post-close run.

    Returns one of:
        'present'      — a recap was found/used.
        'not_expected' — non-trading day (weekend/holiday or --force off-session);
                         no recap exists or is expected.
        'pending'      — trading day, recap not available yet but not overdue
                         (pre-open, or post-close before RECAP_EXPECTED_BY_ET);
                         normal timing, recaps sometimes post late.
        'missing'      — post-close on a trading day, recap still absent at/after
                         RECAP_EXPECTED_BY_ET; overdue, so flag a possible
                         ingestion failure.
    """
    if pdf_present:
        return "present"
    if not _is_trading_day(run_date):
        return "not_expected"
    if mode == "preopen":
        return "pending"
    # post-close on a trading day: pending until the recap is overdue.
    if now.time() < config.RECAP_EXPECTED_BY_ET:
        return "pending"
    return "missing"


def _tally_unknown_cashtags(posts: list) -> dict[str, int]:
    """Count regex-valid but non-whitelisted cashtags across the run's posts.

    One mention per post (deduped within a post), mirroring X mention counts.
    These are informational only — never scored, ranked, or weighted.
    """
    counts: dict[str, int] = {}
    for post in posts:
        text = post.get("text", "") or ""
        for sym in set(extract.unknown_cashtags(text)):
            counts[sym] = counts.get(sym, 0) + 1
    return counts


def _append_warnings(brief: str, warnings: list[str]) -> str:
    """Surface source failures in the brief so silent breaks don't go unnoticed."""
    if not warnings:
        return brief
    lines = "\n".join(f"- {w}" for w in warnings)
    return f"{brief}\n\n---\n\n## ⚠️ Data source warnings\n{lines}"


def _error_notes(exc: Exception, tb: str) -> str:
    """Compact error record for the runs table: type, message, traceback tail."""
    return f"{type(exc).__name__}: {exc}\n{tb}"[:4000]


def _send_failure_alert(
    mode: str, run_date: date | None, run_id: int | None, exc: Exception, tb: str
) -> None:
    """Best-effort hard-failure alert email. Swallows its own errors."""
    try:
        date_str = run_date.isoformat() if run_date else "unknown-date"
        subject = f"⚠️ signal-bot FAILED — {mode} {date_str}"
        body = (
            "signal-bot run FAILED.\n\n"
            f"Mode:   {mode}\n"
            f"Date:   {date_str}\n"
            f"Run ID: {run_id if run_id is not None else 'n/a'}\n"
            f"Error:  {type(exc).__name__}: {exc}\n\n"
            "Traceback (tail):\n"
            f"{tb[-2000:]}"
        )
        email_send.send(subject, body, to=config.ALERT_EMAIL_TO or None)
    except Exception:
        logger.exception("Failure-alert email could not be sent (suppressed).")


def main() -> None:
    args = parse_args()
    run(args.mode, force=args.force)


if __name__ == "__main__":
    main()
