"""
Generate a small sample input workbook for testing.

Layout matches what the engine expects:
    - Row 1: header (first cell 'Item', then one label per category column)
    - Row 2: 'Bucket Days' with a positive integer bucket size per category
    - Following rows: a date in column 1, then demand values per category

Usage:
    python tools/make_sample_workbook.py [output_path]
"""

import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd


def build(output_path: str = "sample/sample_input.xlsx") -> str:
    rng = np.random.default_rng(42)
    start = datetime(2024, 1, 1)
    n_days = 240
    dates = [start + timedelta(days=i) for i in range(n_days)]

    # A smooth-ish series and an intermittent (lumpy) series.
    smooth = np.clip(rng.normal(50, 8, n_days).round(), 0, None)
    intermittent = np.where(rng.random(n_days) < 0.25, rng.integers(1, 40, n_days), 0)

    header = ["Item", "Smooth_Product", "Intermittent_Product"]
    bucket_row = ["Bucket Days", 7, 7]

    rows = [header, bucket_row]
    for d, s, it in zip(dates, smooth, intermittent):
        rows.append([d, int(s), int(it)])

    df = pd.DataFrame(rows)

    import os
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    df.to_excel(output_path, index=False, header=False)
    return output_path


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "sample/sample_input.xlsx"
    path = build(out)
    print(f"Sample workbook written to: {path}")
