"""Database access — agents call get_dialect(), never backend classes directly."""

from __future__ import annotations

from typing import TYPE_CHECKING

from config import DB_BACKEND

if TYPE_CHECKING:
    from db.dialect import Dialect


def get_dialect() -> Dialect:
    """Return the configured dialect backend (SQLite locally, BigQuery in production)."""
    if DB_BACKEND == "bigquery":
        from db.bigquery_backend import BigQueryDialect

        return BigQueryDialect()
    if DB_BACKEND == "sqlite":
        from db.sqlite_backend import SQLiteDialect

        return SQLiteDialect()
    raise ValueError(f"Unsupported DB_BACKEND: {DB_BACKEND!r}")


__all__ = ["get_dialect"]
