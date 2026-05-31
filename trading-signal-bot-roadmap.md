# Automated Trading-Signal Summary Bot — Project Roadmap

**Goal:** Replace the daily manual scroll through X (Twitter) and Discord with an automated pipeline that ingests trader chatter and paid-group trade calls, extracts the tickers and sentiment that matter, and delivers two concise briefs per day — one **pre-market (~8:00 AM ET)** and one **post-close (~5:00 PM ET, ~1 hr after the 4:00 PM close)**.

**Owner:** Peter
**Status:** Planning
**Last updated:** 2026-05-31

---

## 1. Scope

**In scope**
- Monitoring a curated set of traders on X via an X **List** (the "Following" signal).
- Ingesting a daily EOD market-recap PDF from a paid Discord group.
- Ingesting daily trade calls (calls/puts) from paid Discord groups — *subject to a compliant access path being secured (see §2).*
- Ticker extraction, per-ticker sentiment, aggregation/ranking, and LLM summarization.
- Twice-daily delivery to a single channel (email / Telegram / a private Discord channel you own).

**Out of scope (initially)**
- Replicating the X **"For You"** algorithmic feed — not exposed by any API. The List-based "Following" signal is the practical substitute.
- Auto-execution of trades. This is a *decision-support* tool, not an order router.
- Multi-user / productized version.

---

## 2. Key constraints & design decisions (resolve before building)

These are the decisions that determine whether the project is cheap and clean or expensive and fragile.

### 2.1 X data access
- **"For You" is not available via API.** Rebuild your "Following" tab as a curated **X List**, then poll the List timeline.
- **Cost:** X is pay-per-use as of early 2026 (~$0.005 per post read, 2M reads/month cap). A single-user tool polling a List twice daily is a few dollars/month at most.
- **Decision:** Official X API (clean, first-party, you control the key) **vs.** a third-party reseller (twitterapi.io, GetXAPI — ~5–15x cheaper but adds a dependency and its own ToS). Recommendation: **start on the official API** unless cost becomes a real factor at your volume.
- **Set a hard spending cap** in the X developer console on day one.

### 2.2 Discord access — the critical risk
- **Automating your personal Discord account to read channels = self-bot = ToS violation = account-ban risk.** Do not build on this. You'd be risking paid memberships and your account.
- **Compliant ingestion paths, in priority order:**
  1. **Ask each provider** whether they push to email, Telegram, RSS, webhook, or a members portal. Use that as the ingestion point.
  2. **EOD PDF:** pull from email or members-area download if available (email ingestion automates trivially).
  3. **Manual-drop bridge:** you save/forward the PDF and any key calls into a folder or a private Discord channel *you own*, which a legitimate bot watches. ~10 sec/day, fully compliant.
- **Decision required:** confirm the access path for (a) the EOD PDF and (b) the trade calls before committing to Phase 1 scope. Treat anything you can't access compliantly as out of scope for now.

### 2.3 Delivery channel
- Options: **email**, **Telegram bot** (recommended — push notifications, easy formatting, free), or a **private Discord channel you own** (a legitimate bot posting to your own server is fine).
- **Decision required:** pick one to start.

### 2.4 Hosting / scheduling
- Options: **GitHub Actions** (near-free, cron-scheduled, simplest to start) **vs.** a **$5 VPS** with cron **vs.** **AWS Lambda + EventBridge**.
- Recommendation: **GitHub Actions** for the MVP; migrate to a VPS/Lambda if run-time or secret-management needs grow.

---

## 3. Target architecture

```
INGEST                         PROCESS                              DELIVER
──────────────                 ───────────────────                 ─────────────────
X List poll ─────┐
                 │             raw store (SQLite → Postgres)
EOD PDF ─────────┼──────────►  → ticker extraction ($CASHTAG + NER)
(email/portal/   │             → per-ticker sentiment (LLM)
 manual drop)    │             → aggregate, rank, day-over-day delta
                 │             → source weighting + signal/noise filter
Trade calls ─────┘             → LLM summary (pre-open / post-close variants)
(compliant path)                              │
                                              ▼
                              email / Telegram / your own Discord
                              @ 08:00 ET (pre-open) and 17:00 ET (post-close)
```

**Suggested stack:** Python · `tweepy`/`httpx` for X · `pdfplumber` or `pymupdf` for the PDF · an LLM API for sentiment + summarization · SQLite (→ Postgres if needed) for state · scheduled via GitHub Actions/cron.

---

## 4. Signal-quality design (don't skip — this is where the value is)

A raw "most-mentioned tickers" list gets dominated by noise and low-float pump names. Build these in early:

- **Day-over-day mention delta** — a ticker jumping 2 → 40 mentions is the real signal, not one that's always loud.
- **Source weighting** — a call from a trader with a track record ≠ a random reply. Maintain a simple weight per source.
- **Net sentiment + dispersion** — "everyone bullish" vs. "sharply divided" are different trades; report both the net and the spread.
- **Narrative vs. actionable separation** — keep X chatter (narrative/attention) distinct from Discord trade calls (actionable). They justify different conviction; never blur them in the summary.
- **Low-float / pump guardrail** — flag tickers whose mention spike isn't backed by any named, credible source.

---

## 5. Phased roadmap

### Phase 0 — Scoping & decisions *(target: 1 week)*
- [ ] Build the curated X **List** of traders you actually act on.
- [ ] Confirm compliant access for the **EOD PDF** and **trade calls** (§2.2).
- [ ] Choose: X official API vs. reseller; delivery channel; hosting.
- [ ] Set X spending cap; obtain API keys; set up secret storage.
- **Exit criteria:** every data source has a confirmed, ToS-compliant access path and a chosen delivery channel.

### Phase 1 — Ingestion MVP *(target: 1 week)*
- [ ] Poll the X List, store raw posts (author, text, timestamp, engagement).
- [ ] Ingest the EOD PDF from the chosen path; extract text reliably.
- [ ] Ingest trade calls from the chosen path.
- **Exit criteria:** raw data from all confirmed sources lands in the store on a manual run.

### Phase 2 — Processing *(target: 1–2 weeks)*
- [ ] Ticker extraction: `$CASHTAG` regex + NER for company names → resolve to symbols.
- [ ] Per-ticker sentiment via LLM (with confidence/dispersion).
- [ ] Aggregation: ranked tickers, net sentiment, **day-over-day delta**, source weighting.
- [ ] Separate narrative (X) vs. actionable (Discord) buckets.
- **Exit criteria:** a structured daily ticker table with sentiment, deltas, and source attribution.

### Phase 3 — Summarization & delivery *(target: 1 week)*
- [ ] LLM summary with two prompt variants: **pre-open** (overnight + setups to watch) and **post-close** (what moved, what's building).
- [ ] Format for the chosen channel; wire up delivery.
- **Exit criteria:** a clean, readable brief delivered on a manual trigger.

### Phase 4 — Automation & reliability *(target: 1 week)*
- [ ] Schedule both runs (08:00 ET / 17:00 ET; handle market holidays).
- [ ] Dedup across days; persist state; idempotent runs.
- [ ] Failure alerting (so a silent break doesn't leave you blind on a trading day).
- **Exit criteria:** unattended for a full week with no manual intervention.

### Phase 5 — Enrichment *(optional, later)*
- [ ] Join price/volume data (e.g., a market-data API) to flag mention spikes that line up with actual moves.
- [ ] Lightweight backtest: do "high-delta + bullish" tickers actually outperform? Kill the features that don't predict anything.
- [ ] Trader track-record scoring to auto-tune source weights.

---

## 6. Rough cost estimate (single user)

| Item | Estimate |
|---|---|
| X API (official, pay-per-use, ~2 polls/day) | ~$1–10 / month |
| LLM API (sentiment + 2 summaries/day) | ~$5–20 / month |
| Hosting (GitHub Actions / $5 VPS) | $0–5 / month |
| Market-data API (Phase 5, optional) | $0–30 / month |
| **Total (Phases 1–4)** | **~$10–35 / month** |

A third-party X reseller can push the data line toward ~$0. Numbers scale with poll frequency and summary length.

---

## 7. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Discord self-bot ban | Don't automate the user account; use a compliant ingestion path (§2.2). |
| X "For You" not accessible | Use a curated List as the "Following" substitute; accept the scope limit. |
| X API cost runaway | Hard spending cap + low poll frequency. |
| Garbage-in summaries (noise/pumps) | Signal-quality layer (§4): deltas, source weighting, pump guardrail. |
| Silent pipeline failure on a trading day | Failure alerting in Phase 4. |
| Over-reliance on the bot's read | Keep it decision-*support*; the brief flags, you decide. |
| Provider changes format/access | Keep ingestion adapters modular; one source breaking shouldn't break the run. |

---

## 8. Open decisions (need answers to finalize)

1. **Discord access** — what does each paid group actually offer (email/Telegram/webhook/portal), and is the EOD PDF available outside Discord?
2. **X data source** — official API or reseller?
3. **Delivery channel** — email, Telegram, or your own Discord?
4. **Coding ownership** — fully self-built vs. scaffolded with help vs. mostly delegated?

Answering these turns Phase 0 from a week into an afternoon.
