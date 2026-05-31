# signal-bot

An automated trading-signal summary bot. Twice a day it polls a curated **X
List**, ingests an end-of-day **market-recap PDF** (forwarded to an inbox),
extracts tickers, scores per-ticker sentiment with an LLM, ranks the day's
signal (mentions, day-over-day delta, net sentiment, dispersion, source
weighting), and emails a concise brief — one **pre-open** and one **post-close**.

It is decision-support: the brief flags, you decide.

## Architecture

```
INGEST                         PROCESS                          DELIVER
──────────────                 ───────────────────              ───────────────
X List (twitterapi.io) ─┐
                        │      ticker extraction ($CASHTAG + alias NER)
EOD recap PDF ──────────┼───►  per-ticker sentiment (Anthropic, batched)
(email / IMAP)          │      aggregate + rank + day-over-day delta
                        │      source weighting + dispersion
                        ┘      LLM summary (pre-open / post-close)  ──►  email (SMTP)
                                                                        08:00 / 17:00 ET
```

Two buckets are kept strictly separate: **`x`** (narrative / attention) and
**`pdf`** (the recap — credible / actionable). A mention spike on X with no
recap corroboration is flagged as a possible pump.

## Project structure

```
signal-bot/
├── config.py                 # watchlist, source weights, schedule, prompt + alert knobs
├── main.py                   # entrypoint + orchestration + failure alerting
├── store/db.py               # SQLite: posts, pdf_docs, ticker_daily, runs
├── ingest/
│   ├── x_list.py             # X List via twitterapi.io adapter -> normalized posts
│   └── pdf_email.py          # IMAP -> latest PDF attachment -> text (pymupdf)
├── process/
│   ├── extract.py            # $CASHTAG regex + alias NER -> symbols
│   ├── sentiment.py          # per-(ticker, bucket) sentiment via Anthropic (batched)
│   └── aggregate.py          # rank, day-over-day delta, weighting, dispersion
├── summarize/summarize.py    # LLM summary; pre-open vs post-close variants
├── deliver/email_send.py     # SMTP send (Markdown -> HTML)
└── docs/deployment.md        # GitHub Actions secrets setup

../.github/workflows/schedule.yml     # cron: 2 runs/day (UTC) — lives at the REPO ROOT
```

> The Actions workflow must sit at the repository root (`.github/workflows/`),
> not under `signal-bot/`, or GitHub won't detect it. The job uses
> `working-directory: signal-bot`.

## Setup

Requires Python 3.12+.

```bash
cd signal-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in the values
```

Optional (improves company-name extraction; the bot runs fine without it):

```bash
python -m spacy download en_core_web_sm
```

### Configuration

All secrets are read from the environment / `.env` — never hardcoded. See
`.env.example` for the full list. Key points:

- **X data** comes from **twitterapi.io** via REST (`X-API-Key` header), not the
  official X API. Set `X_PROVIDER_API_KEY` and `X_LIST_ID`.
- **LLM** is **Anthropic**. Set `ANTHROPIC_API_KEY` and `LLM_MODEL` (the model
  name is never hardcoded).
- **PDF ingestion is email/IMAP only** — never read Discord directly. Forward
  the recap to the ingestion inbox; the bot reads it over IMAP.
- **Email** uses a Gmail **app password** (not your account password), entered
  with no spaces.

Non-secret tuning lives in `config.py`: `WATCHLIST`, `SOURCE_WEIGHTS`,
`SCHEDULE_UTC`, `PROMPT`, and the `ALERT_ON_FAILURE` / `ALERT_EMAIL_TO` knobs.

## Running

```bash
python main.py --mode postclose     # what moved today + what's building
python main.py --mode preopen       # overnight + setups to watch
```

- On non-trading days (weekends / NYSE holidays) the run **no-ops** cleanly.
- Add `--force` to run anyway (testing). Note: on a non-trading day `--force`
  won't find a same-day recap PDF, so the brief will be X-only.
- **Post-close** ingests today's recap; **pre-open** reuses the prior trading
  day's recap.

## Data flow per run

```
main.py --mode {preopen|postclose}
  → trading-day check (no-op on weekends/holidays)
  → ingest.x_list.fetch_new()    # new posts since last run, deduped
  → ingest.pdf_email.fetch()     # today's PDF text (post-close)
  → process.aggregate.build()    # ranked table: mentions, Δ, sentiment, dispersion, weighted score
  → summarize.summarize(mode)    # pre-open / post-close brief, with pump-risk flags
  → deliver.email_send.send()    # formatted brief
  → store: posts, pdf_docs, ticker_daily, run metadata
```

`ticker_daily` is persisted every run — the day-over-day delta is the core
signal and needs the prior trading day's counts.

## Reliability

- The X adapter retries on rate limits / transient 5xx with backoff.
- Each source is isolated: one provider failing degrades the brief (with a
  warning section) instead of killing the run.
- A run that ingests nothing prepends a loud banner to the brief.
- An unhandled failure emails a `⚠️ signal-bot FAILED …` alert, records the
  error in the `runs` table, and re-raises so the CI job goes red.

## Storage

SQLite (`signal_bot.db`, path overridable via `DB_PATH`). Tables: `posts`,
`pdf_docs`, `ticker_daily`, `runs`. The DB and `.env` are git-ignored.

## Deployment

Runs on GitHub Actions cron (twice daily, weekdays). See
[`docs/deployment.md`](docs/deployment.md) for the repository-secrets setup and
the manual-dispatch verification step to run before trusting the schedule.
