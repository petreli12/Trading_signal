"""LLM summary with preopen vs postclose prompt variants.

Consumes the ranked aggregate table from ``process.aggregate.build`` and
produces a concise brief. Honors the signal-quality rules from the roadmap:

  - Lead with day-over-day mention deltas (the real signal).
  - Report BOTH net sentiment and dispersion (consensus vs. split).
  - Keep X (narrative/attention) separate from the PDF recap (actionable);
    never blur the two.
  - Flag a mention spike not backed by a credible source (the PDF recap) as a
    potential low-float / pump risk.
  - This is decision-support: the brief flags, the reader decides.

The model name comes from the LLM_MODEL env var and the key from
ANTHROPIC_API_KEY — never hardcode a model string.
"""

import json
import os
from functools import lru_cache
from typing import Any

import config

PDF_BUCKET = "pdf"
X_BUCKET = "x"
MAX_OUTPUT_TOKENS = 1500
# An X mention jump this large with no PDF-recap corroboration is a pump flag.
PUMP_MIN_DELTA = 5

_MODE_GUIDANCE = {
    "preopen": (
        "This is the PRE-OPEN brief (~08:00 ET). Focus on overnight "
        "developments and setups to watch for today's session. Frame tickers "
        "as things to watch into the open, not post-mortems."
    ),
    "postclose": (
        "This is the POST-CLOSE brief (~17:00 ET). Focus on what moved today "
        "and what is building for tomorrow. Separate today's action from "
        "narratives gaining traction for the next session."
    ),
}

_SYSTEM_PROMPT = (
    "You are a buy-side analyst writing a tight daily trading brief for one "
    "experienced trader. Be concise, concrete, and skeptical.\n\n"
    "Rules:\n"
    "- Lead with day-over-day mention deltas — a ticker spiking from few to "
    "many mentions is the signal, not one that is always loud.\n"
    "- Always report both net sentiment AND dispersion. 'Everyone bullish' and "
    "'sharply divided' are different trades.\n"
    "- Keep X chatter (narrative / attention) strictly separate from the EOD "
    "recap PDF (actionable, credible source). Never merge them into one claim.\n"
    "- If a ticker is flagged pump_risk, explicitly call it out as a possible "
    "low-float / pump: mention spike with no credible-source corroboration.\n"
    "- This is decision-support. Flag and contextualize; do not give buy/sell "
    "orders or price targets you cannot support from the data.\n"
    "- Omit tickers with no meaningful signal. Prefer a short brief over a "
    "padded one."
)


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


def _prepare(table: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group ranked rows by ticker, split buckets, and compute pump_risk.

    Preserves the incoming rank order (the table is already ranked by combined
    |weighted_score|). Limits to ``config.PROMPT['max_tickers']``.
    """
    by_ticker: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in table:
        ticker = row["ticker"]
        if ticker not in by_ticker:
            by_ticker[ticker] = {"ticker": ticker, "x": None, "pdf": None}
            order.append(ticker)
        bucket = row["bucket"]
        by_ticker[ticker][bucket] = {
            "mentions": row["mention_count"],
            "mention_delta": row["mention_delta"],
            "net_sentiment": round(row["net_sentiment"], 2),
            "dispersion": round(row["dispersion"], 2),
            "confidence": round(row["confidence"], 2),
        }

    prepared: list[dict[str, Any]] = []
    for ticker in order:
        entry = by_ticker[ticker]
        x = entry["x"]
        has_pdf = entry["pdf"] is not None
        x_spike = bool(x and x["mention_delta"] >= PUMP_MIN_DELTA)
        entry["pump_risk"] = x_spike and not has_pdf
        prepared.append(entry)

    max_tickers = config.PROMPT.get("max_tickers", 15)
    return prepared[:max_tickers]


def summarize(mode: str, table: list[dict[str, Any]]) -> str:
    """Generate the brief from the aggregated table.

    Args:
        mode: 'preopen' or 'postclose'.
        table: The ranked aggregate rows from ``process.aggregate.build``.

    Returns:
        The formatted brief text (Markdown).
    """
    if mode not in _MODE_GUIDANCE:
        raise ValueError(f"Unknown mode {mode!r}; expected 'preopen' or 'postclose'.")

    prepared = _prepare(table)
    if not prepared:
        return f"# {mode.title()} Brief\n\nNo meaningful ticker signal in this run."

    user_message = (
        f"{_MODE_GUIDANCE[mode]}\n\n"
        "Buckets: 'x' = X List narrative/attention; 'pdf' = EOD recap "
        "(credible, actionable). pump_risk=true means an X mention spike with "
        "no PDF corroboration.\n\n"
        "Write the brief in Markdown. Structure it as:\n"
        "1. A one-line headline takeaway.\n"
        "2. 'Top movers' — ranked tickers with mentions, day-over-day delta, "
        "net sentiment + dispersion, and the X-vs-recap split.\n"
        "3. 'Watch / risk' — any pump_risk flags and sharply divided "
        "(high-dispersion) names.\n\n"
        "Data (ranked, JSON):\n"
        f"{json.dumps(prepared, ensure_ascii=False)}"
    )

    message = _client().messages.create(
        model=_model(),
        max_tokens=MAX_OUTPUT_TOKENS,
        temperature=config.PROMPT.get("temperature", 0.2),
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    return "".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    ).strip()
