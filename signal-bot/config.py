"""Central configuration: watchlist, source weights, schedule, prompt knobs.

Secrets are NOT stored here — they are read from the environment / .env at
point of use (see each module). This file holds only non-secret tuning knobs.
"""

import os
from datetime import time as _time

from dotenv import load_dotenv

load_dotenv()

# --- Watchlist -----------------------------------------------------------
# Tickers you actively follow. Aggregation still ranks the full mentioned
# universe; the watchlist is surfaced to the summary so these names are called
# out even if their raw rank is lower. Empty list = no special treatment.
WATCHLIST: list[str] = [
    "NVDA", "MSFT", "AAPL", "AMZN", "GOOGL", "META", "TSLA", "AVGO",
    "AMD", "ARM", "PLTR", "QQQ", "SPY", "SMCI", "MU", "COIN", "HOOD",
]

# --- Source weighting ----------------------------------------------------
# Buckets keep X (narrative) and PDF (recap) signal separate. Per-source
# weights feed the weighted_score in process/aggregate.py. The recap PDF is a
# higher-credibility, curated source, so it outweighs raw X chatter.
SOURCE_WEIGHTS: dict[str, float] = {
    "x": 1.0,     # X List narrative / attention
    "pdf": 1.5,   # EOD recap PDF (higher-credibility source)
}

# --- Schedule ------------------------------------------------------------
# GitHub Actions cron is UTC. ET targets documented for reference only.
RUN_MODES = ("preopen", "postclose")
SCHEDULE_UTC = {
    "preopen": "12:00",    # 08:00 ET (EDT)
    "postclose": "22:45",  # 18:45 ET (EDT) — late enough the recap is usually posted
}

# A trading-day EOD recap is expected to be POSTED by this ET wall-clock time.
# Used only to word a post-close run's missing recap:
#   - before this time -> 'pending'  (the recap may simply not be posted yet;
#                                      calm, no alarm — e.g. a manual early run)
#   - at/after          -> 'missing'  (overdue; likely an ingestion/email failure)
# Aligned with the post-close cron (~18:45 ET): by the time that run fires the
# recap is normally already in, so an absent recap then is genuinely notable.
RECAP_EXPECTED_BY_ET = _time(18, 45)  # 6:45 PM ET

# --- Prompt knobs --------------------------------------------------------
# Tuning for the LLM summary step. The model name itself is read from the
# LLM_MODEL env var at call time — never hardcode it here.
PROMPT = {
    "max_tickers": 15,
    "pump_risk_flag": True,   # flag mention-spike-without-credible-source
    "temperature": 0.2,
}

# --- Identifiers ---------------------------------------------------------
X_LIST_ID = os.environ.get("X_LIST_ID", "").strip()

# --- Failure alerting ----------------------------------------------------
# On an unhandled run failure, email an alert (GitHub Actions red is the
# backstop; this is the active notice). Recipient falls back to EMAIL_TO.
# Default ON: only an explicit falsy value disables it. (An unset GitHub
# Actions secret resolves to "", which must NOT silently disable alerting.)
ALERT_ON_FAILURE = os.environ.get("ALERT_ON_FAILURE", "").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
ALERT_EMAIL_TO = (
    os.environ.get("ALERT_EMAIL_TO") or os.environ.get("EMAIL_TO", "")
).strip()
