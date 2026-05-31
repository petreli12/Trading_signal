"""Ticker extraction: $CASHTAG regex + lightweight NER -> resolved symbols.

Two complementary passes:
  1. ``$CASHTAG`` regex — high-precision, explicit ticker mentions ($NVDA).
  2. Company-name resolution — map known company/index names ("Nvidia",
     "the Nasdaq") to symbols via an alias table.

spaCy NER is used as an optional refinement when installed: it restricts
name-based matches to spans tagged as organizations, cutting false positives.
If spaCy or its model is unavailable, the alias pass still runs on raw text, so
extraction works offline with no model download.
"""

import re
from functools import lru_cache

# Cashtags: 1-5 letters with an optional class suffix (e.g. $BRK.B), no digits.
_CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})(?:\.([A-Za-z]))?\b")

# Common cashtag false positives (short English words people prefix with $).
_CASHTAG_STOPWORDS = {"A", "I", "ALL", "FOR", "ON", "IT", "BE", "OR", "SO"}

# Minimal company/index alias table -> canonical symbol. Extend as needed;
# kept small and explicit since name->ticker needs a curated lookup.
COMPANY_ALIASES: dict[str, str] = {
    "apple": "AAPL",
    "microsoft": "MSFT",
    "nvidia": "NVDA",
    "amazon": "AMZN",
    "alphabet": "GOOGL",
    "google": "GOOGL",
    "meta": "META",
    "facebook": "META",
    "tesla": "TSLA",
    "broadcom": "AVGO",
    "arm": "ARM",
    "ibm": "IBM",
    "netflix": "NFLX",
    "amd": "AMD",
    "intel": "INTC",
    "palantir": "PLTR",
    "the nasdaq": "QQQ",
    "nasdaq": "QQQ",
    "the s&p": "SPY",
    "s&p 500": "SPY",
    "sp500": "SPY",
}

# Precompiled, longest-alias-first so multi-word names win over substrings.
_ALIAS_PATTERNS = [
    (re.compile(rf"(?<![\w$]){re.escape(alias)}(?![\w])", re.IGNORECASE), symbol)
    for alias, symbol in sorted(COMPANY_ALIASES.items(), key=lambda kv: -len(kv[0]))
]


def extract_cashtags(text: str) -> list[str]:
    """Return uppercase ticker symbols from ``$CASHTAG`` mentions, in order."""
    out: list[str] = []
    for base, suffix in _CASHTAG_RE.findall(text or ""):
        symbol = base.upper()
        if symbol in _CASHTAG_STOPWORDS:
            continue
        if suffix:
            symbol = f"{symbol}.{suffix.upper()}"
        out.append(symbol)
    return out


@lru_cache(maxsize=1)
def _nlp():
    """Lazily load spaCy's small English model, or return ``None`` if absent."""
    try:
        import spacy

        return spacy.load("en_core_web_sm")
    except Exception:
        return None


def extract_named(text: str) -> list[str]:
    """Resolve known company/index names to symbols via the alias table.

    When spaCy is available, only matches that fall inside an ORG entity span
    are kept (fewer false positives); otherwise the alias patterns run on the
    full text.
    """
    if not text:
        return []

    org_spans: list[tuple[int, int]] | None = None
    nlp = _nlp()
    if nlp is not None:
        doc = nlp(text)
        org_spans = [
            (ent.start_char, ent.end_char)
            for ent in doc.ents
            if ent.label_ in {"ORG", "PRODUCT"}
        ]

    hits: list[tuple[int, str]] = []
    for pattern, symbol in _ALIAS_PATTERNS:
        for match in pattern.finditer(text):
            if org_spans is not None and not _within_any(match.span(), org_spans):
                continue
            hits.append((match.start(), symbol))
    return [symbol for _, symbol in sorted(hits, key=lambda h: h[0])]


def _within_any(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(s <= start and end <= e for s, e in spans)


def tickers(text: str) -> list[str]:
    """Extract resolved ticker symbols from text (cashtags + named entities).

    Args:
        text: Raw post or document text.

    Returns:
        A de-duplicated list of uppercase ticker symbols, preserving the order
        of first appearance.
    """
    seen: dict[str, None] = {}
    for symbol in extract_cashtags(text) + extract_named(text):
        seen.setdefault(symbol, None)
    return list(seen)
