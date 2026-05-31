"""Aggregation: rank, day-over-day delta, source weighting, dispersion.

Pulls the run's X posts and EOD PDF from the store, resolves tickers, scores
sentiment per (ticker, bucket) in one batched LLM call, then computes the
ranked signal table and persists it to ``ticker_daily``.

Buckets are kept separate:
    'x'   — X List narrative (one mention per post)
    'pdf' — EOD recap (one mention per line referencing the ticker)

weighted_score = source_weight * mention_count * net_sentiment * (1 - dispersion)
    - source_weight from config.SOURCE_WEIGHTS (PDF weighted higher than X)
    - dispersion discounts the score when texts disagree about direction
"""

from collections import OrderedDict
from typing import Any

import config
from process import extract, sentiment
from store import db

X_BUCKET = "x"
PDF_BUCKET = "pdf"
_BUCKET_ORDER = {X_BUCKET: 0, PDF_BUCKET: 1}


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


def _collect_groups(
    run_date: str, run_id: int | None, db_path: str, pdf_date: str
) -> dict[tuple[str, str], dict[str, Any]]:
    """Build per-(ticker, bucket) mention groups from posts and the PDF."""
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

    pdf_doc = db.get_pdf_doc_by_date(pdf_date, db_path)
    if pdf_doc:
        for line in (pdf_doc.get("raw_text", "") or "").splitlines():
            line = line.strip()
            if not line:
                continue
            for ticker in extract.tickers(line):
                _accumulate(groups, ticker, PDF_BUCKET, line)

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
        pdf_date: The recap date to pull for the PDF bucket. Defaults to
            ``run_date`` (post-close); pre-open passes the prior trading day so
            it reuses last night's recap.

    Returns:
        Ranked rows (one per ticker+bucket), each with: date, ticker, bucket,
        mention_count, mention_delta, net_sentiment, dispersion, confidence,
        and weighted_score. Ranked by each ticker's combined |weighted_score|.
    """
    path = db_path or db.DEFAULT_DB_PATH
    groups = _collect_groups(run_date, run_id, path, pdf_date or run_date)
    if not groups:
        return []

    items = [
        sentiment.SentimentItem(
            ticker=g["ticker"], source=g["bucket"], texts=g["texts"]
        )
        for g in groups.values()
    ]
    scores = sentiment.score_batch(items)

    prior = db.prior_mention_counts(run_date, path)
    weights = config.SOURCE_WEIGHTS

    rows: list[dict[str, Any]] = []
    for group, sc in zip(groups.values(), scores):
        ticker, bucket = group["ticker"], group["bucket"]
        mention_count = group["mention_count"]
        net_sentiment = sc["score"]
        dispersion = sc["dispersion"]
        weight = weights.get(bucket, 1.0)
        weighted_score = weight * mention_count * net_sentiment * (1.0 - dispersion)

        row = {
            "date": run_date,
            "ticker": ticker,
            "bucket": bucket,
            "mention_count": mention_count,
            "mention_delta": mention_count - prior.get((ticker, bucket), 0),
            "net_sentiment": net_sentiment,
            "dispersion": dispersion,
            "confidence": sc["confidence"],
            "weighted_score": weighted_score,
        }
        db.upsert_ticker_daily(row, path)
        rows.append(row)

    return _rank(rows)


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
