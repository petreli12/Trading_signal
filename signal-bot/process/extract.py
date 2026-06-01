"""Ticker extraction for the X bucket: $CASHTAG only, whitelist-validated.

Design (X path):
  1. ``$CASHTAG`` regex — explicit, high-precision ticker mentions ($NVDA).
     Traders cashtag their tickers, so this is the reliable signal on X.
  2. Whitelist — the cashtag's symbol must be a real US listing (NYSE/Nasdaq).
     Valid-length fakes like ``$ZZZZ`` are dropped; drops are logged so a real
     ticker missing from the list can be spotted and the list refreshed.

We deliberately do NOT resolve bare company names ("Apple", "Nvidia") on the X
path: that path caused false positives like "I love my Apple watch" -> AAPL, and
the spaCy ORG-filter meant to gate it was never installed in prod/CI. The recall
lost is small (cashtags dominate trader posts) relative to the noise removed.

NOTE: the PDF/recap bucket does NOT use this module — it is parsed
deterministically in process/aggregate.py. This is the X path only.

The whitelist lives in ``process/us_tickers.txt`` (regenerate with
``scripts/refresh_symbols.py``; monthly cadence is plenty).
"""

import logging
import re
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger("signal_bot.extract")

# Cashtags: 1-5 letters with an optional class suffix (e.g. $BRK.B), no digits.
_CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})(?:\.([A-Za-z]))?\b")

# Common cashtag false positives (short English words people prefix with $).
_CASHTAG_STOPWORDS = {"A", "I", "ALL", "FOR", "ON", "IT", "BE", "OR", "SO"}

_WHITELIST_PATH = Path(__file__).resolve().parent / "us_tickers.txt"


@lru_cache(maxsize=1)
def _whitelist() -> frozenset[str]:
    """Load the real-US-ticker whitelist (uppercase symbols, one per line)."""
    try:
        text = _WHITELIST_PATH.read_text(encoding="utf-8")
    except OSError:
        logger.error("Ticker whitelist missing at %s; extraction will drop all "
                     "cashtags. Run scripts/refresh_symbols.py.", _WHITELIST_PATH)
        return frozenset()
    return frozenset(line.strip().upper() for line in text.splitlines() if line.strip())


def _is_listed(symbol: str) -> bool:
    """True if ``symbol`` (or its base, ignoring a class suffix) is listed."""
    whitelist = _whitelist()
    return symbol in whitelist or symbol.split(".", 1)[0] in whitelist


def extract_cashtags(text: str) -> list[str]:
    """Return uppercase ticker symbols from ``$CASHTAG`` mentions, in order.

    Raw (pre-whitelist): only the regex + short-word stopwords are applied.
    """
    out: list[str] = []
    for base, suffix in _CASHTAG_RE.findall(text or ""):
        symbol = base.upper()
        if symbol in _CASHTAG_STOPWORDS:
            continue
        if suffix:
            symbol = f"{symbol}.{suffix.upper()}"
        out.append(symbol)
    return out


def tickers(text: str) -> list[str]:
    """Extract de-duplicated, whitelist-validated ticker symbols from text.

    Only ``$CASHTAG`` mentions are considered (X path). A cashtag whose symbol
    is not a real US listing is dropped and logged, so the brief never scores a
    junk ticker but a genuinely-missing symbol stays visible in the logs.

    Returns:
        Uppercase symbols, de-duplicated, in order of first appearance.
    """
    seen: dict[str, None] = {}
    dropped: set[str] = set()
    for symbol in extract_cashtags(text):
        if _is_listed(symbol):
            seen.setdefault(symbol, None)
        else:
            dropped.add(symbol)
    if dropped:
        logger.info("Dropped non-whitelisted cashtag(s): %s", ", ".join(sorted(dropped)))
    return list(seen)
