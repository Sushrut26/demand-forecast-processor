# Demand Forecast Processor

A free desktop tool for demand forecasting. You give it the path of an Excel
workbook, it runs the forecast, and it writes a folder of Excel outputs next to
the input file. It runs fully offline: no server, no account, no sign-up.

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
| Logging | Plain-text log files (app log plus a per-run log) |
| Packaging | PyInstaller standalone executable (`packaging/desktop_app.spec`) |

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python desktop_app.py
```

Type or paste the full path of your workbook into the input box (Browse can fill
it in for you), then press **Run forecast**.

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

`tests/test_engine.py` runs the engine end to end on a generated workbook and
checks bucketing, classification and the output files.

## Layout

```
app/
  forecast_engine.py      # the forecasting core
desktop_app.py            # Tkinter UI and text-file logging
packaging/
  desktop_app.spec        # PyInstaller build spec
tools/
  make_sample_workbook.py # generate a small test input workbook
tests/
  test_engine.py          # end-to-end engine test
```
