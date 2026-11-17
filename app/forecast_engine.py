"""
Forecast engine — the demand-forecasting core, ported from the original
Demand Forecast Processor desktop application.

The pure forecasting logic is preserved unchanged so that outputs match the
desktop app byte-for-byte in behaviour: the same demand bucketing, IQR outlier
correction, CV2/ADI classification, the full model grid-search across the eight
forecasting methods, and the same Excel output files.

The only things removed relative to the desktop script are the Tkinter UI, the
`SecureLicenseManager`, the world-time APIs, machine-id lookups, and the
`main()` / `__main__` bits — none of which belong on a server, where the
database is the source of truth for identity, time and licensing.

`process_workbook(input_path, output_dir, workers)` is the single entry point
used by the background job worker.
"""

import math
import os
import re
import traceback
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# These imports are deliberately top-level so that multiprocessing workers can use them.
from sklearn.linear_model import LinearRegression
from sklearn.metrics import mean_absolute_error
from statsmodels.tsa.api import SimpleExpSmoothing, Holt
from statsmodels.tsa.arima.model import ARIMA
from statsmodels.tsa.statespace.sarimax import SARIMAX
from statsmodels.tsa.holtwinters import ExponentialSmoothing as HWES
from statsmodels.tsa.exponential_smoothing.ets import ETSModel

from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.utils.dataframe import dataframe_to_rows

warnings.filterwarnings("ignore")


# ---------------------------------------------------------------------------
# Performance safety configuration
# ---------------------------------------------------------------------------

MAX_DEFAULT_WORKERS = 2


def configure_worker_runtime() -> None:
    """
    Keep each worker from using too many internal math-library threads.

    This prevents CPU oversubscription when multiple statsmodels fits run in
    parallel. It does not reduce model search space or change forecasting logic.
    """
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
    os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")


def resolve_safe_worker_count(num_tasks: int, requested_workers: Optional[int] = None) -> int:
    """
    Choose a conservative default worker count for large Excel inputs.

    ARIMA/SARIMAX/statsmodels can be memory-heavy. For hundreds of item columns,
    launching CPU-count-1 workers can make the machine appear hung due to RAM
    pressure and CPU oversubscription. This preserves accuracy because it only
    controls concurrency, not model logic.
    """
    if num_tasks <= 0:
        return 1

    cpu_count = os.cpu_count() or 2

    if requested_workers and requested_workers > 0:
        return max(1, min(int(requested_workers), num_tasks, cpu_count))

    safe_cpu_half = max(1, cpu_count // 2)
    return max(1, min(num_tasks, MAX_DEFAULT_WORKERS, safe_cpu_half))


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class CategoryTask:
    """Serializable task object passed to a parallel worker."""
    category: str
    bucketed_df: pd.DataFrame


@dataclass
class CategoryResult:
    """Serializable result object returned by a parallel worker."""
    category: str
    processed_df: pd.DataFrame
    forecast_df: Optional[pd.DataFrame]
    cfd_table: Optional[pd.DataFrame]
    model_summary: pd.DataFrame
    status: str
    error_message: Optional[str] = None


# ---------------------------------------------------------------------------
# General utility functions
# ---------------------------------------------------------------------------

def safe_sheet_name(name: str) -> str:
    """Convert a string into a valid Excel worksheet name."""
    cleaned = re.sub(r"[\[\]\:\*\?\/\\]", "_", str(name)).strip()
    if not cleaned:
        cleaned = "Sheet"
    return cleaned[:31]


def safe_file_stem(name: str) -> str:
    """Convert a category name into a safe filename stem."""
    cleaned = re.sub(r"[^A-Za-z0-9_.\-]+", "_", str(name)).strip("_")
    return cleaned or "category"


def normalize_text(value: Any) -> str:
    """Normalize text for robust header and label matching."""
    if value is None:
        return ""
    return " ".join(str(value).strip().lower().split())


def parse_flexible_date(value: Any) -> pd.Timestamp:
    """Safely parse Excel, string, or datetime values into pandas Timestamp."""
    if pd.isna(value):
        return pd.NaT
    try:
        return pd.to_datetime(value, errors="coerce")
    except Exception:
        return pd.NaT


def get_next_output_dir(base: "str | Path" = "forecastoutput_") -> Path:
    """
    Create a unique output directory path.

    If base includes a parent directory, the output folder is created there.
    """
    base_path = Path(base)
    parent = base_path.parent if str(base_path.parent) not in ("", ".") else Path.cwd()
    prefix = base_path.name

    count = 1
    while True:
        candidate = parent / f"{prefix}{count}"
        if not candidate.exists():
            return candidate.resolve()
        count += 1


# ---------------------------------------------------------------------------
# Input loading and standardization
# ---------------------------------------------------------------------------

def load_and_standardize_input_workbook(
    file_path: "str | Path",
) -> Tuple[pd.DataFrame, Dict[str, int], List[str], List[str]]:
    """
    Load the input workbook once and standardize the layout.

    Returns
    -------
    data:
        Cleaned DataFrame with a date column named 'Item' and one numeric column
        per demand category.
    valid_bucket_sizes:
        Dictionary mapping category column name to bucket size in days.
    invalid_bucket_columns:
        Columns skipped because the bucket-size row was invalid.
    negative_value_columns:
        Columns where negative demand values were found and reset to zero.
    """
    raw = pd.read_excel(file_path, dtype=object)
    raw.columns = [str(col).strip() if col is not None else "" for col in raw.columns]

    if raw.empty or raw.shape[1] < 2:
        raise ValueError("Input Excel sheet must contain at least one date column and one demand/category column.")

    first_col_name = raw.columns[0]
    first_col_series = raw.iloc[:, 0].astype(str).map(normalize_text)

    bucket_aliases = {
        "bucket days", "bucket day", "bucket size", "bucket sizes", "bucket", "days"
    }
    date_aliases = {
        "item", "item code", "date", "dates",
        "transaction date", "transaction dates",
        "trasaction date", "trasaction dates"
    }

    bucket_row_idx = None
    date_label_row_idx = None

    scan_limit = min(10, len(raw))
    for idx in range(scan_limit):
        label = first_col_series.iloc[idx]
        if bucket_row_idx is None and label in bucket_aliases:
            bucket_row_idx = idx
        if date_label_row_idx is None and label in date_aliases:
            date_label_row_idx = idx

    # Fallback: if no explicit bucket row is found, assume the first row below headers stores bucket sizes.
    if bucket_row_idx is None:
        bucket_row_idx = 0

    data_start_idx = bucket_row_idx + 1
    if date_label_row_idx is not None:
        data_start_idx = max(data_start_idx, date_label_row_idx + 1)

    bucket_size_series = pd.to_numeric(raw.iloc[bucket_row_idx, 1:], errors="coerce")
    valid_bucket_sizes: Dict[str, int] = {}
    invalid_bucket_columns: List[str] = []

    for column_name, bucket_value in bucket_size_series.items():
        clean_name = str(column_name).strip()
        if pd.isna(bucket_value):
            invalid_bucket_columns.append(clean_name)
            continue

        bucket_value = int(bucket_value)
        if bucket_value <= 0:
            invalid_bucket_columns.append(clean_name)
            continue

        valid_bucket_sizes[clean_name] = bucket_value

    if not valid_bucket_sizes:
        raise ValueError("No valid bucket sizes were found. The Bucket Days row must contain positive whole numbers.")

    data = raw.iloc[data_start_idx:].copy().reset_index(drop=True)
    data.rename(columns={first_col_name: "Item"}, inplace=True)
    data.columns = [str(col).strip() if col is not None else "" for col in data.columns]

    selected_columns = ["Item"] + [col for col in valid_bucket_sizes if col in data.columns]
    data = data[selected_columns]

    data["Item"] = data["Item"].apply(parse_flexible_date)
    data = data.dropna(subset=["Item"]).copy()

    if data.empty:
        raise ValueError("No valid transaction/date rows were found after cleaning the sheet.")

    data["Item"] = pd.to_datetime(data["Item"], errors="coerce")
    data = data.dropna(subset=["Item"]).sort_values("Item").reset_index(drop=True)

    negative_value_columns: List[str] = []
    for col in data.columns[1:]:
        data[col] = pd.to_numeric(data[col], errors="coerce").fillna(0.0)
        if (data[col] < 0).any():
            negative_value_columns.append(col)
            data.loc[data[col] < 0, col] = 0.0

    return data, valid_bucket_sizes, invalid_bucket_columns, negative_value_columns


# ---------------------------------------------------------------------------
# Bucketing, outlier correction, and demand classification
# ---------------------------------------------------------------------------

def build_bucketed_dataframe(data: pd.DataFrame, category: str, bucket_size: int) -> pd.DataFrame:
    """
    Build category-level bucketed demand using vectorized grouping.

    - Start from the minimum transaction date.
    - Use non-overlapping buckets of bucket_size days.
    - Drop the final incomplete bucket, matching the original script behavior.
    """
    category_df = data[["Item", category]].copy()
    category_df = category_df.dropna(subset=["Item"])

    if category_df.empty:
        return pd.DataFrame(columns=["Row Labels", "Sum of Sales"])

    start_date = category_df["Item"].min().normalize()
    end_date = category_df["Item"].max().normalize()

    day_offsets = (category_df["Item"].dt.normalize() - start_date).dt.days
    category_df["_bucket_index"] = (day_offsets // bucket_size).astype(int)

    grouped = category_df.groupby("_bucket_index", as_index=False)[category].sum()
    grouped.rename(columns={category: "Sum of Sales"}, inplace=True)

    grouped["bucket_start"] = grouped["_bucket_index"].apply(
        lambda i: start_date + pd.Timedelta(days=int(i) * bucket_size)
    )
    grouped["bucket_end"] = grouped["bucket_start"] + pd.to_timedelta(bucket_size - 1, unit="D")
    grouped = grouped[grouped["bucket_end"] <= end_date].copy()

    grouped["Row Labels"] = (
        grouped["bucket_start"].dt.strftime("%d-%m-%Y")
        + " - "
        + grouped["bucket_end"].dt.strftime("%d-%m-%Y")
    )

    return grouped[["Row Labels", "Sum of Sales"]].reset_index(drop=True)


def apply_outlier_and_classification(bucketed_df: pd.DataFrame) -> pd.DataFrame:
    """
    Add quartiles, IQR limits, outlier-corrected demand, CV2, ADI,
    demand classification, and sequence.
    """
    df = bucketed_df.copy()

    if df.empty:
        for col in [
            "1st quartile", "3rd quartile", "IQR", "lower limit", "higher limit",
            "median", "Outlier", "CV2", "Non-zero Demand Days", "ADI",
            "Classification", "Sequence"
        ]:
            df[col] = []
        return df

    sales_data = pd.to_numeric(df["Sum of Sales"], errors="coerce").fillna(0.0).to_numpy()

    q1 = float(np.percentile(sales_data, 25, method="midpoint"))
    q3 = float(np.percentile(sales_data, 75, method="midpoint"))
    iqr_value = q3 - q1
    lower_limit = q1 - 1.5 * iqr_value
    higher_limit = q3 + 1.5 * iqr_value
    median_value = float(np.median(sales_data))

    df["1st quartile"] = q1
    df["3rd quartile"] = q3
    df["IQR"] = iqr_value
    df["lower limit"] = lower_limit
    df["higher limit"] = higher_limit
    df["median"] = median_value

    # Preserve original logic:
    # If Q1 and Q3 are both zero, keep the original value.
    # Else replace out-of-limit values with median.
    if q1 == 0 and q3 == 0:
        df["Outlier"] = df["Sum of Sales"]
    else:
        df["Outlier"] = np.where(
            (df["Sum of Sales"] >= lower_limit) & (df["Sum of Sales"] <= higher_limit),
            df["Sum of Sales"],
            median_value,
        )

    non_zero_values = df.loc[df["Outlier"] != 0, "Outlier"].astype(float)

    if len(non_zero_values) == 0:
        cv2 = np.nan
        adi = np.inf
        non_zero_days = 0
    else:
        mean_value = non_zero_values.mean()
        if mean_value == 0 or np.isnan(mean_value):
            cv2 = np.nan
        else:
            cv2 = float((non_zero_values.std(ddof=1) / mean_value) ** 2) if len(non_zero_values) > 1 else 0.0
        non_zero_days = int(df["Outlier"].ne(0).sum())
        adi = float(len(df) / non_zero_days) if non_zero_days > 0 else np.inf

    df["CV2"] = cv2
    df["Non-zero Demand Days"] = non_zero_days
    df["ADI"] = adi

    conditions = [
        (df["ADI"] < 1.32) & (df["CV2"] < 0.49),
        (df["ADI"] < 1.32) & (df["CV2"] > 0.49),
        (df["ADI"] > 1.32) & (df["CV2"] < 0.49),
        (df["ADI"] > 1.32) & (df["CV2"] > 0.49),
    ]
    choices = ["Smooth", "Erratic", "Intermittent", "Lumpy"]

    df["Classification"] = np.select(conditions, choices, default="Unclassified")
    df["Sequence"] = range(1, len(df) + 1)

    return df


# ---------------------------------------------------------------------------
# Original full-search optimization functions
# ---------------------------------------------------------------------------

def optimize_arima(
    data: pd.Series, p_range=range(0, 4), d_range=range(0, 3), q_range=range(0, 4)
) -> Tuple[Tuple[int, int, int], Optional[float]]:
    """Find optimal ARIMA parameters using AIC (search p:0..3, d:0..2, q:0..3)."""
    best_aic = float("inf")
    best_order = None

    for p, d, q in product(p_range, d_range, q_range):
        try:
            model = ARIMA(data, order=(p, d, q))
            fitted = model.fit()
            if fitted.aic < best_aic:
                best_aic = float(fitted.aic)
                best_order = (p, d, q)
        except Exception:
            continue

    if best_order is None:
        best_order = (1, 1, 1)
        return best_order, None

    return best_order, best_aic


def optimize_sarimax(
    data: pd.Series,
    exog: Optional[pd.DataFrame] = None,
    seasonal_period: Optional[int] = None,
) -> Tuple[Tuple[int, int, int], Optional[Tuple[int, int, int, int]], Optional[float]]:
    """Find optimal SARIMAX parameters using the original search ranges."""
    best_aic = float("inf")
    best_order = None
    best_seasonal = None

    p_range = range(0, 3)
    d_range = range(0, 2)
    q_range = range(0, 3)

    for p, d, q in product(p_range, d_range, q_range):
        try:
            if seasonal_period:
                for P, D, Q in product(range(0, 2), range(0, 2), range(0, 2)):
                    model = SARIMAX(
                        data,
                        exog=exog,
                        order=(p, d, q),
                        seasonal_order=(P, D, Q, seasonal_period),
                    )
                    fitted = model.fit(disp=False)
                    if fitted.aic < best_aic:
                        best_aic = float(fitted.aic)
                        best_order = (p, d, q)
                        best_seasonal = (P, D, Q, seasonal_period)
            else:
                model = SARIMAX(data, exog=exog, order=(p, d, q))
                fitted = model.fit(disp=False)
                if fitted.aic < best_aic:
                    best_aic = float(fitted.aic)
                    best_order = (p, d, q)
        except Exception:
            continue

    if best_order is None:
        best_order = (1, 1, 1)
        return best_order, best_seasonal, None

    return best_order, best_seasonal, best_aic


def optimize_ses(data: pd.Series) -> float:
    """Find optimal smoothing parameter for Simple Exponential Smoothing."""
    try:
        model = SimpleExpSmoothing(data).fit(optimized=True)
        return float(model.params["smoothing_level"])
    except Exception:
        return 0.5


def optimize_holt(data: pd.Series) -> Tuple[float, float]:
    """Find optimal smoothing and trend parameters for Holt's method."""
    try:
        model = Holt(data).fit(optimized=True)
        return float(model.params["smoothing_level"]), float(model.params["smoothing_trend"])
    except Exception:
        return 0.5, 0.5


def optimize_hwes(data: pd.Series) -> Tuple[Dict[str, Any], Optional[float]]:
    """Auto-optimize Holt-Winters parameters using the original options."""
    best_aic = float("inf")
    best_config = None

    trend_options = ["add", None]
    seasonal_options = [None]
    damped_options = [True, False]

    for trend, seasonal, damped in product(trend_options, seasonal_options, damped_options):
        if trend is None and damped:
            continue
        try:
            model = HWES(
                data,
                trend=trend,
                seasonal=seasonal,
                damped_trend=damped if trend else False,
            )
            fitted = model.fit(optimized=True)
            if fitted.aic < best_aic:
                best_aic = float(fitted.aic)
                best_config = {"trend": trend, "seasonal": seasonal, "damped": damped}
        except Exception:
            continue

    if best_config is None:
        best_config = {"trend": "add", "seasonal": None, "damped": False}
        return best_config, None

    return best_config, best_aic


def optimize_ets(data: pd.Series) -> Tuple[Dict[str, Any], Optional[float]]:
    """Find optimal ETS configuration using the original options."""
    best_aic = float("inf")
    best_config = None

    error_options = ["add"]
    trend_options = ["add", None]
    seasonal_options = [None]
    damped_options = [True, False]

    for error, trend, seasonal, damped in product(error_options, trend_options, seasonal_options, damped_options):
        if trend is None and damped:
            continue
        try:
            model = ETSModel(
                data,
                error=error,
                trend=trend,
                seasonal=seasonal,
                damped_trend=damped if trend else False,
            )
            fitted = model.fit()
            if fitted.aic < best_aic:
                best_aic = float(fitted.aic)
                best_config = {
                    "error": error,
                    "trend": trend,
                    "seasonal": seasonal,
                    "damped": damped,
                }
        except Exception:
            continue

    if best_config is None:
        best_config = {"error": "add", "trend": "add", "seasonal": None, "damped": True}
        return best_config, None

    return best_config, best_aic


def optimize_moving_average(train_data: pd.Series, max_window: int = 12) -> int:
    """Find optimal moving-average window size using in-sample validation MAE."""
    best_mae = float("inf")
    best_window = 3

    min_window = 2
    max_test_window = min(len(train_data) // 2, max_window)

    for window in range(min_window, max_test_window + 1):
        try:
            predictions = []
            actuals = []
            validation_size = min(5, len(train_data) - window)

            for i in range(len(train_data) - validation_size, len(train_data)):
                if i >= window:
                    pred = np.mean(train_data.iloc[i - window:i])
                    predictions.append(pred)
                    actuals.append(train_data.iloc[i])

            if predictions:
                mae = mean_absolute_error(actuals, predictions)
                if mae < best_mae:
                    best_mae = float(mae)
                    best_window = window
        except Exception:
            continue

    return best_window


def optimize_all_models(train_data: pd.Series, test_data: pd.Series) -> Dict[str, Dict[str, Any]]:
    """Optimize all model families using the original full search spaces."""
    optimized_params: Dict[str, Dict[str, Any]] = {}

    try:
        arima_order, arima_aic = optimize_arima(train_data)
        optimized_params["ARIMA"] = {"order": arima_order, "aic": arima_aic}
    except Exception:
        optimized_params["ARIMA"] = {"order": (1, 1, 1), "aic": None}

    try:
        sarimax_order, sarimax_seasonal, sarimax_aic = optimize_sarimax(train_data)
        optimized_params["SARIMAX"] = {
            "order": sarimax_order,
            "seasonal": sarimax_seasonal,
            "aic": sarimax_aic,
        }
    except Exception:
        optimized_params["SARIMAX"] = {"order": (1, 1, 1), "seasonal": None, "aic": None}

    try:
        ses_alpha = optimize_ses(train_data)
        optimized_params["SES"] = {"smoothing_level": ses_alpha}
    except Exception:
        optimized_params["SES"] = {"smoothing_level": 0.5}

    try:
        holt_alpha, holt_beta = optimize_holt(train_data)
        optimized_params["Holt"] = {"alpha": holt_alpha, "beta": holt_beta}
    except Exception:
        optimized_params["Holt"] = {"alpha": 0.5, "beta": 0.5}

    try:
        hwes_config, hwes_aic = optimize_hwes(train_data)
        optimized_params["HWES"] = {**hwes_config, "aic": hwes_aic}
    except Exception:
        optimized_params["HWES"] = {"trend": "add", "seasonal": None, "damped": False, "aic": None}

    try:
        ets_config, ets_aic = optimize_ets(train_data)
        optimized_params["ETS"] = {**ets_config, "aic": ets_aic}
    except Exception:
        optimized_params["ETS"] = {"error": "add", "trend": "add", "seasonal": None, "damped": True, "aic": None}

    try:
        ma_window = optimize_moving_average(train_data)
        optimized_params["Moving Average"] = {"window": ma_window}
    except Exception:
        optimized_params["Moving Average"] = {"window": 3}

    return optimized_params


# ---------------------------------------------------------------------------
# Forecast functions
# ---------------------------------------------------------------------------

def forecast_to_array(forecast: "np.ndarray | pd.Series") -> np.ndarray:
    """Convert a model forecast into a plain NumPy array (drops pandas index)."""
    if isinstance(forecast, pd.Series):
        return forecast.to_numpy(dtype=float)
    return np.asarray(forecast, dtype=float)


def rounded_forecast_array(forecast: "np.ndarray | pd.Series") -> np.ndarray:
    """Return the same rounded (whole-number) forecast values shown in Excel."""
    forecast_array = forecast_to_array(forecast)
    return np.array([round(float(value)) for value in forecast_array], dtype=float)


def calculate_errors(actual: np.ndarray, forecast: "np.ndarray | pd.Series") -> np.ndarray:
    """Calculate forecast errors as Actual - Rounded Forecast (auditable in Excel)."""
    actual_array = np.asarray(actual, dtype=float)
    forecast_array = rounded_forecast_array(forecast)
    return actual_array - forecast_array


def fit_and_forecast_arima(train_data: pd.Series, forecast_steps: int, order: Tuple[int, int, int]) -> Optional[pd.Series]:
    """Fit ARIMA with selected order and forecast future periods."""
    try:
        model = ARIMA(train_data, order=order)
        fitted_model = model.fit()
        return fitted_model.forecast(steps=forecast_steps)
    except Exception:
        return None


def fit_and_forecast_sarimax(
    train_data: pd.Series,
    forecast_steps: int,
    order: Tuple[int, int, int],
    seasonal_order: Optional[Tuple[int, int, int, int]] = None,
    exog: Optional[pd.DataFrame] = None,
) -> Optional[pd.Series]:
    """Fit SARIMAX with selected order and forecast future periods."""
    try:
        if seasonal_order:
            model = SARIMAX(train_data, order=order, seasonal_order=seasonal_order, exog=exog)
        else:
            model = SARIMAX(train_data, order=order, exog=exog)
        fitted_model = model.fit(disp=False)
        return fitted_model.forecast(steps=forecast_steps, exog=exog)
    except Exception:
        return None


def fit_and_forecast_ses(train_data: pd.Series, forecast_steps: int, smoothing_level: float) -> Optional[pd.Series]:
    """Fit Simple Exponential Smoothing and forecast future periods."""
    try:
        model = SimpleExpSmoothing(train_data)
        fitted_model = model.fit(smoothing_level=smoothing_level, optimized=False)
        return fitted_model.forecast(steps=forecast_steps)
    except Exception:
        return None


def fit_and_forecast_holt(train_data: pd.Series, forecast_steps: int, alpha: float, beta: float) -> Optional[pd.Series]:
    """Fit Holt model and forecast future periods."""
    try:
        model = Holt(train_data)
        fitted_model = model.fit(smoothing_level=alpha, smoothing_trend=beta, optimized=False)
        return fitted_model.forecast(steps=forecast_steps)
    except Exception:
        return None


def fit_and_forecast_hwes(train_data: pd.Series, forecast_steps: int, config: Dict[str, Any]) -> Optional[pd.Series]:
    """Fit Holt-Winters model and forecast future periods."""
    try:
        model = HWES(
            train_data,
            trend=config.get("trend"),
            seasonal=config.get("seasonal"),
            damped_trend=config.get("damped", False) if config.get("trend") else False,
        )
        fitted_model = model.fit(optimized=True)
        return fitted_model.forecast(steps=forecast_steps)
    except Exception:
        return None


def fit_and_forecast_ets(train_data: pd.Series, forecast_steps: int, config: Dict[str, Any]) -> Optional[pd.Series]:
    """Fit ETS model and forecast future periods."""
    try:
        model = ETSModel(
            train_data,
            error=config.get("error", "add"),
            trend=config.get("trend"),
            seasonal=config.get("seasonal"),
            damped_trend=config.get("damped", False) if config.get("trend") else False,
        )
        fitted_model = model.fit()
        return fitted_model.forecast(steps=forecast_steps)
    except Exception:
        return None


def moving_average_forecast(train_data: pd.Series, forecast_steps: int, window: int) -> Optional[np.ndarray]:
    """Forecast using the last rolling average."""
    try:
        if len(train_data) < window:
            window = max(1, len(train_data))
        ma_values = train_data.rolling(window=window).mean()
        last_ma = ma_values.iloc[-1]
        if pd.isna(last_ma):
            last_ma = train_data.mean()
        return np.full(forecast_steps, last_ma)
    except Exception:
        return None


def linear_regression_forecast(train_data: pd.Series, forecast_steps: int) -> Optional[np.ndarray]:
    """Forecast using linear regression over sequence number."""
    try:
        X = np.arange(len(train_data)).reshape(-1, 1)
        y = train_data.values
        model = LinearRegression().fit(X, y)
        future_X = np.arange(len(train_data), len(train_data) + forecast_steps).reshape(-1, 1)
        return model.predict(future_X)
    except Exception:
        return None


def forecast_with_best_model(
    complete_data: pd.Series,
    forecast_steps: int,
    best_model_name: str,
    optimized_params: Dict[str, Dict[str, Any]],
) -> "Optional[np.ndarray | pd.Series]":
    """Generate future forecast using the selected best model on complete data."""
    if best_model_name == "ARIMA":
        return fit_and_forecast_arima(complete_data, forecast_steps, optimized_params["ARIMA"]["order"])

    if best_model_name == "SARIMAX":
        return fit_and_forecast_sarimax(
            complete_data,
            forecast_steps,
            optimized_params["SARIMAX"]["order"],
            optimized_params["SARIMAX"].get("seasonal"),
        )

    if best_model_name == "SES":
        return fit_and_forecast_ses(complete_data, forecast_steps, optimized_params["SES"]["smoothing_level"])

    if best_model_name == "Holt":
        return fit_and_forecast_holt(
            complete_data,
            forecast_steps,
            optimized_params["Holt"]["alpha"],
            optimized_params["Holt"]["beta"],
        )

    if best_model_name == "HWES":
        config = {k: v for k, v in optimized_params["HWES"].items() if k != "aic"}
        return fit_and_forecast_hwes(complete_data, forecast_steps, config)

    if best_model_name == "ETS":
        config = {k: v for k, v in optimized_params["ETS"].items() if k != "aic"}
        return fit_and_forecast_ets(complete_data, forecast_steps, config)

    if best_model_name == "Moving Average":
        return moving_average_forecast(complete_data, forecast_steps, optimized_params["Moving Average"]["window"])

    return linear_regression_forecast(complete_data, forecast_steps)


# ---------------------------------------------------------------------------
# Category processing worker
# ---------------------------------------------------------------------------

def process_category_task(task: CategoryTask) -> CategoryResult:
    """
    Process one category. Independent and serializable so it can run safely
    inside a multiprocessing worker.
    """
    configure_worker_runtime()
    category = task.category

    try:
        processed_df = apply_outlier_and_classification(task.bucketed_df)

        if processed_df.empty:
            return CategoryResult(
                category=category,
                processed_df=processed_df,
                forecast_df=None,
                cfd_table=None,
                model_summary=pd.DataFrame([{"Metric": "Status", "Value": "No data"}]),
                status="NO_DATA",
            )

        classification = str(processed_df["Classification"].iloc[0])
        model_summary_rows: List[Dict[str, Any]] = [
            {"Metric": "Category", "Value": category},
            {"Metric": "Classification", "Value": classification},
            {"Metric": "Periods", "Value": len(processed_df)},
            {"Metric": "Non-zero Demand Days", "Value": processed_df["Non-zero Demand Days"].iloc[0]},
            {"Metric": "ADI", "Value": processed_df["ADI"].iloc[0]},
            {"Metric": "CV2", "Value": processed_df["CV2"].iloc[0]},
        ]

        if classification == "Smooth" and len(processed_df) >= 6 and processed_df["Outlier"].dropna().shape[0] >= 6:
            train_size = len(processed_df) - 3
            train_data = processed_df["Outlier"].iloc[:train_size].astype(float).reset_index(drop=True)
            test_data = processed_df["Outlier"].iloc[train_size:].astype(float).reset_index(drop=True)
            forecast_steps = len(test_data)

            optimized_params = optimize_all_models(train_data, test_data)
            results: Dict[str, Dict[str, Any]] = {}

            # ARIMA
            arima_forecast = fit_and_forecast_arima(train_data, forecast_steps, optimized_params["ARIMA"]["order"])
            if arima_forecast is not None:
                errors = calculate_errors(test_data.values, arima_forecast)
                results["ARIMA"] = {"forecast": arima_forecast, "mae": float(np.mean(np.abs(errors))), "errors": errors}

            # SARIMAX
            sarimax_forecast = fit_and_forecast_sarimax(
                train_data, forecast_steps,
                optimized_params["SARIMAX"]["order"], optimized_params["SARIMAX"].get("seasonal"),
            )
            if sarimax_forecast is not None:
                errors = calculate_errors(test_data.values, sarimax_forecast)
                results["SARIMAX"] = {"forecast": sarimax_forecast, "mae": float(np.mean(np.abs(errors))), "errors": errors}

            # SES
            ses_forecast = fit_and_forecast_ses(train_data, forecast_steps, optimized_params["SES"]["smoothing_level"])
            if ses_forecast is not None:
                errors = calculate_errors(test_data.values, ses_forecast)
                results["SES"] = {"forecast": ses_forecast, "mae": float(np.mean(np.abs(errors))), "errors": errors}

            # Holt
            holt_forecast = fit_and_forecast_holt(
                train_data, forecast_steps, optimized_params["Holt"]["alpha"], optimized_params["Holt"]["beta"],
            )
            if holt_forecast is not None:
                errors = calculate_errors(test_data.values, holt_forecast)
                results["Holt"] = {"forecast": holt_forecast, "mae": float(np.mean(np.abs(errors))), "errors": errors}

            # HWES
            hwes_config = {k: v for k, v in optimized_params["HWES"].items() if k != "aic"}
            hwes_forecast = fit_and_forecast_hwes(train_data, forecast_steps, hwes_config)
            if hwes_forecast is not None:
                errors = calculate_errors(test_data.values, hwes_forecast)
                results["HWES"] = {"forecast": hwes_forecast, "mae": float(np.mean(np.abs(errors))), "errors": errors}

            # ETS
            ets_config = {k: v for k, v in optimized_params["ETS"].items() if k != "aic"}
            ets_forecast = fit_and_forecast_ets(train_data, forecast_steps, ets_config)
            if ets_forecast is not None:
                errors = calculate_errors(test_data.values, ets_forecast)
                results["ETS"] = {"forecast": ets_forecast, "mae": float(np.mean(np.abs(errors))), "errors": errors}

            # Moving Average
            ma_forecast = moving_average_forecast(train_data, forecast_steps, optimized_params["Moving Average"]["window"])
            if ma_forecast is not None:
                errors = calculate_errors(test_data.values, ma_forecast)
                results["Moving Average"] = {"forecast": ma_forecast, "mae": float(np.mean(np.abs(errors))), "errors": errors}

            # Linear Regression
            lr_forecast = linear_regression_forecast(train_data, forecast_steps)
            if lr_forecast is not None:
                errors = calculate_errors(test_data.values, lr_forecast)
                results["Linear Regression"] = {"forecast": lr_forecast, "mae": float(np.mean(np.abs(errors))), "errors": errors}

            if results:
                best_model_name = min(results.keys(), key=lambda k: results[k]["mae"])
                best_mae = results[best_model_name]["mae"]
            else:
                best_model_name = "Linear Regression"
                best_mae = None

            forecast_df = pd.DataFrame({
                "Period": range(train_size + 1, train_size + forecast_steps + 1),
                "Actual": test_data.values,
            })

            for model_name, result in results.items():
                forecast_values = result["forecast"]
                forecast_array = rounded_forecast_array(forecast_values)
                forecast_df[f"{model_name} Forecast"] = forecast_array.astype(int)
                forecast_df[f"{model_name} Error"] = result["errors"]

            complete_data = processed_df["Outlier"].astype(float).reset_index(drop=True)
            complete_forecast = forecast_with_best_model(complete_data, forecast_steps, best_model_name, optimized_params)

            if complete_forecast is None:
                complete_forecast = linear_regression_forecast(complete_data, forecast_steps)
                best_model_name = "Linear Regression"

            forecast_values = rounded_forecast_array(complete_forecast)

            forecast_df["Best Forecast Model"] = best_model_name
            forecast_df["Best Complete Forecast"] = forecast_values.astype(int)

            for model_name, result in results.items():
                model_summary_rows.append({"Metric": f"{model_name} MAE", "Value": result["mae"]})
            model_summary_rows.append({"Metric": "Best Forecast Model", "Value": best_model_name})
            model_summary_rows.append({"Metric": "Best Model MAE", "Value": best_mae})

            for model_name, params in optimized_params.items():
                model_summary_rows.append({"Metric": f"{model_name} Params", "Value": str(params)})

            return CategoryResult(
                category=category,
                processed_df=processed_df,
                forecast_df=forecast_df,
                cfd_table=None,
                model_summary=pd.DataFrame(model_summary_rows),
                status="FORECASTED",
            )

        # Non-smooth logic: create cumulative frequency table.
        values = processed_df["Outlier"].astype(float)
        frequency = values.value_counts().sort_index()
        cumulative_frequency = frequency.cumsum()
        total_frequency = frequency.sum()

        if total_frequency == 0:
            percentage_cumulative_frequency = pd.Series([0] * len(cumulative_frequency), index=cumulative_frequency.index)
        else:
            percentage_cumulative_frequency = (100 * cumulative_frequency / total_frequency).round().astype(int)

        cfd_table = pd.DataFrame({
            "Outlier": cumulative_frequency.index,
            "Cumulative Frequency": percentage_cumulative_frequency.values,
        })

        return CategoryResult(
            category=category,
            processed_df=processed_df,
            forecast_df=None,
            cfd_table=cfd_table,
            model_summary=pd.DataFrame(model_summary_rows),
            status="CFD_CREATED",
        )

    except Exception as e:
        return CategoryResult(
            category=category,
            processed_df=pd.DataFrame(),
            forecast_df=None,
            cfd_table=None,
            model_summary=pd.DataFrame([{"Metric": "Error", "Value": str(e)}]),
            status="ERROR",
            error_message=traceback.format_exc(),
        )


# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------

def run_parallel_processing(tasks: List[CategoryTask], workers: Optional[int] = None) -> List[CategoryResult]:
    """
    Run category processing with a sequential fallback.

    - Each worker handles one category at a time.
    - Workers do not write Excel files; writing happens in the main process.
    - If the process pool fails, tasks are retried sequentially.
    """
    if not tasks:
        return []

    if workers is None or workers <= 0:
        workers = resolve_safe_worker_count(len(tasks), workers)

    # For one category, or when forced sequential, avoid multiprocessing overhead.
    if workers == 1 or len(tasks) == 1:
        return [process_category_task(task) for task in tasks]

    results: List[CategoryResult] = []

    try:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            future_to_category = {
                executor.submit(process_category_task, task): task.category
                for task in tasks
            }

            for future in as_completed(future_to_category):
                category = future_to_category[future]
                try:
                    results.append(future.result())
                except Exception as e:
                    results.append(
                        CategoryResult(
                            category=category, processed_df=pd.DataFrame(), forecast_df=None,
                            cfd_table=None, model_summary=pd.DataFrame([{"Metric": "Error", "Value": str(e)}]),
                            status="ERROR", error_message=traceback.format_exc(),
                        )
                    )

        results.sort(key=lambda r: r.category)
        return results

    except Exception:
        fallback_results: List[CategoryResult] = []
        for task in tasks:
            try:
                fallback_results.append(process_category_task(task))
            except Exception:
                fallback_results.append(
                    CategoryResult(
                        category=task.category, processed_df=pd.DataFrame(), forecast_df=None,
                        cfd_table=None,
                        model_summary=pd.DataFrame([{"Metric": "Error", "Value": "Category failed during sequential fallback"}]),
                        status="ERROR", error_message=traceback.format_exc(),
                    )
                )
        fallback_results.sort(key=lambda r: r.category)
        return fallback_results


# ---------------------------------------------------------------------------
# Excel output functions
# ---------------------------------------------------------------------------

def autofit_worksheet(ws, max_width: int = 42) -> None:
    """Apply practical column widths and wrapped headers."""
    for column_cells in ws.columns:
        max_length = 0
        column_letter = get_column_letter(column_cells[0].column)
        for cell in column_cells:
            if cell.value is None:
                continue
            max_length = max(max_length, len(str(cell.value)))
        ws.column_dimensions[column_letter].width = min(max(max_length + 2, 10), max_width)

    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)


def style_header_row(ws, row_num: int = 1) -> None:
    """Apply professional header styling."""
    fill = PatternFill("solid", fgColor="1F4E78")
    font = Font(color="FFFFFF", bold=True)
    thin = Side(style="thin", color="D9E2F3")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for cell in ws[row_num]:
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border


def write_dataframe_to_sheet(wb: Workbook, sheet_name: str, df: pd.DataFrame) -> None:
    """Write a pandas DataFrame to an openpyxl worksheet."""
    ws = wb.create_sheet(title=safe_sheet_name(sheet_name))
    if df is None or df.empty:
        ws.append(["No data"])
        return

    for row in dataframe_to_rows(df, index=False, header=True):
        ws.append(row)

    style_header_row(ws, 1)
    ws.freeze_panes = "A2"
    autofit_worksheet(ws)


def add_cfd_chart(wb: Workbook, cfd_table: pd.DataFrame) -> None:
    """Add a cumulative-frequency line chart to the workbook."""
    if cfd_table is None or cfd_table.empty:
        return

    ws = wb.create_sheet(title="Cumulative Frequency Chart")
    ws.append(["Outlier", "Cumulative Frequency"])
    for _, row in cfd_table.iterrows():
        ws.append([row["Outlier"], row["Cumulative Frequency"]])

    style_header_row(ws, 1)

    chart = LineChart()
    chart.title = "Cumulative Frequency Distribution"
    chart.y_axis.title = "Cumulative Frequency (%)"
    chart.x_axis.title = "Outlier Value"
    chart.height = 12
    chart.width = 22

    data = Reference(ws, min_col=2, min_row=1, max_row=len(cfd_table) + 1)
    cats = Reference(ws, min_col=1, min_row=2, max_row=len(cfd_table) + 1)
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(cats)

    ws.add_chart(chart, "D2")
    autofit_worksheet(ws)


def save_category_workbook(result: CategoryResult, output_dir: Path) -> Path:
    """Save one category workbook (Processed Data, Forecasts/CFD, Model Summary)."""
    wb = Workbook()
    default_ws = wb.active
    wb.remove(default_ws)

    write_dataframe_to_sheet(wb, "Processed Data", result.processed_df)

    if result.forecast_df is not None:
        write_dataframe_to_sheet(wb, "Forecasts and Errors", result.forecast_df)

    if result.cfd_table is not None:
        write_dataframe_to_sheet(wb, "Cumulative Frequency Table", result.cfd_table)
        add_cfd_chart(wb, result.cfd_table)

    write_dataframe_to_sheet(wb, "Model Summary", result.model_summary)

    file_path = output_dir / f"{safe_file_stem(result.category)}.xlsx"
    wb.save(file_path)
    return file_path


def create_aggregated_forecasts(results: List[CategoryResult], output_dir: Path) -> Optional[Path]:
    """Create aggregated_forecasts.xlsx from Best Complete Forecast columns."""
    aggregated_data: Dict[str, pd.Series] = {}

    for result in results:
        if result.forecast_df is not None and "Best Complete Forecast" in result.forecast_df.columns:
            aggregated_data[result.category] = result.forecast_df["Best Complete Forecast"].reset_index(drop=True)

    if not aggregated_data:
        return None

    final_df = pd.DataFrame(aggregated_data)
    output_path = output_dir / "aggregated_forecasts.xlsx"
    final_df.to_excel(output_path, index=False)
    return output_path


def create_summary_cfd(results: List[CategoryResult], output_dir: Path, thresholds: Optional[List[int]] = None) -> Optional[Path]:
    """Create summary_cfd.xlsx using the requested cumulative-frequency thresholds."""
    if thresholds is None:
        thresholds = [90, 95, 99]

    summary_data = pd.DataFrame({"Threshold": thresholds}).set_index("Threshold")

    for result in results:
        if result.cfd_table is None or result.cfd_table.empty:
            continue

        outlier_values = []
        for threshold in thresholds:
            matching_rows = result.cfd_table[result.cfd_table["Cumulative Frequency"] >= threshold]
            outlier_value = matching_rows["Outlier"].iloc[0] if not matching_rows.empty else np.nan
            outlier_values.append(outlier_value)

        summary_data[result.category] = outlier_values

    summary_data.dropna(axis=1, how="all", inplace=True)

    if summary_data.empty or summary_data.shape[1] == 0:
        return None

    output_path = output_dir / "summary_cfd.xlsx"
    summary_data.to_excel(output_path)
    return output_path


def create_processing_summary(
    results: List[CategoryResult],
    output_dir: Path,
    invalid_bucket_columns: List[str],
    negative_value_columns: List[str],
    elapsed_seconds: float,
    workers: int,
) -> Path:
    """Create a processing summary workbook for auditability."""
    rows = []
    for result in results:
        rows.append({
            "Category": result.category,
            "Status": result.status,
            "Rows Processed": len(result.processed_df),
            "Has Forecast": result.forecast_df is not None,
            "Has CFD": result.cfd_table is not None,
            "Error": result.error_message if result.error_message else "",
        })

    summary_df = pd.DataFrame(rows)
    warnings_df = pd.DataFrame({
        "Warning Type": ["Invalid Bucket Columns", "Negative Values Reset To Zero", "Workers Used", "Elapsed Seconds"],
        "Details": [
            ", ".join(invalid_bucket_columns) if invalid_bucket_columns else "",
            ", ".join(negative_value_columns) if negative_value_columns else "",
            workers,
            round(elapsed_seconds, 2),
        ],
    })

    output_path = output_dir / "processing_summary.xlsx"
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Category Summary", index=False)
        warnings_df.to_excel(writer, sheet_name="Warnings", index=False)

    return output_path


# ---------------------------------------------------------------------------
# End-to-end processing
# ---------------------------------------------------------------------------

def process_workbook(
    input_path: "str | Path",
    output_dir: "Optional[str | Path]" = None,
    workers: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Main end-to-end processor.

    Returns a dictionary with output locations and processing summary.
    """
    start_time = datetime.now()

    input_path = Path(input_path).resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    if output_dir is None:
        output_dir_path = get_next_output_dir(input_path.parent / "forecastoutput_")
    else:
        output_dir_path = Path(output_dir).resolve()

    output_dir_path.mkdir(parents=True, exist_ok=True)

    if not output_dir_path.exists():
        raise RuntimeError(f"Output directory could not be created: {output_dir_path}")

    data, bucket_sizes, invalid_bucket_columns, negative_value_columns = load_and_standardize_input_workbook(input_path)

    tasks: List[CategoryTask] = []
    for category, bucket_size in bucket_sizes.items():
        if category not in data.columns:
            continue
        bucketed_df = build_bucketed_dataframe(data, category, bucket_size)
        tasks.append(CategoryTask(category=category, bucketed_df=bucketed_df))

    if not tasks:
        raise ValueError("No valid category tasks could be created from the input workbook.")

    resolved_workers = resolve_safe_worker_count(len(tasks), workers)

    results = run_parallel_processing(tasks, resolved_workers)

    category_files = []
    for result in results:
        category_files.append(save_category_workbook(result, output_dir_path))

    aggregated_forecasts_path = create_aggregated_forecasts(results, output_dir_path)
    summary_cfd_path = create_summary_cfd(results, output_dir_path)

    elapsed_seconds = (datetime.now() - start_time).total_seconds()
    processing_summary_path = create_processing_summary(
        results=results,
        output_dir=output_dir_path,
        invalid_bucket_columns=invalid_bucket_columns,
        negative_value_columns=negative_value_columns,
        elapsed_seconds=elapsed_seconds,
        workers=resolved_workers,
    )

    return {
        "output_dir": str(output_dir_path),
        "category_files": [str(path) for path in category_files],
        "aggregated_forecasts": str(aggregated_forecasts_path) if aggregated_forecasts_path else None,
        "summary_cfd": str(summary_cfd_path) if summary_cfd_path else None,
        "processing_summary": str(processing_summary_path),
        "categories_processed": len(results),
        "workers": resolved_workers,
        "elapsed_seconds": elapsed_seconds,
        "statuses": {result.category: result.status for result in results},
    }
