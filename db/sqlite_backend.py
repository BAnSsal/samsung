"""SQLite dialect for local development against synthetic/local.db."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

import config
from db.dialect import Dialect

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


class SQLiteDialect(Dialect):
    """SQLite implementation; execution_result arrays use the execution_results junction table."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        path = Path(db_path) if db_path is not None else Path(config.SQLITE_PATH)
        if not path.is_absolute():
            path = _PROJECT_ROOT / path
        self._db_path = path

    def table_ref(self, table_name: str) -> str:
        return table_name

    def date_sub(self, days: int) -> str:
        return f"datetime('now', '-{int(days)} days')"

    def current_date(self) -> str:
        return "date('now')"

    def countif(self, condition: str) -> str:
        return f"SUM(CASE WHEN {condition} THEN 1 ELSE 0 END)"

    def array_contains(self, array_col: str, value: str, *, table_alias: str = "t") -> str:
        """
        Map BigQuery ARRAY membership to a junction-table EXISTS subquery.

        array_col is the logical column name (e.g. execution_result); locally the
        values live in config.EXECUTION_RESULTS_TABLE joined on conversation_id.
        """
        er_table = self.table_ref(config.EXECUTION_RESULTS_TABLE)
        conv_col = config.CONVERSATION_ID_COLUMN
        literal = _sql_string_literal(value)
        return (
            f"EXISTS (SELECT 1 FROM {er_table} er "
            f"WHERE er.{conv_col} = {table_alias}.{conv_col} "
            f"AND er.result = {literal})"
        )

    def partition_filter(self, days_back: int) -> str:
        # Generated even though SQLite does not enforce partition pruning locally.
        col = config.PARTITION_COLUMN
        return f"{col} >= {self.date_sub(days_back)}"

    def existence_check_sql(self, table_name: str) -> str:
        literal = _sql_string_literal(table_name)
        return f"SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = {literal}"

    def execute(self, sql: str) -> pd.DataFrame:
        with sqlite3.connect(self._db_path) as conn:
            return pd.read_sql_query(sql, conn)

    def list_tables_sql(self) -> str:
        return (
            "SELECT name AS table_name "
            "FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )

    def list_columns_sql(self, table_name: str) -> str:
        # PRAGMA table_info returns cid, name, type, notnull, dflt_value, pk
        return f"PRAGMA table_info({table_name})"
