# Demand Forecast Processor

A desktop tool for demand forecasting. You give it the path of an Excel
workbook, it runs the forecast, and it writes a folder of Excel outputs next to
the input file. No server is needed. Access is controlled by signed,
time-limited license keys (Ed25519); the forecasting runs fully on your machine.

## What it does

Input: one Excel workbook. The first column holds dates, a `Bucket Days` row sets
the bucket size, then there is one demand column per category. For each category
the engine buckets demand into N-day windows, corrects outliers with the IQR rule,
classifies the series (CV² / ADI), and then:

- **Smooth series:** grid-searches 8 models (ARIMA, SARIMAX, SES, Holt, Holt-Winters,
  ETS, Moving Average and Linear Regression) and keeps the one with the lowest MAE.
- **Non-smooth series:** builds a cumulative-frequency table and chart.

Output: a new `forecastoutput_N/` folder next to the input file with one workbook
per category, plus `aggregated_forecasts.xlsx`, `summary_cfd.xlsx`,
`processing_summary.xlsx` and a `run_log.txt` describing the run.

## Logs

Every action is written to a plain-text log, `logs/demand_forecast_log.txt`, in the
folder the app runs from. Each run also writes `run_log.txt` into its output
folder. Errors show the log file's location so you can open it directly.

## Architecture

| Concern | Choice |
|---|---|
| UI | Tkinter (`desktop_app.py`): type or paste the input path, run, see status |
| Forecasting | `app/forecast_engine.py`, parallel per category |
| Licensing | Ed25519-signed, time-limited keys (`app/licensing.py`) |
| License storage | Encrypted local file (`app/license_manager.py`) |
| Anti-tamper time | World-time API lookup with a system-clock fallback |
| Logging | Plain-text log files (app log plus a per-run log) |
| Packaging | PyInstaller standalone executable (`packaging/desktop_app.spec`) |

## How licensing works

- A key's payload is `{customer_name, issued_at, expires_at, product_code}`, signed with an
  Ed25519 private key that stays with whoever issues keys.
- The app embeds only the matching public key (`LicenseManager.PUBLIC_KEY_PEM` in
  `app/license_manager.py`), so it can verify a key but never create one. The
  private key is never bundled into the app.
- On every launch and before every run, the app re-verifies the stored key and
  checks its expiry against a world-time API, so turning back the system clock
  doesn't extend access. If no time API is reachable it falls back to the
  system clock.
- When a key expires, the app blocks runs and asks for a new key; activating one
  restores access straight away.

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### Create a signing key pair (once)

```bash
python tools/make_signing_key.py
export LICENSE_SIGNING_KEY="<private PEM>"
export LICENSE_PUBLIC_KEY="<public PEM>"   # dev convenience; for a build, paste it into LicenseManager.PUBLIC_KEY_PEM
```

### Issue a key

```bash
python tools/generate_license.py   # asks for the name and the duration in days (default 2)
```

### Run the app

```bash
python desktop_app.py
```

The first launch asks for the name and key from the step above. Then type or
paste the full path of your workbook into the input box (Browse can fill it in
for you) and press **Run forecast**.

To try it on sample data, generate a small test workbook and enter its path:

```bash
python tools/make_sample_workbook.py sample/sample_input.xlsx
```

## Build a standalone executable

```bash
pyinstaller packaging/desktop_app.spec
```

Output: `dist/DemandForecastProcessor` (or `.exe` on Windows).

## Tests

```bash
pip install pytest
python -m pytest tests/ -q
```

- `tests/test_engine.py` runs the engine end to end on a generated workbook and
  checks bucketing, classification and the output files.
- `tests/test_licensing.py` checks key signing, verification, expiry and tampering.
- `tests/test_license_manager.py` checks activation, expiry, renewal and tampered
  local files, with the time source mocked so no network is needed.

## Layout

```
app/
  forecast_engine.py      # the forecasting core
  licensing.py            # Ed25519 sign / verify and expiry helpers
  license_manager.py      # encrypted local license storage and tamper-resistant time
  config.py               # env-driven settings (signing key, product code, default duration)
desktop_app.py            # Tkinter UI: license check, input path, run, text-file logging
packaging/
  desktop_app.spec        # PyInstaller build spec
tools/
  make_signing_key.py     # one-time: generate the Ed25519 key pair
  generate_license.py     # issue a signed, time-limited key
  make_sample_workbook.py # generate a small test input workbook
tests/                    # engine, licensing and license-manager tests
```
