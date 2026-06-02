"""X List ingestion via a third-party provider adapter.

Data comes from twitterapi.io over REST (``X-API-Key`` header) — NOT the
official X API. This module is the single provider seam: ``fetch`` contains all
provider-specific HTTP and response parsing, normalizing to a stable internal
shape so swapping providers later touches only this file.

Normalized post shape:
    {"id": str, "author": str, "text": str, "ts": str, "engagement": int}

Endpoint:
    GET https://api.twitterapi.io/twitter/list/tweets_timeline
        ?listId=<id>&cursor=<cursor>
    Header: X-API-Key: <key>
    Response: {"tweets": [...], "has_next_page": bool, "next_cursor": str}
"""

import os
import time
from datetime import datetime, timezone
from typing import Any, TypedDict

import requests

from store import db

BASE_URL = "https://api.twitterapi.io"
LIST_TIMELINE_PATH = "/twitter/list/tweets_timeline"
# Safety cap on pages fetched per run (~20 tweets/page). Pagination stops early
# at the since_id watermark, so on a normal run only the genuinely-new posts are
# pulled; this cap just bounds a first run / a busy session / a long gap.
DEFAULT_MAX_PAGES = 10
DEFAULT_TIMEOUT = 30
# Retry/backoff for transient provider errors (rate limits, 5xx).
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 2.0
INTER_PAGE_DELAY_SECONDS = 0.5
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# twitterapi.io createdAt looks like "Tue Dec 10 07:00:30 +0000 2024".
_TWITTER_TS_FORMAT = "%a %b %d %H:%M:%S %z %Y"


class NormalizedPost(TypedDict):
    id: str
    author: str
    text: str
    ts: str
    engagement: int


def _api_key() -> str:
    key = (os.environ.get("X_PROVIDER_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "X_PROVIDER_API_KEY is not set. Add it to your .env / environment."
        )
    return key


def _parse_ts(raw: str) -> str:
    """Convert a twitterapi.io timestamp to an ISO 8601 UTC string.

    Falls back to the raw value if parsing fails so we never drop a post.
    """
    if not raw:
        return ""
    try:
        dt = datetime.strptime(raw, _TWITTER_TS_FORMAT)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return raw


def _engagement(tweet: dict[str, Any]) -> int:
    """Sum the public engagement counters into a single metric."""
    return sum(
        int(tweet.get(key) or 0)
        for key in ("likeCount", "retweetCount", "replyCount", "quoteCount")
    )


def _normalize(tweet: dict[str, Any]) -> NormalizedPost:
    author = (tweet.get("author") or {}).get("userName", "") or ""
    return NormalizedPost(
        id=str(tweet.get("id", "")),
        author=author,
        text=tweet.get("text", "") or "",
        ts=_parse_ts(tweet.get("createdAt", "") or ""),
        engagement=_engagement(tweet),
    )


def _request_page(
    headers: dict[str, str], params: dict[str, str], timeout: int
) -> dict[str, Any]:
    """GET one page with retry/backoff on rate limits and transient 5xx errors."""
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        resp = requests.get(
            f"{BASE_URL}{LIST_TIMELINE_PATH}",
            headers=headers,
            params=params,
            timeout=timeout,
        )
        if resp.status_code in _RETRYABLE_STATUS:
            retry_after = resp.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else BACKOFF_BASE_SECONDS * (2**attempt)
            except ValueError:
                delay = BACKOFF_BASE_SECONDS * (2**attempt)
            last_exc = requests.HTTPError(f"{resp.status_code} from provider")
            if attempt < MAX_RETRIES - 1:
                time.sleep(delay)
                continue
        resp.raise_for_status()
        return resp.json()
    raise last_exc or RuntimeError("X provider request failed")


def fetch(
    list_id: str,
    since_id: str | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[NormalizedPost]:
    """Poll the X List and return posts normalized to ``NormalizedPost``.

    Paginates via the provider cursor (newest first) and stops early once it
    reaches a tweet with id <= ``since_id`` (snowflake ids are monotonic).

    Args:
        list_id: The X List identifier to poll.
        since_id: If provided, only return posts newer than this tweet id.
        max_pages: Safety cap on pages fetched per call.
        timeout: Per-request timeout in seconds.

    Returns:
        A list of normalized posts, newest first.
    """
    headers = {"X-API-Key": _api_key()}
    since = int(since_id) if since_id and str(since_id).isdigit() else None
    collected: list[NormalizedPost] = []
    cursor = ""

    for page in range(max_pages):
        payload = _request_page(
            headers, {"listId": list_id, "cursor": cursor}, timeout
        )

        reached_seen = False
        for tweet in payload.get("tweets") or []:
            tid = str(tweet.get("id", ""))
            if since is not None and tid.isdigit() and int(tid) <= since:
                reached_seen = True
                break
            collected.append(_normalize(tweet))

        if reached_seen or not payload.get("has_next_page"):
            break
        cursor = payload.get("next_cursor") or ""
        if not cursor:
            break
        if page < max_pages - 1:
            time.sleep(INTER_PAGE_DELAY_SECONDS)

    return collected


def fetch_new(
    list_id: str,
    run_id: int | None = None,
    db_path: str | None = None,
) -> list[NormalizedPost]:
    """Fetch posts newer than what's stored, persist them, and return them.

    Uses the max stored tweet id as a ``since_id`` watermark and inserts with
    ``INSERT OR IGNORE`` so re-runs never create duplicates.

    Args:
        list_id: The X List identifier to poll.
        run_id: The current run id, recorded on inserted posts.
        db_path: Optional override for the database path (defaults to db config).

    Returns:
        The list of newly stored posts (already-seen posts are filtered out).
    """
    path = db_path or db.DEFAULT_DB_PATH
    since_id = db.latest_post_id(path)
    posts = fetch(list_id, since_id=since_id)

    seen = db.existing_post_ids((p["id"] for p in posts), path)
    new_posts = [p for p in posts if p["id"] not in seen]

    db.insert_posts(new_posts, run_id=run_id, db_path=path)
    return new_posts
