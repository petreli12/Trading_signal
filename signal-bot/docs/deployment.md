# Deployment — GitHub Actions Secrets

The bot runs on the GitHub Actions cron in `.github/workflows/schedule.yml`
(twice daily, weekdays). It reads **all** configuration from environment
variables; in CI those come from **repository secrets**. Nothing is committed —
`.env` is git-ignored and used only for local runs.

> **Workflow location:** the workflow lives at the **repository root**
> (`.github/workflows/schedule.yml`), not under `signal-bot/` — GitHub Actions
> only detects workflows at the root. The job runs with
> `working-directory: signal-bot`.

## 1. Secrets to create

Add each of the following as a repository secret. These mirror `signal-bot/.env.example`
and are mapped into the job env in `schedule.yml`. The secret name **must exactly
match** the variable name (case-sensitive).

| # | Secret name | What it is |
|---|---|---|
| 1 | `X_PROVIDER_API_KEY` | twitterapi.io API key (sent as the `X-API-Key` header) |
| 2 | `X_LIST_ID` | The curated X List ID to poll |
| 3 | `ANTHROPIC_API_KEY` | Anthropic API key (sentiment + summary) |
| 4 | `LLM_MODEL` | Anthropic model name (e.g. `claude-haiku-4-5-20251001`) |
| 5 | `IMAP_USER` | Ingestion inbox address (reads the forwarded EOD PDF) |
| 6 | `IMAP_PASSWORD` | Gmail **app password** — paste with **no spaces** |
| 7 | `SMTP_USER` | Sending account address |
| 8 | `SMTP_PASSWORD` | Gmail **app password** — paste with **no spaces** |
| 9 | `EMAIL_TO` | Where the brief is delivered (comma-separated for multiple) |
| 10 | `IMAP_HOST` | IMAP server (e.g. `imap.gmail.com`) |
| 11 | `SMTP_HOST` | SMTP server (e.g. `smtp.gmail.com`) |

> **11 strictly required.** `SMTP_PORT` is **optional** — it defaults to `587`
> in code, so add it (#12) only if your provider uses a different port (e.g.
> `465` for SSL).

### Optional (failure alerting)

These have safe defaults; add them only to override behavior:

| Secret name | Default | Purpose |
|---|---|---|
| `ALERT_ON_FAILURE` | `true` | Set to `false` to disable the hard-failure alert email |
| `ALERT_EMAIL_TO` | falls back to `EMAIL_TO` | Send failure alerts to a different address |

> An unset `ALERT_ON_FAILURE` secret is treated as enabled — alerting only turns
> off when the value is explicitly `false`/`0`/`no`/`off`.

## 2. How to add each secret

1. Go to your repository on GitHub.
2. **Settings → Secrets and variables → Actions**.
3. Open the **Secrets** tab and click **New repository secret**.
   - Create a **repository secret**, *not* an **environment secret** — the
     workflow reads `${{ secrets.NAME }}` at the repository scope.
4. Set **Name** to the exact variable name from the table above and **Secret**
   to its value, then **Add secret**.
5. Repeat for every required secret.

Notes:
- **Gmail app passwords:** strip all spaces. Google displays them as
  `abcd efgh ijkl mnop`; enter `abcdefghijklmnop`.
- **Exact names:** a typo (e.g. `ANTHROPIC_KEY`) leaves the var unset and the
  run fails. Names must match `schedule.yml` exactly.

## 3. Confirm the workflow maps every secret

`schedule.yml` injects each secret into the run step's `env:` block. The mapped
variables are:

```
X_PROVIDER_API_KEY, X_LIST_ID, ANTHROPIC_API_KEY, LLM_MODEL,
IMAP_HOST, IMAP_USER, IMAP_PASSWORD,
SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_TO,
ALERT_ON_FAILURE, ALERT_EMAIL_TO
```

If you add a new env var in code, add a matching line to that `env:` block.

## 4. Verify with a manual run BEFORE trusting the cron

Do not wait for the scheduled run to discover a bad secret.

1. Push the repo (with `.github/workflows/schedule.yml`) to GitHub.
2. **Actions → "signal-bot schedule" → Run workflow** (this uses
   `workflow_dispatch`). Pick a mode (`postclose` is fine). On a non-trading day
   (weekend/holiday) the run no-ops, so tick **force** to exercise the full
   pipeline and get a brief; on a trading day leave it off.
3. Confirm:
   - the job finishes **green**, and
   - the **brief email arrives** at `EMAIL_TO`.
4. If the job goes **red**, open the failed step's logs. You should also receive
   a `⚠️ signal-bot FAILED …` alert email (unless SMTP itself is the problem).
   Fix the offending secret and re-run the dispatch.
5. Only once a manual dispatch succeeds end-to-end should you rely on the cron.

### Scheduling reference

- Cron is **UTC**. The workflow targets EDT: `12:00` UTC pre-open, `21:00` UTC
  post-close. During EST these land an hour earlier in ET; `main.py` no-ops on
  non-trading days, so an off-by-an-hour on holidays is harmless.
- GitHub may delay scheduled jobs under load; the twice-daily cadence tolerates
  this.
