"""
UI Designer Agent (LLD §4.6) — pure rule-based, no LLM.

Takes the list of ChartConfig objects produced by the Chart Type Agent and
assigns a grid layout ({row, width, height}) to each chart using Superset's
12-column grid.

Rules:
  - Each chart type has a fixed (width, height) from CHART_DIMENSIONS.
  - Charts are placed left-to-right; when the next chart would exceed 12
    columns the row counter increments and the column resets to 0.
  - All charts sharing the same row are forced to the same height
    (the maximum height of any chart in that row).

Public API:
    design(chart_configs: list[ChartConfig]) -> list[LayoutItem]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.chart_type import ChartConfig, ChartType

GRID_COLUMNS: int = 12

# ---------------------------------------------------------------------------
# Fixed dimensions per chart type  (width in grid columns, height in pixels)
# Width choices rationale:
#   big_number  → 3 cols (fits 4 per row for a KPI row)
#   line        → 12 cols (needs horizontal space to show trend)
#   pie         → 6 cols (half-width, usually paired)
#   bar         → 6 cols (half-width, pairs well)
#   stacked_bar → 12 cols (needs full width for legibility)
#   table       → 12 cols (full width for readable columns)
# ---------------------------------------------------------------------------

CHART_DIMENSIONS: dict[str, tuple[int, int]] = {
    # chart_type: (width_cols, height_px)
    "big_number":  (3,  150),
    "line":        (12, 400),
    "pie":         (6,  350),
    "bar":         (6,  350),
    "stacked_bar": (12, 400),
    "table":       (12, 400),
}

_DEFAULT_DIMENSIONS: tuple[int, int] = (6, 350)


# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------

@dataclass
class LayoutItem:
    """Grid position for one chart."""
    chart_index: int        # position in the input list (0-based)
    row: int                # grid row (0-based)
    col: int                # starting column within the row (0-based)
    width: int              # column span (1–12)
    height: int             # height in pixels (may be raised to row maximum)


# ---------------------------------------------------------------------------
# Layout engine
# ---------------------------------------------------------------------------

def design(chart_configs: list) -> list[LayoutItem]:
    """
    Assign {row, col, width, height} to each chart.

    Parameters
    ----------
    chart_configs:
        List of ChartConfig (or any object with a .chart_type attribute).

    Returns
    -------
    List of LayoutItem, one per chart, in the same order as the input.
    """
    if not chart_configs:
        return []

    items: list[LayoutItem] = []
    current_row = 0
    current_col = 0

    for idx, cfg in enumerate(chart_configs):
        chart_type: str = getattr(cfg, "chart_type", "table")
        width, height = CHART_DIMENSIONS.get(chart_type, _DEFAULT_DIMENSIONS)

        # Overflow: start a new row if this chart won't fit
        if current_col + width > GRID_COLUMNS:
            current_row += 1
            current_col = 0

        items.append(LayoutItem(
            chart_index=idx,
            row=current_row,
            col=current_col,
            width=width,
            height=height,
        ))
        current_col += width

        # If we land exactly at 12 cols, next chart starts a new row
        if current_col == GRID_COLUMNS:
            current_row += 1
            current_col = 0

    # Enforce same-row height  (all charts in a row get the row's max height)
    _normalise_row_heights(items)

    return items


def _normalise_row_heights(items: list[LayoutItem]) -> None:
    """Raise every chart in a row to the maximum height of that row."""
    row_max: dict[int, int] = {}
    for item in items:
        row_max[item.row] = max(row_max.get(item.row, 0), item.height)
    for item in items:
        item.height = row_max[item.row]


# ---------------------------------------------------------------------------
# Helpers for Dashboard Code Gen
# ---------------------------------------------------------------------------

def layout_as_superset_positions(
    items: list[LayoutItem],
    chart_ids: list[int],
) -> list[dict]:
    """
    Convert LayoutItems to Superset position dicts.

    Superset's position JSON uses units where 1 column = 1 unit and
    height is in 'sliceHeight' rows (each row = ~100 px).  We normalise
    height to the nearest whole 100-px row.

    Parameters
    ----------
    items:
        Output of design().
    chart_ids:
        List of Superset chart IDs in the same order as items.

    Returns
    -------
    List of position dicts ready for Superset PUT /api/v1/dashboard/{id}.
    """
    positions = []
    for item, chart_id in zip(items, chart_ids):
        positions.append({
            "type": "CHART",
            "id": f"CHART-{chart_id}",
            "meta": {
                "chartId": chart_id,
                "width": item.width,
                "height": max(1, round(item.height / 100)),
            },
            "gridLayout": {
                "x": item.col,
                "y": item.row * 4,      # Superset rows are 4 units tall by default
                "w": item.width,
                "h": max(1, round(item.height / 100)),
            },
        })
    return positions
