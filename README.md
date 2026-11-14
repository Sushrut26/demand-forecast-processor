# Demand Forecast Processor (Desktop)

A single-user Windows/desktop application built around a demand-forecasting
engine. The user browses to an Excel workbook, runs the forecast, and gets back a
folder of Excel outputs — no server, no internet dependency beyond an optional
time check. Access is controlled by **signed, time-limited license keys** (default
**2 days**, admin-configurable per key). When a key expires the app blocks further
processing until the vendor issues — and the customer activates — a fresh one.

The forecasting **engine logic is preserved unchanged**: the same demand bucketing,
IQR outlier correction, CV²/ADI classification, and full grid-search across all 8
forecasting methods produce identical output workbooks to the original tool.

## What it does

Input: one Excel workbook — first column = dates, a `Bucket Days` row, then one demand
column per category. For each category the engine buckets demand into N-day windows,
corrects outliers, classifies the series, and either:
- **Smooth** series → grid-searches ARIMA, SARIMAX, SES, Holt, HWES, ETS, Moving
  Average, and Linear Regression, picking the lowest-MAE model, or
- **non-smooth** series → builds a cumulative-frequency table + chart.

Output: a new `forecastoutput_N/` folder next to the input file containing one
workbook per category plus `aggregated_forecasts.xlsx`, `summary_cfd.xlsx`, and
`processing_summary.xlsx`.

## Architecture

| Concern | Choice |
|---|---|
| UI | Tkinter (`desktop_app.py`) — browse, run, status |
| Forecasting | `app/forecast_engine.py` (ported verbatim, unchanged) |
| Licensing | Ed25519 signed, time-limited tokens (`app/licensing.py`) |
| License storage | Encrypted local file (`app/license_manager.py`) |
| Anti-tamper time | World-time API lookup with offline/system-clock fallback |
| Packaging | PyInstaller → standalone `.exe` (`packaging/desktop_app.spec`) |

### Licensing model — how the trust boundary works

- A signed token's payload is `{customer_name, issued_at, expires_at, product_code}`,
  signed with an **Ed25519 private key** only the vendor holds.
- The shipped app embeds only the matching **public key**
  (`LicenseManager.PUBLIC_KEY_PEM` in `app/license_manager.py`) — it can verify a
  token's authenticity but cannot create new ones. **The private key must never be
  bundled into the app or given to a customer.**
- On every launch, and before every run, the app re-verifies the stored token's
  signature and checks `expires_at` against the current time. Time comes from a
  world-time API (`worldtimeapi.org` → `timeapi.io`) so a customer can't simply turn
  back their system clock to extend access; if both APIs are unreachable it falls
  back to the local system clock (`allow_offline_mode = True`).
- When a license lapses, the app blocks "Process" and prompts for a new key. The
  vendor runs `tools/generate_license.py` again with a fresh `duration_days` and
  sends the new token — activating it restores access immediately.

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### One-time setup: generate a signing keypair

```bash
python tools/make_signing_key.py
```

This prints a **public** and **private** PEM. For local development:

```bash
export LICENSE_SIGNING_KEY="<paste the private PEM (or its base64/\n form)>"
export LICENSE_PUBLIC_KEY="<paste the public PEM>"
```

`LICENSE_SIGNING_KEY` is only ever needed by `tools/generate_license.py` (the admin
tool). `LICENSE_PUBLIC_KEY` is a **dev/test convenience** so `desktop_app.py` doesn't
need to be edited while iterating — see "Before shipping" below for the real flow.

### Issue a license key

```bash
python tools/generate_license.py
# Customer name: Acme Corp
# Duration in days [2]: 2
```

This prints the signed token, writes `LICENSE_Acme Corp.txt` with activation
instructions, and appends a line to `generated_licenses.log`.

### Run the app

```bash
python desktop_app.py
```

First launch prompts for activation — paste the customer name and token from the
step above. A sample input workbook for testing:

```bash
python tools/make_sample_workbook.py sample/sample_input.xlsx
```

### Before shipping to a customer

Generate a **real, persistent** keypair (`tools/make_signing_key.py`), keep the
private key secret on your machine only, and paste the **public** key into
`app/license_manager.py`'s `LicenseManager.PUBLIC_KEY_PEM` before building — this is
what makes tokens issued by `tools/generate_license.py` verify correctly in the
shipped app.

## Build a standalone executable

```bash
pip install pyinstaller  # already in requirements.txt
pyinstaller packaging/desktop_app.spec
```

Output: `dist/DemandForecastProcessor` (or `.exe` on Windows). The public key baked
into `app/license_manager.py` at build time travels with the executable; the private
key never does.

## Admin runbook

1. **Issue a key**: `python tools/generate_license.py` → customer name + duration
   (days). Send the customer the printed token (or the generated `LICENSE_<name>.txt`)
   along with the app/exe.
2. **Customer activates**: on first launch (or via "Renew / Activate License"), they
   enter their customer name and paste the token exactly as given.
3. **On expiry**: the app blocks "Process" and shows the expiry date. Issue a new key
   with step 1 (same customer name) and have them activate it — access resumes
   immediately, no reinstall needed.
4. **Changing the default duration**: set `DEFAULT_LICENSE_DAYS` in the environment
   before running `tools/generate_license.py`, or just answer the duration prompt
   directly each time.

## Tests

```bash
pip install pytest
python -m pytest tests/ -q
```

- `tests/test_engine.py` — forecast engine output parity (bucketing, classification,
  end-to-end `process_workbook`).
- `tests/test_licensing.py` — Ed25519 token sign/verify/expiry/tamper checks
  (`app/licensing.py`).
- `tests/test_license_manager.py` — activation, expiry transitions, renewal, tampered
  local file rejection, and customer-name/key mismatches, with the time source
  mocked so tests don't depend on network access.

## Layout

```
app/
  forecast_engine.py    # the forecasting core (ported verbatim, unchanged)
  licensing.py           # Ed25519 sign (admin) / verify (client) + expiry helpers
  license_manager.py     # encrypted local license storage + tamper-resistant time
  config.py               # env-driven settings (signing key, product code, default duration)
desktop_app.py            # Tkinter UI: browse, activate, run, status
packaging/
  desktop_app.spec        # PyInstaller build spec
tools/
  make_signing_key.py     # one-time: generate the Ed25519 keypair
  generate_license.py     # admin: issue a signed, time-limited token
  make_sample_workbook.py # generate a small test input workbook
tests/                     # engine + licensing + license-manager tests
```
