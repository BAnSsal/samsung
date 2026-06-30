"""Abstract database dialect — agents use get_dialect(), never backend classes directly."""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class Dialect(ABC):
    """Base class for backend-specific SQL fragment generation and query execution."""

    @abstractmethod
    def table_ref(self, table_name: str) -> str:
        """Return a fully qualified or bare table reference for the backend."""

    @abstractmethod
    def date_sub(self, days: int) -> str:
        """Return a SQL expression for the timestamp N days before now."""

    @abstractmethod
    def current_date(self) -> str:
        """Return a SQL expression for today's date."""

    @abstractmethod
    def countif(self, condition: str) -> str:
        """Return a SQL aggregate that counts rows matching condition."""

    @abstractmethod
    def array_contains(
        self, array_col: str, value: str, *, table_alias: str = "t"
    ) -> str:
        """Return a SQL predicate checking array membership (or junction-table equivalent)."""

    @abstractmethod
    def partition_filter(self, days_back: int) -> str:
        """Return a WHERE-clause fragment enforcing the partition column lower bound."""

    @abstractmethod
    def existence_check_sql(self, table_name: str) -> str:
        """Return SQL that yields a row when table_name exists in this backend."""

    @abstractmethod
    def execute(self, sql: str) -> pd.DataFrame:
        """Run SQL and return results as a DataFrame."""

    @abstractmethod
    def list_tables_sql(self) -> str:
        """Return SQL that yields a 'table_name' column for every user table."""

    @abstractmethod
    def list_columns_sql(self, table_name: str) -> str:
        """Return SQL that yields 'column_name' and 'data_type' for every column in table_name."""
