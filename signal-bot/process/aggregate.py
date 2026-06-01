"""Aggregation: rank, day-over-day delta, source weighting, dispersion.

Two buckets, scored very differently and kept strictly separate:
    'x'   — X List narrative. Mentions are noisy free text, so an LLM scores
            net sentiment per ticker across all the posts mentioning it.
    'pdf' — EOD recap. The recap states its OWN call in a structured
            "Current Sentiment:" field per ticker section. That label is the
            anchor score (parsed deterministically — no LLM inference). The
            free-text Notes are used only for nuance and to DETECT divergence
            between the label and the prose; they never move the score.

weighted_score = source_weight * mention_count * net_sentiment * (1 - dispersion)
    - source_weight from config.SOURCE_WEIGHTS (PDF weighted higher than X)
    - dispersion discounts the score when signal is mixed (for PDF: a
      label-vs-prose divergence)
"""

import re
from collections import OrderedDict
from typing import Any

import config
from process import extract, sentiment
from store import db

X_BUCKET = "x"
PDF_BUCKET = "pdf"
_BUCKET_ORDER = {X_BUCKET: 0, PDF_BUCKET: 1}

# --- Recap "Current Sentiment" label -> anchor score ---------------------
# Magnitudes: outright calls (BULLISH/BEARISH) at ±0.6 leave headroom above the
# noisier X scores; the "neutral leaning" hedges sit at ±0.25. Sign + ordering
# strictly follow the label: BEARISH < NEUTRAL-LEANING-BEARISH < NEUTRAL <
# NEUTRAL-LEANING-BULLISH < BULLISH.
_LABEL_SCORES = {
    "BULLISH": 0.6,
    "BEARISH": -0.6,
    "NEUTRAL": 0.0,
    "NEUTRAL LEANING BULLISH": 0.25,
    "NEUTRAL LEANING BEARISH": -0.25,
}
# A parsed label is the source's explicit statement, so confidence is high.
_LABEL_CONFIDENCE = 0.8
# Dispersion applied when the prose lean contradicts the label's sign.
_DIVERGENCE_DISPERSION = 0.35

_SENTIMENT_RE = re.compile(r"^Current Sentiment:\s*(.+)$", re.IGNORECASE)
# A SECTION label is a cashtag alone on its line ("$AAPL"); a cashtag embedded
# in prose ("a 5k push in $BTC") is NOT a section and must not be scored.
_STANDALONE_LABEL_RE = re.compile(r"^\$([A-Z]{1,5}(?:\.[A-Z])?)$")
_STAT_PREFIXES = (
    "Closed:", "High:", "Low:", "Current Sentiment:", "Key Level:",
    "Levels Above:", "Levels Below:", "Notes", "© Copyright",
    "Daily Market Analysis",
)
# Rough lexical lean used ONLY to flag label/prose divergence (never to score).
_BULL_MARKERS = (
    "calls can work", "can work", "run to", "push to", "higher low",
    "break above", "breakout", "in play", "new high", "next leg", "can run",
    "move to", "wants to break", "continuation", "ran from", "held above",
    "broke and held", "leg higher",
)
_BEAR_MARKERS = (
    "avoid", "puts", "short", "fails", "top can start", "top forms",
    "pullback", "rejected", "back under", "harder trade", "hard trade",
    "breakdown", "stay away", "weak",
)


def _map_label(label: str) -> tuple[str, float]:
    """Normalize a Current Sentiment label to (canonical label, anchor score)."""
    key = " ".join(label.strip().upper().split())
    if key in _LABEL_SCORES:
        return key, _LABEL_SCORES[key]
    if "LEANING BULLISH" in key:
        return "NEUTRAL LEANING BULLISH", _LABEL_SCORES["NEUTRAL LEANING BULLISH"]
    if "LEANING BEARISH" in key:
        return "NEUTRAL LEANING BEARISH", _LABEL_SCORES["NEUTRAL LEANING BEARISH"]
    if "BULLISH" in key:
        return "BULLISH", _LABEL_SCORES["BULLISH"]
    if "BEARISH" in key:
        return "BEARISH", _LABEL_SCORES["BEARISH"]
    return "NEUTRAL", 0.0


def _recap_pages(raw_text: str) -> list[list[str]]:
    """Split the recap into pages on the repeating copyright header."""
    pages: list[list[str]] = []
    current: list[str] = []
    for line in (raw_text or "").splitlines():
        if line.startswith("© Copyright") and current:
            pages.append(current)
            current = []
        current.append(line)
    if current:
        pages.append(current)
    return pages


def _is_commentary(line: str) -> bool:
    """True for free-text Notes prose (not stat fields, labels, or headers)."""
    s = line.strip()
    if not s or s.startswith(_STAT_PREFIXES) or _STANDALONE_LABEL_RE.match(s):
        return False
    return any(c.islower() for c in s)


def _mentions(line: str, ticker: str) -> bool:
    """True if a line refers to ``ticker`` by cashtag or bare symbol."""
    return re.search(rf"(?<![A-Za-z0-9.]){re.escape(ticker)}\b", line) is not None


def _prose_lean(prose: str) -> int:
    """Rough directional lean of prose: +1 bullish, -1 bearish, 0 neutral."""
    text = prose.lower()
    bull = sum(text.count(w) for w in _BULL_MARKERS)
    bear = sum(text.count(w) for w in _BEAR_MARKERS)
    if bull > bear:
        return 1
    if bear > bull:
        return -1
    return 0


def _parse_recap_sections(raw_text: str) -> list[dict[str, Any]]:
    """Parse the recap into per-ticker sections anchored on Current Sentiment.

    Within each page, the stat-blocks (each with a "Current Sentiment:" line)
    and the standalone "$TICKER" label lines appear in the SAME order, so we
    zip them positionally. Tickers that appear only in prose (e.g. $BTC inside
    a sentence, or "S&P 500") have no section and are intentionally not scored.

    Each section carries the label-anchored score, a high confidence, the
    attached prose, and a divergence flag set when the prose lean contradicts
    the label's sign (surfaced, not used to change the score).
    """
    commentary = [ln for ln in (raw_text or "").splitlines() if _is_commentary(ln)]
    sections: list[dict[str, Any]] = []

    for page in _recap_pages(raw_text):
        labels = [m.group(1) for ln in page if (m := _SENTIMENT_RE.match(ln.strip()))]
        tickers = [
            m.group(1) for ln in page if (m := _STANDALONE_LABEL_RE.match(ln.strip()))
        ]
        for ticker, raw_label in zip(tickers, labels):
            canon_label, net = _map_label(raw_label)
            prose_lines = [ln.strip() for ln in commentary if _mentions(ln, ticker)]
            prose = " ".join(prose_lines)
            lean = _prose_lean(prose)
            label_sign = (net > 0) - (net < 0)
            divergence = lean != 0 and lean != label_sign
            note = ""
            if divergence:
                note = (
                    f"recap label {canon_label.title()}, "
                    f"notes lean {'bullish' if lean > 0 else 'cautious/bearish'}"
                )
            sections.append(
                {
                    "ticker": ticker,
                    "label": canon_label,
                    "net_sentiment": net,
                    "confidence": _LABEL_CONFIDENCE,
                    "dispersion": _DIVERGENCE_DISPERSION if divergence else 0.0,
                    "mention_count": 1 + len(prose_lines),
                    "stance_divergence": divergence,
                    "divergence_note": note,
                    "prose": prose,
                }
            )
    return sections


def _accumulate(
    groups: dict[tuple[str, str], dict[str, Any]],
    ticker: str,
    bucket: str,
    text: str,
) -> None:
    key = (ticker, bucket)
    group = groups.setdefault(
        key, {"ticker": ticker, "bucket": bucket, "mention_count": 0, "texts": []}
    )
    group["mention_count"] += 1
    group["texts"].append(text)


def _collect_x_groups(
    run_date: str, run_id: int | None, db_path: str
) -> dict[tuple[str, str], dict[str, Any]]:
    """Build per-ticker X-bucket mention groups from the run's posts."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    posts = (
        db.get_posts_by_run(run_id, db_path)
        if run_id is not None
        else db.get_posts_by_date(run_date, db_path)
    )
    for post in posts:
        text = post.get("text", "") or ""
        for ticker in extract.tickers(text):
            _accumulate(groups, ticker, X_BUCKET, text)
    return groups


def build(
    run_date: str,
    run_id: int | None = None,
    db_path: str | None = None,
    pdf_date: str | None = None,
) -> list[dict[str, Any]]:
    """Build and persist the ranked aggregate table for a run.

    Args:
        run_date: The trading date of the run (YYYY-MM-DD). Used for the
            ticker_daily key and to look up the prior trading day for deltas.
        run_id: If given, aggregate the posts fetched by this run; otherwise
            aggregate all posts timestamped on ``run_date``.
        db_path: Optional database path override.
        pdf_date: The recap coverage date to pull for the PDF bucket. If
            ``None`` or no recap exists for that date, the PDF bucket is simply
            empty (no neutral filler rows) — a legitimate "recap not yet
            available" state. Post-close passes ``run_date``; pre-open passes
            the most-recent-available recap's date (resolved by the caller).

    Returns:
        Ranked rows (one per ticker+bucket), each with: date, ticker, bucket,
        mention_count, mention_delta, net_sentiment, dispersion, confidence,
        weighted_score, and (PDF rows) stance_divergence + divergence_note.
        Ranked by each ticker's combined |weighted_score|.
    """
    path = db_path or db.DEFAULT_DB_PATH
    pdf_doc = db.get_pdf_doc_by_date(pdf_date, path) if pdf_date else None

    # X bucket: noisy narrative -> LLM net sentiment per ticker.
    x_groups = _collect_x_groups(run_date, run_id, path)
    # PDF bucket: structured recap -> deterministic label-anchored score.
    pdf_sections = _parse_recap_sections(pdf_doc.get("raw_text", "")) if pdf_doc else []

    if not x_groups and not pdf_sections:
        return []

    items = [
        sentiment.SentimentItem(
            ticker=g["ticker"], source=g["bucket"], texts=g["texts"]
        )
        for g in x_groups.values()
    ]
    scores = sentiment.score_batch(items)

    prior = db.prior_mention_counts(run_date, path)
    weights = config.SOURCE_WEIGHTS

    rows: list[dict[str, Any]] = []

    for group, sc in zip(x_groups.values(), scores):
        ticker = group["ticker"]
        mention_count = group["mention_count"]
        net_sentiment = sc["score"]
        dispersion = sc["dispersion"]
        weight = weights.get(X_BUCKET, 1.0)
        rows.append(
            _persist_row(
                {
                    "date": run_date,
                    "ticker": ticker,
                    "bucket": X_BUCKET,
                    "mention_count": mention_count,
                    "mention_delta": mention_count - prior.get((ticker, X_BUCKET), 0),
                    "net_sentiment": net_sentiment,
                    "dispersion": dispersion,
                    "confidence": sc["confidence"],
                    "weighted_score": weight
                    * mention_count
                    * net_sentiment
                    * (1.0 - dispersion),
                },
                path,
            )
        )

    weight = weights.get(PDF_BUCKET, 1.0)
    for sec in pdf_sections:
        ticker = sec["ticker"]
        mention_count = sec["mention_count"]
        net_sentiment = sec["net_sentiment"]
        dispersion = sec["dispersion"]
        row = _persist_row(
            {
                "date": run_date,
                "ticker": ticker,
                "bucket": PDF_BUCKET,
                "mention_count": mention_count,
                "mention_delta": mention_count - prior.get((ticker, PDF_BUCKET), 0),
                "net_sentiment": net_sentiment,
                "dispersion": dispersion,
                "confidence": sec["confidence"],
                "weighted_score": weight
                * mention_count
                * net_sentiment
                * (1.0 - dispersion),
            },
            path,
        )
        # Runtime-only fields (not ticker_daily columns) for the brief.
        row["stance_divergence"] = sec["stance_divergence"]
        row["divergence_note"] = sec["divergence_note"]
        rows.append(row)

    return _rank(rows)


def _persist_row(row: dict[str, Any], db_path: str) -> dict[str, Any]:
    """Upsert a ticker_daily row and return it (for the ranked result)."""
    db.upsert_ticker_daily(row, db_path)
    return row


def _rank(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank by each ticker's combined |weighted_score|, keeping buckets together."""
    combined: dict[str, float] = OrderedDict()
    for row in rows:
        combined[row["ticker"]] = combined.get(row["ticker"], 0.0) + row["weighted_score"]

    ticker_rank = {
        ticker: idx
        for idx, (ticker, _) in enumerate(
            sorted(combined.items(), key=lambda kv: abs(kv[1]), reverse=True)
        )
    }
    return sorted(
        rows,
        key=lambda r: (ticker_rank[r["ticker"]], _BUCKET_ORDER.get(r["bucket"], 9)),
    )
