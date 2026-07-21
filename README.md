# Godrej Forecast Platform

An enterprise, multi-user web application built around the Godrej demand-forecasting
engine. Users log in, upload a demand workbook, run a forecast job, and download the
result bundle. Accounts are vendor-provisioned, each with a **locked data-retention
setting** and **time-limited license keys** (default 2 days, admin-configurable).

It is the web evolution of the original single-user desktop tool: the forecasting
**engine logic is preserved unchanged** (same bucketing, IQR outlier correction,
CV²/ADI classification, full model grid-search across 8 methods, and the same Excel
outputs). Because the server now owns identity, time, and storage, the desktop
anti-tamper machinery (machine binding, world-time lookups, encrypted local file) is
gone — the database is the source of truth.

## What it does

Input: one Excel workbook — first column = dates, a `Bucket Days` row, then one demand
column per category. For each category the engine buckets demand into N-day windows,
corrects outliers, classifies the series, and either:
- **Smooth** series → grid-searches ARIMA, SARIMAX, SES, Holt, HWES, ETS, Moving
  Average, and Linear Regression, picking the lowest-MAE model, or
- **non-smooth** series → builds a cumulative-frequency table + chart.

Output: a downloadable **zip** containing one workbook per category plus
`aggregated_forecasts.xlsx`, `summary_cfd.xlsx`, and `processing_summary.xlsx`.

## Architecture

Single FastAPI service (UI + API + engine + one background worker):

| Concern | Choice |
|---|---|
| Web + UI | FastAPI + Jinja2 templates + Tailwind (CDN) |
| Forecasting | `app/forecast_engine.py` (ported verbatim) |
| Jobs | In-process single-consumer queue → one job at a time (`app/jobs.py`) |
| Database | Postgres (Supabase) in prod; SQLite locally by default |
| File storage | Supabase Storage (retention ON); ephemeral temp dir otherwise |
| Auth | Signed session cookies, bcrypt passwords, admin/user roles |
| Licensing | Ed25519 signed, time-limited tokens (`app/licensing.py`) |

## Roles

- **Admin** (seeded from env): create accounts + users, toggle each account's
  `store_data` retention flag, generate/revoke license keys with a configurable
  duration, and review global job history.
- **User**: activate a license key, upload & run jobs, download results, view history.
  The retention setting is shown read-only — changing it means contacting the vendor.

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# (optional) generate a persistent signing key and export it:
python tools/make_signing_key.py
export LICENSE_SIGNING_KEY="<paste the private PEM, or its base64/\\n form>"

export ADMIN_EMAIL=admin@yourco.com ADMIN_PASSWORD=change-me
uvicorn app.main:app --reload
```

Then open http://localhost:8000/login. With no `DATABASE_URL` set it uses a local
SQLite file (`./data/app.db`); with no Supabase config, retention-ON files persist to
`./data/storage`. A sample input workbook can be generated with:

```bash
python tools/make_sample_workbook.py sample/sample_input.xlsx
```

## Deploy on Render (free)

1. **Supabase**: create a free project. Copy the Postgres connection string
   (`DATABASE_URL`). For persistent file storage, also copy `SUPABASE_URL` and the
   service-role key (`SUPABASE_SERVICE_KEY`); a `forecast-files` bucket is created
   automatically on first use.
2. **Signing key**: run `python tools/make_signing_key.py` and keep the private key
   for `LICENSE_SIGNING_KEY` (secret — anyone with it can mint keys).
3. **Render**: create a Blueprint from this repo (`render.yaml`). Set the secret env
   vars in the dashboard: `DATABASE_URL`, `LICENSE_SIGNING_KEY`, `ADMIN_EMAIL`,
   `ADMIN_PASSWORD`, and (optionally) the Supabase storage vars. `SECRET_KEY` is
   generated for you.
4. Deploy. Visit `/login`, sign in as the admin, and provision your first account.

### Admin runbook

1. **Create account** → set its data-retention flag and the first user's credentials.
2. **Generate license key** (duration in days, defaults to `DEFAULT_LICENSE_DAYS`=2).
   Copy the shown token and send it to the customer.
3. Customer **logs in → Activate** the key → access is granted until it expires.
4. On expiry the customer is blocked from running jobs; **issue a new key** and they
   activate it to continue. **Revoke** invalidates a key and recomputes account access.

## Free-tier notes

Render's free tier is 1 CPU / 512 MB and sleeps when idle. Jobs run one at a time to
stay within memory; the full model search is unchanged, so very large workbooks may be
slow or hit limits. Upload guardrails (`MAX_UPLOAD_MB`, `MAX_ROWS`,
`MAX_CATEGORY_COLUMNS`) reject oversized inputs early. Raising the plan removes the
ceiling with no code changes.

## Tests

```bash
pip install pytest
python -m pytest tests/ -q
```

Covers engine output parity and license token signing/validation/expiry/tamper checks.

## Layout

```
app/
  main.py            # app wiring, admin seeding, worker launch
  config.py          # env-driven settings
  db.py models.py    # SQLAlchemy engine + ORM models
  security.py auth.py# password hashing, sessions, login/logout
  licensing.py       # Ed25519 sign/verify + expiry
  storage.py jobs.py # persistent/ephemeral storage + background worker
  forecast_engine.py # the forecasting core (ported verbatim)
  routes_user.py routes_admin.py
  templating.py templates/ static/
tools/               # signing-key + sample-workbook generators
tests/               # engine + licensing tests
render.yaml          # Render blueprint
```
