"""Per-(ticker, source) sentiment scoring via the Anthropic LLM.

The model name comes from the LLM_MODEL env var and the key from
ANTHROPIC_API_KEY — never hardcode a model string.

Scoring is batched: all (ticker, source) groups for a run go out in a single
request that returns strict JSON, which keeps token cost down. ``score`` is a
thin convenience wrapper over ``score_batch`` for a single group.
"""

import json
import os
import re
from functools import lru_cache
from typing import Any, TypedDict

# Keep prompts bounded so cost stays predictable on noisy inputs.
MAX_TEXTS_PER_GROUP = 20
MAX_CHARS_PER_TEXT = 400
MAX_OUTPUT_TOKENS = 4096
TEMPERATURE = 0.0
# Cap groups per request so the JSON response never overflows MAX_OUTPUT_TOKENS.
CHUNK_SIZE = 40

_SYSTEM_PROMPT = (
    "You are a financial sentiment analyst. For each item you are given a "
    "ticker symbol and a set of texts (social posts or a market recap) that "
    "mention it. Judge the net directional sentiment toward that ticker "
    "expressed in the texts.\n\n"
    "Return STRICT JSON only — a single array, no prose, no code fences. Each "
    "element must be an object: "
    '{"id": <int>, "score": <float -1.0..1.0>, '
    '"confidence": <float 0.0..1.0>, "dispersion": <float 0.0..1.0>}.\n'
    "score: -1.0 = strongly bearish, 0.0 = neutral/mixed, +1.0 = strongly "
    "bullish (net across all texts for that ticker). "
    "confidence: how clearly the texts express a directional view "
    "(0.0 = no signal, 1.0 = unambiguous). "
    "dispersion: how much the texts DISAGREE with each other about direction "
    "(0.0 = unanimous, 1.0 = sharply split bullish vs bearish). "
    "Output one object per input id, in any order, and nothing else."
)


class SentimentScore(TypedDict):
    ticker: str
    source: str
    score: float
    confidence: float
    dispersion: float


class SentimentItem(TypedDict):
    ticker: str
    source: str
    texts: list[str]


def _model() -> str:
    model = (os.environ.get("LLM_MODEL") or "").strip()
    if not model:
        raise RuntimeError("LLM_MODEL is not set. Add it to your .env / environment.")
    return model


@lru_cache(maxsize=1)
def _client():
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to your .env / environment."
        )
    import anthropic

    return anthropic.Anthropic(api_key=api_key)


def _build_user_message(items: list[SentimentItem]) -> str:
    blocks: list[str] = []
    for idx, item in enumerate(items):
        texts = [t.strip().replace("\n", " ") for t in item["texts"] if t.strip()]
        texts = [t[:MAX_CHARS_PER_TEXT] for t in texts[:MAX_TEXTS_PER_GROUP]]
        joined = "\n".join(f"  - {t}" for t in texts) or "  - (no text)"
        blocks.append(
            f"id: {idx}\nticker: {item['ticker']}\nsource: {item['source']}\n"
            f"texts:\n{joined}"
        )
    return (
        "Score the sentiment for each of the following items.\n\n"
        + "\n\n".join(blocks)
    )


def _parse_scores(raw: str) -> dict[int, tuple[float, float, float]]:
    """Parse the model output into {id: (score, confidence, dispersion)}.

    Parses object-by-object rather than requiring a well-formed array, so it
    tolerates Markdown code fences and a truncated trailing object.
    """
    out: dict[int, tuple[float, float, float]] = {}
    for blob in re.findall(r"\{[^{}]*\}", raw, re.DOTALL):
        try:
            obj = json.loads(blob)
            idx = int(obj["id"])
            score = max(-1.0, min(1.0, float(obj["score"])))
            confidence = max(0.0, min(1.0, float(obj["confidence"])))
            dispersion = max(0.0, min(1.0, float(obj.get("dispersion", 0.0))))
        except (KeyError, TypeError, ValueError):
            continue
        out[idx] = (score, confidence, dispersion)
    return out


def _score_chunk(items: list[SentimentItem]) -> list[SentimentScore]:
    """Score one chunk of groups in a single LLM call."""
    message = _client().messages.create(
        model=_model(),
        max_tokens=MAX_OUTPUT_TOKENS,
        temperature=TEMPERATURE,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_message(items)}],
    )
    raw = "".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    )
    parsed = _parse_scores(raw)

    results: list[SentimentScore] = []
    for idx, item in enumerate(items):
        score_val, confidence, dispersion = parsed.get(idx, (0.0, 0.0, 0.0))
        results.append(
            SentimentScore(
                ticker=item["ticker"],
                source=item["source"],
                score=score_val,
                confidence=confidence,
                dispersion=dispersion,
            )
        )
    return results


def score_batch(items: list[SentimentItem]) -> list[SentimentScore]:
    """Score sentiment for many (ticker, source) groups, chunked across calls.

    Groups are sent in chunks of ``CHUNK_SIZE`` so each JSON response stays
    within ``MAX_OUTPUT_TOKENS``. Results are returned in input order; any item
    the model omits defaults to a neutral, zero-confidence score.

    Args:
        items: One entry per (ticker, source) group, each with its texts.
    """
    if not items:
        return []
    results: list[SentimentScore] = []
    for start in range(0, len(items), CHUNK_SIZE):
        results.extend(_score_chunk(items[start : start + CHUNK_SIZE]))
    return results


def score(ticker: str, source: str, texts: list[str]) -> SentimentScore:
    """Score sentiment for a single ticker from texts in one source bucket.

    Convenience wrapper over ``score_batch``. Prefer ``score_batch`` when
    scoring many groups to minimize LLM cost.

    Args:
        ticker: The resolved ticker symbol.
        source: The source bucket (e.g. 'x' or 'pdf').
        texts: Texts mentioning the ticker.

    Returns:
        A ``SentimentScore`` with a signed score and confidence.
    """
    return score_batch(
        [SentimentItem(ticker=ticker, source=source, texts=texts)]
    )[0]
