"""
Tests for agents/chart_type.py — one case per rule (LLD §4.5).
Run with:  python -m pytest tests/test_chart_type.py -v
       or: python tests/test_chart_type.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd

from agents.chart_type import classify, ChartConfig

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_DIVIDER = "-" * 60


def _header(title: str) -> None:
    print(f"\n{_DIVIDER}")
    print(f"  {title}")
    print(_DIVIDER)


def _check(condition: bool, label: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}")
    if not condition:
        raise AssertionError(label)


def _show(cfg: ChartConfig) -> None:
    print(f"  chart_type   : {cfg.chart_type}")
    print(f"  metric_col   : {cfg.metric_col}")
    print(f"  groupby_cols : {cfg.groupby_cols}")
    print(f"  x_axis_col   : {cfg.x_axis_col}")
    print(f"  number_format: {cfg.number_format}")
    if cfg.grain_adjusted:
        print(f"  grain        : {cfg.grain}  (adjusted)")


# ---------------------------------------------------------------------------
# Rule 1 — big number
# ---------------------------------------------------------------------------

def test_big_number() -> None:
    _header("Rule 1: big_number  (1 row, 1 numeric, 0 categorical)")
    df = pd.DataFrame({"total_conversations": [106_000]})
    cfg = classify(df, title="Total conversations")
    _show(cfg)
    _check(cfg.chart_type == "big_number", "chart_type == big_number")
    _check(cfg.metric_col == "total_conversations", "metric_col correct")
    _check(cfg.groupby_cols == [], "no groupby_cols")
    _check(cfg.number_format == "integer", "number_format integer (large whole number)")


# ---------------------------------------------------------------------------
# Rule 2 — line chart (time series)
# ---------------------------------------------------------------------------

def test_line_chart() -> None:
    _header("Rule 2: line  (temporal + numeric columns)")
    df = pd.DataFrame({
        "day": ["2026-06-01", "2026-06-02", "2026-06-03"],
        "total_conversations": [350, 420, 390],
    })
    cfg = classify(df, title="Daily volume")
    _show(cfg)
    _check(cfg.chart_type == "line", "chart_type == line")
    _check(cfg.x_axis_col == "day", "x_axis_col == day")
    _check(cfg.metric_col == "total_conversations", "metric_col correct")


# ---------------------------------------------------------------------------
# Rule 2b — line with grain adjustment (>1 year span)
# ---------------------------------------------------------------------------

def test_line_grain_adjusted() -> None:
    _header("Rule 2b: line + grain adjustment  (>365-day span)")
    dates = pd.date_range("2023-01-01", periods=400, freq="D").strftime("%Y-%m-%d").tolist()
    df = pd.DataFrame({
        "day": dates,
        "total_conversations": range(400),
    })
    cfg = classify(df, title="Multi-year trend")
    _show(cfg)
    _check(cfg.chart_type == "line", "chart_type == line")
    _check(cfg.grain_adjusted, "grain_adjusted == True")
    _check(cfg.grain == "month", "grain == month")


# ---------------------------------------------------------------------------
# Rule 3 — pie (<=5 unique categorical values)
# ---------------------------------------------------------------------------

def test_pie() -> None:
    _header("Rule 3: pie  (1 categorical, <=5 unique + 1 numeric)")
    df = pd.DataFrame({
        "execution_result": ["SUCCESS", "EXECUTION_FAILED", "EXECUTION_DEEPLINK_REQUESTED"],
        "total_conversations": [37050, 15900, 42400],
    })
    cfg = classify(df, title="Result breakdown")
    _show(cfg)
    _check(cfg.chart_type == "pie", "chart_type == pie")
    _check(cfg.groupby_cols == ["execution_result"], "groupby == execution_result")
    _check(cfg.metric_col == "total_conversations", "metric_col correct")


# ---------------------------------------------------------------------------
# Rule 4 — bar (>5 unique categorical values)
# ---------------------------------------------------------------------------

def test_bar() -> None:
    _header("Rule 4: bar  (1 categorical >5 unique + 1 numeric)")
    countries = ["India", "US", "Korea", "UK", "Germany", "Japan", "Brazil", "Australia"]
    df = pd.DataFrame({
        "country": countries,
        "total_conversations": [63600, 15900, 10600, 4240, 3180, 3180, 2120, 2120],
    })
    cfg = classify(df, title="Conversations by country")
    _show(cfg)
    _check(cfg.chart_type == "bar", "chart_type == bar")
    _check(cfg.groupby_cols == ["country"], "groupby == country")


# ---------------------------------------------------------------------------
# Rule 5 — stacked bar (2 categorical + 1 numeric)
# ---------------------------------------------------------------------------

def test_stacked_bar() -> None:
    _header("Rule 5: stacked_bar  (2 categorical + 1 numeric)")
    df = pd.DataFrame({
        "device_type": ["PHONE", "PHONE", "SPEAKER", "SPEAKER"],
        "execution_result": ["SUCCESS", "EXECUTION_FAILED", "SUCCESS", "EXECUTION_FAILED"],
        "total_conversations": [16498, 31404, 9454, 17084],
    })
    cfg = classify(df, title="Device × result")
    _show(cfg)
    _check(cfg.chart_type == "stacked_bar", "chart_type == stacked_bar")
    _check(len(cfg.groupby_cols) == 2, "2 groupby_cols")
    _check("device_type" in cfg.groupby_cols, "device_type in groupby")
    _check("execution_result" in cfg.groupby_cols, "execution_result in groupby")


# ---------------------------------------------------------------------------
# Rule 6 — fallback table
# ---------------------------------------------------------------------------

def test_fallback_table() -> None:
    _header("Rule 6: table  (fallback — 3 numeric columns)")
    df = pd.DataFrame({
        "total_conversations": [100, 200],
        "successful_conversations": [35, 70],
        "sum_kpi_completion": [74.3, 148.2],
    })
    cfg = classify(df, title="Raw stats")
    _show(cfg)
    _check(cfg.chart_type == "table", "chart_type == table")


# ---------------------------------------------------------------------------
# Number format inference
# ---------------------------------------------------------------------------

def test_number_format_rate_column() -> None:
    _header("Number format: percent for rate/kpi columns")
    df = pd.DataFrame({"success_rate": [0.35]})
    cfg = classify(df, title="Success rate")
    _show(cfg)
    _check(cfg.number_format == "percent_1", "number_format == percent_1 (value <1.01)")


def test_number_format_kpi_column() -> None:
    _header("Number format: decimal for kpi_completion column")
    df = pd.DataFrame({"avg_kpi_completion": [0.73]})
    cfg = classify(df, title="Avg KPI")
    _show(cfg)
    _check(cfg.number_format == "decimal_2", "number_format == decimal_2 (kpi in name)")


# ---------------------------------------------------------------------------
# Edge case — empty DataFrame
# ---------------------------------------------------------------------------

def test_empty_dataframe() -> None:
    _header("Edge case: empty DataFrame -> table")
    df = pd.DataFrame()
    cfg = classify(df, title="Nothing")
    _show(cfg)
    _check(cfg.chart_type == "table", "empty df -> table")
    _check(cfg.metric_col is None, "metric_col is None")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> None:
    print("\n=== Chart Type Agent rule tests ===")
    tests = [
        test_big_number,
        test_line_chart,
        test_line_grain_adjusted,
        test_pie,
        test_bar,
        test_stacked_bar,
        test_fallback_table,
        test_number_format_rate_column,
        test_number_format_kpi_column,
        test_empty_dataframe,
    ]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as exc:
            print(f"  FAILED: {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR : {exc}")
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"  Results: {passed} passed, {failed} failed")
    print("=" * 60)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
