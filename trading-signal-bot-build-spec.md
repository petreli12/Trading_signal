# Trading-Signal Summary Bot — Build Spec (for Cursor)

A concrete, build-ready spec for self-building the bot in Cursor. Pairs with the project roadmap.

**Last updated:** 2026-05-31

---

## 1. Locked decisions

| Decision | Choice |
|---|---|
| X feed | Curated **X List** (substitute for "Following"), polled twice daily |
| X data source | **Third-party provider** (e.g. twitterapi.io) behind a thin adapter; official X API as later fallback |
| Discord EOD PDF | **Forward-to-email** flow (compliant) — see §2 |
| Discord live trade calls | **Out of scope for v1** (no compliant low-friction ingestion; revisit later) |
| Delivery | **Email** (SMTP) |
| Hosting / schedule | **GitHub Actions** cron, two runs/day |
| Language | Python |

---

## 2. The Discord PDF flow (compliant)

Do **not** read Discord with your user token — that's a self-bot, violates ToS, and risks your paid account. Keep one human step instead:

1. Create a dedicated ingestion inbox (a new Gmail, or a `+alias` on an existing account).
2. Each EOD, download the recap PDF from Discord and forward/email it to that address (mobile share-sheet → email, ~10 sec).
3. The bot connects to that inbox via **IMAP**, finds today's message with a PDF attachment, downloads it, and extracts text.

This keeps the only "reach into Discord" action performed by you, a human member — which is exactly what's allowed. (Alternative: drop the PDF into a synced Drive/Dropbox folder the bot watches; email is simpler for a cron job.)

---

## 3. Project structure

```
signal-bot/
├── requirements.txt
├── .env.example
├── config.py                 # watchlist, source weights, schedule, prompt knobs
├── main.py                   # entrypoint: python main.py --mode preopen|postclose
├── store/
│   └── db.py                 # SQLite: posts, pdf_docs, ticker_daily, runs
├── ingest/
│   ├── x_list.py             # poll X List via provider adapter -> normalized posts
│   └── pdf_email.py          # IMAP read inbox, pull PDF, extract text
├── process/
│   ├── extract.py            # $CASHTAG regex + NER -> resolved symbols
│   ├── sentiment.py          # per-ticker sentiment via LLM
│   └── aggregate.py          # rank, day-over-day delta, source weighting, dispersion
├── summarize/
│   └── summarize.py          # LLM summary; preopen vs postclose prompt variants
├── deliver/
│   └── email_send.py         # SMTP send
└── .github/workflows/
    └── schedule.yml          # cron: 2 runs/day (UTC)
```

Keep `x_list.py` as an adapter with a stable internal output shape (`{id, author, text, ts, engagement}`) so swapping providers later touches only that file.

---

## 4. Environment variables (`.env.example`)

```
# X data provider
X_PROVIDER_API_KEY=
X_LIST_ID=

# LLM (sentiment + summarization)
LLM_API_KEY=
LLM_MODEL=

# Ingestion inbox (reads the forwarded PDF)
IMAP_HOST=imap.gmail.com
IMAP_USER=
IMAP_PASSWORD=          # app-specific password, not your account password

# Delivery (sends the brief)
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=          # app-specific password
EMAIL_TO=
```

In production these become **GitHub Actions secrets**, not a committed file.

---

## 5. Per-run data flow

```
main.py --mode {preopen|postclose}
  → ingest.x_list.fetch()         # new posts since last run
  → ingest.pdf_email.fetch()      # today's PDF text (postclose run; preopen reuses last night's)
  → process.extract.tickers()     # symbols per post/doc
  → process.sentiment.score()     # sentiment per (ticker, source)
  → process.aggregate.build()     # ranked table: mentions, Δ vs prior day, net sentiment, dispersion, weighted score
  → summarize.summarize(mode)     # preopen: overnight + setups; postclose: what moved + what's building
  → deliver.email_send.send()     # formatted brief
  → store: persist posts, ticker_daily, run metadata (for next-day deltas + dedup)
```

Persist `ticker_daily` every run — the day-over-day delta is the core signal and needs yesterday's counts.

---

## 6. Scheduling

- GitHub Actions cron is **UTC**. ET targets:
  - Pre-open **08:00 ET** → `12:00` UTC (EDT) / `13:00` UTC (EST)
  - Post-close **17:00 ET** → `21:00` UTC (EDT) / `22:00` UTC (EST)
- Simplest robust approach: schedule the EDT times and have `main.py` no-op if it's not actually a trading session.
- **Skip non-trading days.** Use `pandas_market_calendars` (NYSE calendar) to bail early on weekends/holidays.

---

## 7. ⚠️ Tell Cursor these things (stale-knowledge guardrails)

Cursor's model will get these wrong by default — paste these constraints into your Cursor rules/context:

1. **X API pricing is pay-per-use as of 2026.** There is no usable free tier and no new Basic/Pro subscriptions. Do **not** generate code assuming elevated/free `tweepy` access or the old tiers. We are calling **[provider]** via REST behind an adapter, not the official API.
2. **Never write a Discord self-bot.** Do not use a user token, `discord.py-self`, or any user-account automation to read channels — it violates Discord ToS and risks an account ban. The PDF arrives via **email/IMAP**, not Discord. Reject any suggestion to read Discord directly.
3. **Use the current LLM model name and endpoint** for whichever provider's key is in `.env` — don't hardcode a model string from memory; read it from `LLM_MODEL`.
4. **No secrets in code.** Everything via `os.environ` / `.env` locally and GitHub secrets in CI.
5. **Email auth uses an app-specific password**, not the account password (Gmail requires this with 2FA).

---

## 8. Cursor build sequence (scoped tasks)

Build and test one module at a time — Cursor works best on small, well-defined diffs.

1. **Scaffold:** create the structure in §3, `requirements.txt`, `.env.example`, and the SQLite schema in `store/db.py` (tables: `posts`, `pdf_docs`, `ticker_daily`, `runs`).
2. **X ingestion:** `ingest/x_list.py` — fetch posts from the List via the provider, normalize to `{id, author, text, ts, engagement}`, dedup against `posts`. Test against a small real pull.
3. **PDF ingestion:** `ingest/pdf_email.py` — IMAP-connect, find the latest message with a PDF attachment, save it, extract text with `pymupdf`. Test on one forwarded PDF.
4. **Extraction:** `process/extract.py` — `$CASHTAG` regex + lightweight NER (e.g. spaCy) → resolved symbols. Unit-test on sample text.
5. **Sentiment:** `process/sentiment.py` — per-(ticker, source) sentiment via LLM, returning a score + confidence. Batch where possible to cut cost.
6. **Aggregation:** `process/aggregate.py` — ranked table with mention count, **day-over-day delta**, net sentiment, dispersion, and a weighted score using per-source weights from `config.py`. Keep X (narrative) and PDF (recap) buckets separate.
7. **Summarize:** `summarize/summarize.py` — two prompt variants (preopen, postclose) that consume the aggregated table; instruct the model to flag mention-spike-without-credible-source as a pump risk.
8. **Deliver:** `deliver/email_send.py` — SMTP send, clean HTML or plaintext.
9. **Orchestrate + schedule:** `main.py` with `--mode`, then `.github/workflows/schedule.yml` cron with secrets.

---

## 9. Definition of done (v1)

- [ ] Manual `python main.py --mode postclose` produces and emails a correct brief from real X + PDF data.
- [ ] Pre-open variant runs and emails correctly.
- [ ] Day-over-day deltas populate correctly on the second day of running.
- [ ] No secrets in the repo; all via env/GitHub secrets.
- [ ] Holiday/weekend runs no-op cleanly.
- [ ] Both runs fire unattended for a full trading week.

---

## 10. First things to do outside Cursor

1. Create the curated **X List** and grab its ID.
2. Sign up for the X data provider; verify current pricing; get the API key.
3. Create the dedicated ingestion inbox; enable IMAP; generate an app password.
4. Generate the SMTP app password for the sending address.
5. Confirm your LLM provider + model string.
