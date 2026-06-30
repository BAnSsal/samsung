"""
Tests for agents/ui_designer.py (LLD §4.6).
Run with:  python -m pytest tests/test_ui_designer.py -v
       or: python tests/test_ui_designer.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents.ui_designer import (
    CHART_DIMENSIONS,
    GRID_COLUMNS,
    LayoutItem,
    design,
    layout_as_superset_positions,
)

# ---------------------------------------------------------------------------
# Minimal stub — avoids importing pandas in tests that don't need it
# ---------------------------------------------------------------------------

@dataclass
class _FakeConfig:
    chart_type: str
    title: str = ""


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


def _print_layout(items: list[LayoutItem]) -> None:
    print(f"  {'idx':>3}  {'row':>3}  {'col':>3}  {'w':>3}  {'h':>5}  type")
    for item in items:
        print(
            f"  {item.chart_index:>3}  {item.row:>3}  "
            f"{item.col:>3}  {item.width:>3}  {item.height:>5}"
        )


def _assert_no_row_exceeds_12(items: list[LayoutItem]) -> None:
    row_end: dict[int, int] = {}
    for item in items:
        end = item.col + item.width
        if end > row_end.get(item.row, 0):
            row_end[item.row] = end
    for row, end in row_end.items():
        _check(end <= GRID_COLUMNS, f"row {row} column span {end} <= {GRID_COLUMNS}")


def _assert_same_row_heights(items: list[LayoutItem]) -> None:
    row_heights: dict[int, set[int]] = {}
    for item in items:
        row_heights.setdefault(item.row, set()).add(item.height)
    for row, heights in row_heights.items():
        _check(len(heights) == 1, f"row {row} has uniform height (got {heights})")


# ---------------------------------------------------------------------------
# Test 1 — single big_number
# ---------------------------------------------------------------------------

def test_single_big_number() -> None:
    _header("Test 1: single big_number")
    charts = [_FakeConfig("big_number")]
    items = design(charts)
    _print_layout(items)

    _check(len(items) == 1, "1 layout item returned")
    _check(items[0].row == 0, "row 0")
    _check(items[0].col == 0, "col 0")
    _check(items[0].width == CHART_DIMENSIONS["big_number"][0], "correct width")
    _assert_no_row_exceeds_12(items)


# ---------------------------------------------------------------------------
# Test 2 — four big_numbers on one row (4 x 3 = 12)
# ---------------------------------------------------------------------------

def test_four_big_numbers_fill_row() -> None:
    _header("Test 2: four big_numbers fill exactly one row (4 x 3 = 12)")
    charts = [_FakeConfig("big_number")] * 4
    items = design(charts)
    _print_layout(items)

    _check(len(items) == 4, "4 items")
    rows = {i.row for i in items}
    _check(rows == {0}, "all on row 0")
    _check([i.col for i in items] == [0, 3, 6, 9], "cols 0,3,6,9")
    _assert_no_row_exceeds_12(items)


# ---------------------------------------------------------------------------
# Test 3 — five big_numbers wrap to second row
# ---------------------------------------------------------------------------

def test_five_big_numbers_wrap() -> None:
    _header("Test 3: five big_numbers wrap to row 1")
    charts = [_FakeConfig("big_number")] * 5
    items = design(charts)
    _print_layout(items)

    _check(items[4].row == 1, "5th chart on row 1")
    _check(items[4].col == 0, "5th chart at col 0")
    _assert_no_row_exceeds_12(items)


# ---------------------------------------------------------------------------
# Test 4 — full-width charts each get their own row
# ---------------------------------------------------------------------------

def test_full_width_charts_each_own_row() -> None:
    _header("Test 4: line + stacked_bar + table each on separate rows")
    charts = [
        _FakeConfig("line"),
        _FakeConfig("stacked_bar"),
        _FakeConfig("table"),
    ]
    items = design(charts)
    _print_layout(items)

    _check(items[0].row == 0 and items[0].col == 0, "line on row 0, col 0")
    _check(items[1].row == 1 and items[1].col == 0, "stacked_bar on row 1, col 0")
    _check(items[2].row == 2 and items[2].col == 0, "table on row 2, col 0")
    _assert_no_row_exceeds_12(items)


# ---------------------------------------------------------------------------
# Test 5 — two pies side-by-side (6+6 = 12)
# ---------------------------------------------------------------------------

def test_two_pies_side_by_side() -> None:
    _header("Test 5: two pies side-by-side (6+6=12)")
    charts = [_FakeConfig("pie"), _FakeConfig("pie")]
    items = design(charts)
    _print_layout(items)

    _check(items[0].row == 0 and items[0].col == 0, "first pie row 0 col 0")
    _check(items[1].row == 0 and items[1].col == 6, "second pie row 0 col 6")
    _assert_no_row_exceeds_12(items)
    _assert_same_row_heights(items)


# ---------------------------------------------------------------------------
# Test 6 — three pies force third onto new row (6+6+6 > 12)
# ---------------------------------------------------------------------------

def test_three_pies_third_wraps() -> None:
    _header("Test 6: three pies — third wraps to row 1 (6+6+6 > 12)")
    charts = [_FakeConfig("pie")] * 3
    items = design(charts)
    _print_layout(items)

    _check(items[2].row == 1, "third pie on row 1")
    _check(items[2].col == 0, "third pie at col 0")
    _assert_no_row_exceeds_12(items)


# ---------------------------------------------------------------------------
# Test 7 — same row height normalisation
# ---------------------------------------------------------------------------

def test_same_row_height_normalised() -> None:
    _header("Test 7: two charts on same row get max height")
    # pie (350px) beside bar (350px) — both same; put big_number (150) with bar
    charts = [
        _FakeConfig("big_number"),  # 3 wide, 150px
        _FakeConfig("bar"),         # 6 wide, 350px — same row
    ]
    items = design(charts)
    _print_layout(items)

    _check(items[0].row == items[1].row, "both on same row")
    _check(
        items[0].height == items[1].height,
        f"heights equalised (got {items[0].height} vs {items[1].height})",
    )
    expected_h = max(
        CHART_DIMENSIONS["big_number"][1],
        CHART_DIMENSIONS["bar"][1],
    )
    _check(items[0].height == expected_h, f"height == {expected_h}")
    _assert_no_row_exceeds_12(items)


# ---------------------------------------------------------------------------
# Test 8 — mixed realistic dashboard
# ---------------------------------------------------------------------------

def test_mixed_realistic_dashboard() -> None:
    _header("Test 8: realistic 6-chart dashboard")
    # Typical layout: 4 KPI numbers, then a line, then a bar+pie side by side
    charts = [
        _FakeConfig("big_number"),   # row 0 cols 0-2
        _FakeConfig("big_number"),   # row 0 cols 3-5
        _FakeConfig("big_number"),   # row 0 cols 6-8
        _FakeConfig("big_number"),   # row 0 cols 9-11
        _FakeConfig("line"),         # row 1 cols 0-11
        _FakeConfig("bar"),          # row 2 cols 0-5
        _FakeConfig("pie"),          # row 2 cols 6-11
    ]
    items = design(charts)
    _print_layout(items)

    _check(len(items) == 7, "7 layout items")
    _check({items[i].row for i in range(4)} == {0}, "first 4 charts on row 0")
    _check(items[4].row == 1, "line on row 1")
    _check(items[5].row == 2 and items[5].col == 0, "bar on row 2 col 0")
    _check(items[6].row == 2 and items[6].col == 6, "pie on row 2 col 6")
    _assert_no_row_exceeds_12(items)
    _assert_same_row_heights(items)


# ---------------------------------------------------------------------------
# Test 9 — empty input
# ---------------------------------------------------------------------------

def test_empty_input() -> None:
    _header("Test 9: empty chart list")
    items = design([])
    _check(items == [], "empty input -> empty output")


# ---------------------------------------------------------------------------
# Test 10 — superset position conversion
# ---------------------------------------------------------------------------

def test_superset_positions() -> None:
    _header("Test 10: layout_as_superset_positions output shape")
    charts = [_FakeConfig("line"), _FakeConfig("bar")]
    items = design(charts)
    chart_ids = [101, 102]
    positions = layout_as_superset_positions(items, chart_ids)
    _print_layout(items)
    print(f"  positions[0]: {positions[0]}")
    print(f"  positions[1]: {positions[1]}")

    _check(len(positions) == 2, "2 position dicts")
    _check(positions[0]["meta"]["chartId"] == 101, "chartId 101")
    _check(positions[1]["meta"]["chartId"] == 102, "chartId 102")
    _check(positions[0]["meta"]["width"] == 12, "line width 12")
    for pos in positions:
        _check("gridLayout" in pos, "gridLayout key present")
        _check(pos["gridLayout"]["w"] <= GRID_COLUMNS, "w <= 12")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> None:
    print("\n=== UI Designer Agent layout tests ===")
    tests = [
        test_single_big_number,
        test_four_big_numbers_fill_row,
        test_five_big_numbers_wrap,
        test_full_width_charts_each_own_row,
        test_two_pies_side_by_side,
        test_three_pies_third_wraps,
        test_same_row_height_normalised,
        test_mixed_realistic_dashboard,
        test_empty_input,
        test_superset_positions,
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
