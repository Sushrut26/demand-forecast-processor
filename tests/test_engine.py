"""End-to-end smoke test for the forecast engine."""

import os
from pathlib import Path

import pandas as pd

from app.forecast_engine import process_workbook
from tools.make_sample_workbook import build


def test_process_workbook_produces_expected_outputs(tmp_path):
    input_path = build(str(tmp_path / "sample_input.xlsx"))
    output_dir = tmp_path / "out"

    summary = process_workbook(input_path=input_path, output_dir=str(output_dir), workers=1)

    # Aggregate + summary files exist.
    assert Path(summary["processing_summary"]).exists()
    assert summary["categories_processed"] == 2

    # One workbook per category was written.
    xlsx_files = list(output_dir.glob("*.xlsx"))
    names = {f.name for f in xlsx_files}
    assert "processing_summary.xlsx" in names
    # The smooth product should generate a forecast; aggregated file present.
    assert summary["aggregated_forecasts"] is not None
    assert Path(summary["aggregated_forecasts"]).exists()

    # Statuses recorded per category.
    assert set(summary["statuses"].keys()) == {"Smooth_Product", "Intermittent_Product"}


def test_bucketing_is_deterministic(tmp_path):
    from app.forecast_engine import (
        build_bucketed_dataframe, load_and_standardize_input_workbook,
    )

    input_path = build(str(tmp_path / "s.xlsx"))
    data, buckets, _, _ = load_and_standardize_input_workbook(input_path)
    b1 = build_bucketed_dataframe(data, "Smooth_Product", buckets["Smooth_Product"])
    b2 = build_bucketed_dataframe(data, "Smooth_Product", buckets["Smooth_Product"])
    pd.testing.assert_frame_equal(b1, b2)
    assert not b1.empty
