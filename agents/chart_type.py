"""
Chart Type Agent (LLD §4.5) — pure rule-based, no LLM.

Inspects a DataFrame's shape and column types, then selects a Superset-compatible
chart type plus the column role assignments the Dashboard Code Gen Agent needs.

Public API:
    classify(df, title="") -> ChartConfig
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------

ChartType = Literal[
    "big_number",
    "line",
    "pie",
    "bar",
    "stacked_bar",
    "table",
]

NumberFormat = Literal["integer", "decimal_2", "percent_1", "auto"]


@dataclass
class ChartConfig:
    chart_type: ChartType
    title: str
    metric_col: str | None          # primary numeric column
    groupby_cols: list[str]         # categorical dimension(s)
    x_axis_col: str | None          # time column for line charts
    number_format: NumberFormat
    # grain_adjusted is True when the time column was resampled (>1 year span)
    grain_adjusted: bool = False
    grain: Literal["day", "month", "quarter"] = "day"
    # raw column classification for downstream use
    numeric_cols: list[str] = field(default_factory=list)
    categorical_cols: list[str] = field(default_factory=list)
    temporal_cols: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Column classification helpers
# ---------------------------------------------------------------------------

_TEMPORAL_KEYWORDS = (
    "date", "day", "hour", "month", "week", "quarter", "year",
    "timestamp", "time", "period",
)


def _looks_temporal(col: str, series: pd.Series) -> bool:
    """True if the column name or values suggest a time dimension."""
    if any(kw in col.lower() for kw in _TEMPORAL_KEYWORDS):
        return True
    if pd.api.types.is_datetime64_any_dtype(series):
        return True
    if pd.api.types.is_object_dtype(series):
        sample = series.dropna().head(5).astype(str)
        for val in sample:
            # Match YYYY-MM-DD, YYYY-MM-DD HH:MM:SS, YYYY-MM-DD HH:00:00
            if len(val) >= 10 and val[:4].isdigit() and val[4] == "-":
                return True
    return False


def _classify_columns(
    df: pd.DataFrame,
) -> tuple[list[str], list[str], list[str]]:
    """Return (numeric_cols, categorical_cols, temporal_cols)."""
    numeric: list[str] = []
    categorical: list[str] = []
    temporal: list[str] = []

    for col in df.columns:
        series = df[col]
        if _looks_temporal(col, series):
            temporal.append(col)
        elif pd.api.types.is_numeric_dtype(series):
            numeric.append(col)
        else:
            categorical.append(col)

    return numeric, categorical, temporal


# ---------------------------------------------------------------------------
# Number format inference
# ---------------------------------------------------------------------------

def _infer_number_format(series: pd.Series) -> NumberFormat:
    """Pick a display format from the value range of a numeric column."""
    s = series.dropna()
    if s.empty:
        return "auto"
    maximum = s.abs().max()
    if col_name := getattr(series, "name", ""):
        lc = str(col_name).lower()
        if "rate" in lc or "pct" in lc or "percent" in lc:
            return "percent_1"
        if "kpi" in lc or "completion" in lc or "avg" in lc:
            return "decimal_2"
    if maximum < 1.01:
        return "percent_1"
    if maximum < 1000 and not (s % 1 == 0).all():
        return "decimal_2"
    return "integer"


# ---------------------------------------------------------------------------
# Grain adjustment
# ---------------------------------------------------------------------------

def _time_span_days(df: pd.DataFrame, temporal_col: str) -> float:
    """Estimate the date range in days of a temporal column."""
    try:
        parsed = pd.to_datetime(df[temporal_col], errors="coerce").dropna()
        if len(parsed) < 2:
            return 0.0
        return float((parsed.max() - parsed.min()).days)
    except Exception:  # noqa: BLE001
        return 0.0


def _recommended_grain(
    span_days: float,
) -> tuple[bool, Literal["day", "month", "quarter"]]:
    """Return (adjusted, grain) based on how wide the time range is."""
    if span_days > 365:
        return True, "month"
    if span_days > 365 * 3:
        return True, "quarter"
    return False, "day"


# ---------------------------------------------------------------------------
# Core classification rules  (first match wins)
# ---------------------------------------------------------------------------

def classify(df: pd.DataFrame, title: str = "") -> ChartConfig:
    """
    Inspect df and return a ChartConfig describing the best chart type.

    Rules (LLD §4.5, first match wins):
      1. 1 row, ≥1 numeric, 0 categorical  → big_number
      2. ≥1 temporal + ≥1 numeric          → line
      3. 1 categorical + 1 numeric, ≤5 unique cat values → pie
      4. 1 categorical + 1 numeric, >5 unique cat values → bar
      5. 2 categorical + 1 numeric          → stacked_bar
      6. fallback                            → table
    """
    if df.empty:
        return ChartConfig(
            chart_type="table",
            title=title,
            metric_col=None,
            groupby_cols=[],
            x_axis_col=None,
            number_format="auto",
        )

    numeric, categorical, temporal = _classify_columns(df)

    n_rows = len(df)
    n_num = len(numeric)
    n_cat = len(categorical)
    n_tmp = len(temporal)

    # pick best metric col (first numeric)
    metric_col = numeric[0] if numeric else None
    number_fmt = _infer_number_format(df[metric_col]) if metric_col else "auto"

    # grain for temporal columns
    grain_adjusted = False
    grain: Literal["day", "month", "quarter"] = "day"
    x_axis_col = temporal[0] if temporal else None
    if x_axis_col:
        span = _time_span_days(df, x_axis_col)
        grain_adjusted, grain = _recommended_grain(span)

    # --- Rule 1: big number ---
    if n_rows == 1 and n_num >= 1 and n_cat == 0 and n_tmp == 0:
        return ChartConfig(
            chart_type="big_number",
            title=title,
            metric_col=metric_col,
            groupby_cols=[],
            x_axis_col=None,
            number_format=number_fmt,
            numeric_cols=numeric,
            categorical_cols=categorical,
            temporal_cols=temporal,
        )

    # --- Rule 2: line chart (time series) ---
    if n_tmp >= 1 and n_num >= 1:
        return ChartConfig(
            chart_type="line",
            title=title,
            metric_col=metric_col,
            groupby_cols=categorical,
            x_axis_col=x_axis_col,
            number_format=number_fmt,
            grain_adjusted=grain_adjusted,
            grain=grain,
            numeric_cols=numeric,
            categorical_cols=categorical,
            temporal_cols=temporal,
        )

    # --- Rule 3 & 4: pie / bar (1 cat + 1 num) ---
    if n_cat == 1 and n_num >= 1 and n_tmp == 0:
        cat_col = categorical[0]
        n_unique = df[cat_col].nunique()
        chart_type: ChartType = "pie" if n_unique <= 5 else "bar"
        return ChartConfig(
            chart_type=chart_type,
            title=title,
            metric_col=metric_col,
            groupby_cols=[cat_col],
            x_axis_col=None,
            number_format=number_fmt,
            numeric_cols=numeric,
            categorical_cols=categorical,
            temporal_cols=temporal,
        )

    # --- Rule 5: stacked bar (2 cat + 1 num) ---
    if n_cat == 2 and n_num >= 1 and n_tmp == 0:
        return ChartConfig(
            chart_type="stacked_bar",
            title=title,
            metric_col=metric_col,
            groupby_cols=categorical,
            x_axis_col=None,
            number_format=number_fmt,
            numeric_cols=numeric,
            categorical_cols=categorical,
            temporal_cols=temporal,
        )

    # --- Rule 6: fallback table ---
    return ChartConfig(
        chart_type="table",
        title=title,
        metric_col=metric_col,
        groupby_cols=categorical + temporal,
        x_axis_col=x_axis_col,
        number_format=number_fmt,
        grain_adjusted=grain_adjusted,
        grain=grain,
        numeric_cols=numeric,
        categorical_cols=categorical,
        temporal_cols=temporal,
    )
