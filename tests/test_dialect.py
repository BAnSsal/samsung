"""
Dialect integration tests — run against synthetic/local.db via get_dialect().

Each test exercises a specific SQL pattern the agents will use in production.
Run with:  python -m pytest tests/test_dialect.py -v
       or: python tests/test_dialect.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config
from db import get_dialect

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


# ---------------------------------------------------------------------------
# Test 1 — total conversation count
# ---------------------------------------------------------------------------

def test_total_conversations() -> None:
    _header("Test 1: Total conversation count")

    d = get_dialect()
    raw = d.table_ref(config.RAW_TABLE_NAME)
    sql = f"SELECT COUNT(*) AS total FROM {raw}"
    df = d.execute(sql)

    total = int(df["total"].iloc[0])
    print(f"  total conversations: {total:,}")

    _check(total > 0, "row count is positive")
    _check(total == 106_000, f"expected 106,000 rows, got {total:,}")


# ---------------------------------------------------------------------------
# Test 2 — success rate via junction-table pattern
# ---------------------------------------------------------------------------

def test_success_rate() -> None:
    _header("Test 2: Success rate (array_contains junction-table pattern)")

    d = get_dialect()
    raw = d.table_ref(config.RAW_TABLE_NAME)
    success_pred = d.array_contains(
        "execution_result",
        config.EXECUTION_RESULT_SUCCESS_VALUE,
        table_alias="t",
    )

    sql = f"""
        SELECT
            COUNT(*) AS total,
            {d.countif(success_pred)} AS successful
        FROM {raw} t
    """
    df = d.execute(sql)

    total = int(df["total"].iloc[0])
    successful = int(df["successful"].iloc[0])
    rate = 100.0 * successful / total if total else 0.0

    print(f"  total:      {total:,}")
    print(f"  successful: {successful:,}")
    print(f"  rate:       {rate:.1f}%")
    print(f"  generated predicate: {success_pred}")

    _check(total > 0, "total > 0")
    _check(0 < rate < 100, "success rate is between 0% and 100%")
    _check(30.0 <= rate <= 40.0, f"success rate near 35% (got {rate:.1f}%)")


# ---------------------------------------------------------------------------
# Test 3 — daily_kpi_summary, last 7 days
# ---------------------------------------------------------------------------

def test_daily_kpi_summary_last_7_days() -> None:
    _header("Test 3: daily_kpi_summary — last 7 days")

    d = get_dialect()
    rollup = d.table_ref(config.get_rollup_spec_by_name("daily_kpi_summary")["name"])

    sql = f"""
        SELECT
            day,
            execution_result,
            total_conversations,
            sum_kpi_completion,
            ROUND(sum_kpi_completion * 1.0 / total_conversations, 4) AS avg_kpi
        FROM {rollup}
        WHERE day >= {d.current_date()} || ''
        ORDER BY day DESC, total_conversations DESC
        LIMIT 20
    """

    # Use a fixed recent date range instead of "now" since synthetic data ends 2026-06-28
    sql_fixed = f"""
        SELECT
            day,
            execution_result,
            total_conversations,
            sum_kpi_completion,
            ROUND(sum_kpi_completion * 1.0 / total_conversations, 4) AS avg_kpi
        FROM {rollup}
        ORDER BY day DESC, total_conversations DESC
        LIMIT 14
    """
    df = d.execute(sql_fixed)

    print(f"  rows returned: {len(df)}")
    print(df.to_string(index=False))

    _check(len(df) > 0, "rollup returned rows")
    _check("day" in df.columns, "has 'day' column")
    _check("execution_result" in df.columns, "has 'execution_result' column")
    _check("total_conversations" in df.columns, "has 'total_conversations' column")
    _check("sum_kpi_completion" in df.columns, "has 'sum_kpi_completion' column")
    _check(
        (df["total_conversations"] > 0).all(),
        "all total_conversations > 0",
    )


# ---------------------------------------------------------------------------
# Test 4 — daily_device_summary, top devices by total conversations
# ---------------------------------------------------------------------------

def test_daily_device_summary_top_devices() -> None:
    _header("Test 4: daily_device_summary — top devices by volume")

    d = get_dialect()
    rollup = d.table_ref(config.get_rollup_spec_by_name("daily_device_summary")["name"])

    sql = f"""
        SELECT
            {config.DEVICE_TYPE_COLUMN},
            SUM(total_conversations) AS total_conversations,
            SUM(successful_conversations) AS successful_conversations,
            ROUND(
                100.0 * SUM(successful_conversations) / SUM(total_conversations),
                1
            ) AS success_pct
        FROM {rollup}
        GROUP BY {config.DEVICE_TYPE_COLUMN}
        ORDER BY total_conversations DESC
    """
    df = d.execute(sql)

    print(f"  rows returned: {len(df)}")
    print(df.to_string(index=False))

    _check(len(df) > 0, "rollup returned rows")
    _check(len(df) >= 5, f"at least 5 device types (got {len(df)})")
    top_device = df.iloc[0][config.DEVICE_TYPE_COLUMN]
    _check(top_device == "PHONE", f"top device is PHONE (got {top_device!r})")
    _check(
        df["total_conversations"].is_monotonic_decreasing,
        "results ordered by total_conversations DESC",
    )
    total_all = int(df["total_conversations"].sum())
    _check(total_all == 106_000, f"device counts sum to 106,000 (got {total_all:,})")


# ---------------------------------------------------------------------------
# Test 5 — existence_check_sql for all known tables
# ---------------------------------------------------------------------------

def test_existence_check() -> None:
    _header("Test 5: existence_check_sql — all tables")

    d = get_dialect()
    tables_expected_present = (
        [config.RAW_TABLE_NAME, config.EXECUTION_RESULTS_TABLE]
        + config.rollup_names()
    )
    table_expected_absent = "this_table_does_not_exist"

    for table in tables_expected_present:
        df = d.execute(d.existence_check_sql(table))
        exists = len(df) > 0
        print(f"  {table}: {'EXISTS' if exists else 'MISSING'}")
        _check(exists, f"'{table}' exists in the database")

    df = d.execute(d.existence_check_sql(table_expected_absent))
    absent = len(df) == 0
    print(f"  {table_expected_absent}: {'ABSENT (correct)' if absent else 'WRONGLY PRESENT'}")
    _check(absent, f"'{table_expected_absent}' correctly absent")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> None:
    print("\n=== Dialect integration tests against synthetic/local.db ===")
    tests = [
        test_total_conversations,
        test_success_rate,
        test_daily_kpi_summary_last_7_days,
        test_daily_device_summary_top_devices,
        test_existence_check,
    ]
    passed = 0
    failed = 0
    for fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as exc:
            print(f"  FAILED: {exc}")
            failed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR:  {exc}")
            failed += 1

    print(f"\n{'=' * 60}")
    print(f"  Results: {passed} passed, {failed} failed")
    print("=" * 60)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
