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
# Surface an unrecognized cashtag only once it's mentioned this many times in a
# run (below this is likely a typo, not a missing ticker).
UNKNOWN_CASHTAG_MIN = 3

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

# How the LLM should treat the recap (PDF) bucket's availability. This controls
# TONE so an EXPECTED absence (weekend / not-yet-posted) is not described as a
# crisis, while a genuinely missing recap on a trading day stays flagged.
_RECAP_GUIDANCE = {
    "present": (
        "An EOD recap IS available (the 'pdf' bucket) — use it as the credible "
        "anchor against the X narrative."
    ),
    "not_expected": (
        "IMPORTANT: today is a NON-TRADING DAY, so NO EOD recap exists or is "
        "expected. The lack of recap/PDF corroboration is entirely NORMAL — do "
        "NOT label it critical, alarming, or a problem, and do NOT open with a "
        "'zero PDF corroboration' alarm. Because there is no recap at all, "
        "pump_risk is true for every spiking ticker BY DEFINITION and is NOT a "
        "meaningful pump signal here — do not call these pumps. Present the X "
        "narrative as the day's attention picture."
    ),
    "pending": (
        "IMPORTANT: today's EOD recap has not been posted yet (normal timing). "
        "Treat the missing recap as EXPECTED and routine — do NOT raise it as "
        "critical. Because no recap is available this run, pump_risk is true for "
        "every spiking ticker BY DEFINITION and is NOT a meaningful pump signal "
        "— do not call these pumps. Anchor on the X narrative and note calmly "
        "that recap corroboration is pending."
    ),
    "missing": (
        "WARNING: today's EOD recap SHOULD be available but is absent — this may "
        "be an ingestion/email failure. Flag that recap corroboration is missing "
        "as a data-pipeline caveat. Note that with no recap, pump_risk is true "
        "for every spiking ticker by definition, so treat those flags with that "
        "caveat rather than as confirmed pumps."
    ),
}

# Deterministic, top-of-brief note for each non-present recap state (so the
# wording can't be dropped or softened by the model). 'present' adds nothing.
_RECAP_NOTE = {
    "not_expected": "> _Non-trading day — no EOD recap expected. Brief is X-only._\n\n",
    "pending": (
        "> _EOD recap not available yet — anchored on X only; "
        "it may post later this session._\n\n"
    ),
    "missing": (
        "> ⚠️ **Today's EOD recap is missing on a trading day — likely an "
        "ingestion/email failure. Below is X-only; verify the recap email "
        "pipeline.**\n\n"
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
        cell = {
            "mentions": row["mention_count"],
            "mention_delta": row["mention_delta"],
            "net_sentiment": round(row["net_sentiment"], 2),
            "dispersion": round(row["dispersion"], 2),
            "confidence": round(row["confidence"], 2),
        }
        if row.get("stance_divergence"):
            cell["stance_divergence"] = True
            cell["divergence_note"] = row.get("divergence_note", "")
        by_ticker[ticker][bucket] = cell

    watchlist = {t.upper() for t in config.WATCHLIST}
    prepared: list[dict[str, Any]] = []
    for ticker in order:
        entry = by_ticker[ticker]
        x = entry["x"]
        has_pdf = entry["pdf"] is not None
        x_spike = bool(x and x["mention_delta"] >= PUMP_MIN_DELTA)
        entry["pump_risk"] = x_spike and not has_pdf
        entry["watchlist"] = ticker.upper() in watchlist
        prepared.append(entry)

    max_tickers = config.PROMPT.get("max_tickers", 15)
    return prepared[:max_tickers]


def _dir(score: float) -> str:
    """Compact direction tag for a net sentiment score."""
    if score >= 0.5:
        return "bull"
    if score <= -0.5:
        return "bear"
    if score > 0:
        return "lean+"
    if score < 0:
        return "lean-"
    return "flat"


def sms_digest(mode: str, table: list[dict[str, Any]], max_movers: int = 3) -> str:
    """Build a short plaintext SMS summary: headline + top movers + flags.

    Deterministic (no LLM). Derived from the same ranked table the email uses,
    so the text and the full brief never disagree.
    """
    label = "Pre-open" if mode == "preopen" else "Post-close"
    prepared = _prepare(table)
    if not prepared:
        return f"signal-bot {label}: no ticker signal today."

    lines = [f"signal-bot {label} — top {min(max_movers, len(prepared))}:"]
    flagged: list[str] = []
    for entry in prepared[:max_movers]:
        ticker = entry["ticker"]
        parts: list[str] = []
        if entry["x"]:
            parts.append(f"X {_dir(entry['x']['net_sentiment'])} ({entry['x']['mentions']})")
        if entry["pdf"]:
            parts.append(f"recap {_dir(entry['pdf']['net_sentiment'])}")
        lines.append(f"{ticker}: {', '.join(parts) or 'n/a'}")
        if entry.get("pump_risk"):
            flagged.append(f"{ticker} pump?")
        if entry["pdf"] and entry["pdf"].get("stance_divergence"):
            flagged.append(f"{ticker} diverg")
    if flagged:
        lines.append("flags: " + ", ".join(flagged))
    return "\n".join(lines)


# Plain-language column glossary, appended to every brief that has a table so a
# casual reader can decode it. Deterministic (not LLM-written) so it's always
# present and always matches the standardized column set requested in the prompt.
_LEGEND = (
    "\n\n---\n\n## How to read this brief\n"
    "- **Ticker** — the stock symbol (e.g. NVDA = Nvidia).\n"
    "- **Mentions** — how many posts in this run talked about it. More = more attention.\n"
    "- **Δ (change)** — change in mentions vs. the previous run. A big jump is the real "
    "signal (sudden buzz), not a name that's always loud.\n"
    "- **Net sentiment** — overall bullish/bearish lean, from −1.0 (very bearish) to "
    "+1.0 (very bullish); 0 is neutral.\n"
    "- **Dispersion** — how much the crowd disagrees: near 0 = everyone agrees, near 1 = "
    "sharply split (a divided, riskier read).\n"
    "- **Confidence** — how trustworthy the read is; clear, repeated mentions score higher.\n"
    "- **Notes / Signal** — plain-English takeaway for that ticker.\n"
    "- **pump_risk** — a mention spike with no EOD-recap backing: treat as possible hype, "
    "not a confirmed move.\n"
    "- **X vs. recap** — *X* is trader chatter from the X List (attention/narrative); "
    "*recap* is the end-of-day recap PDF (a credible, curated source). They're kept "
    "separate on purpose — never blend them.\n"
)


def _render_unknown_cashtags(counts: dict[str, int] | None) -> str:
    """Markdown section naming frequently-mentioned non-whitelisted cashtags.

    Only cashtags at/above ``UNKNOWN_CASHTAG_MIN`` mentions are listed; these
    are informational (possible new/missing tickers), never scored or ranked.
    """
    if not counts:
        return ""
    frequent = sorted(
        ((sym, n) for sym, n in counts.items() if n >= UNKNOWN_CASHTAG_MIN),
        key=lambda kv: (-kv[1], kv[0]),
    )
    if not frequent:
        return ""
    listed = ", ".join(f"${sym} x{n}" for sym, n in frequent)
    return (
        "\n\n---\n\n## Unrecognized cashtags (possible new/missing tickers)\n"
        f"{listed}\n\n"
        "*Not scored or ranked — review and add to the whitelist if real.*"
    )


def summarize(
    mode: str,
    table: list[dict[str, Any]],
    unknown_cashtags: dict[str, int] | None = None,
    recap_status: str = "present",
) -> str:
    """Generate the brief from the aggregated table.

    Args:
        mode: 'preopen' or 'postclose'.
        table: The ranked aggregate rows from ``process.aggregate.build``.
        unknown_cashtags: Optional {symbol: run mention count} of regex-valid
            but non-whitelisted cashtags. Frequent ones are surfaced in a
            trailing informational section (never scored).
        recap_status: How to describe the EOD-recap (PDF) bucket's absence:
            'present'      — a recap was used (no note).
            'not_expected' — non-trading day; absence is normal (calm note).
            'pending'      — trading day, recap not posted yet (informational).
            'missing'      — trading day, recap genuinely absent (prominent warning).

    Returns:
        The formatted brief text (Markdown).
    """
    if mode not in _MODE_GUIDANCE:
        raise ValueError(f"Unknown mode {mode!r}; expected 'preopen' or 'postclose'.")
    if recap_status not in _RECAP_GUIDANCE:
        raise ValueError(f"Unknown recap_status {recap_status!r}.")

    unknown_section = _render_unknown_cashtags(unknown_cashtags)
    recap_note = _RECAP_NOTE.get(recap_status, "")

    prepared = _prepare(table)
    if not prepared:
        return (
            recap_note
            + f"# {mode.title()} Brief\n\nNo meaningful ticker signal in this run."
            + unknown_section
        )

    user_message = (
        f"{_MODE_GUIDANCE[mode]}\n\n"
        f"{_RECAP_GUIDANCE[recap_status]}\n\n"
        "Buckets: 'x' = X List narrative/attention; 'pdf' = EOD recap "
        "(credible, actionable) — the pdf net_sentiment is the recap's own "
        "stated call. pump_risk=true means an X mention spike with no PDF "
        "corroboration. watchlist=true means the reader actively follows this "
        "ticker — surface it even if its rank is modest. If a pdf bucket has "
        "stance_divergence=true, explicitly note it using divergence_note "
        "(the recap's headline label disagrees with the tone of its own "
        "notes).\n\n"
        "Write the brief in Markdown. Structure it as:\n"
        "1. A one-line headline takeaway.\n"
        "2. 'Top movers' — render as a Markdown table with EXACTLY these columns "
        "in this order: Ticker | Mentions | Δ | Net sentiment | Dispersion | "
        "Confidence | Notes. One row per ticker; put the X-vs-recap split and any "
        "caveats in Notes.\n"
        "3. 'Watch / risk' — any pump_risk flags and sharply divided "
        "(high-dispersion) names.\n"
        "Do NOT add your own glossary or column key — one is appended "
        "automatically after your brief.\n\n"
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
    brief = "".join(
        block.text for block in message.content if getattr(block, "type", "") == "text"
    ).strip()
    # Prepend the recap note and append the unknowns + column legend
    # deterministically (not via the LLM) so none can be dropped or reworded.
    return recap_note + brief + unknown_section + _LEGEND
