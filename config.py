"""
Central configuration for Bixby Dashboard AI.

All deployment-time settings live here. Agent code must import from this module
rather than reading environment variables or hardcoding table/column names.

Local development: DB_BACKEND = "sqlite" (default below).
Production (Samsung VM): set DB_BACKEND = "bigquery" and update the sections
marked CHANGE FOR PRODUCTION.
"""

from __future__ import annotations

from typing import TypedDict

# ---------------------------------------------------------------------------
# Database backend
# ---------------------------------------------------------------------------

DB_BACKEND: str = "sqlite"
SQLITE_PATH: str = "synthetic/local.db"

# CHANGE FOR PRODUCTION: set DB_BACKEND = "bigquery" and verify project/dataset.
BQ_PROJECT: str = "bixby2-analytics-dev"  # CHANGE FOR PRODUCTION
BQ_DATASET: str = "bxb4_dw"  # CHANGE FOR PRODUCTION

# Optional JSON service-account path. Production on Samsung VM uses ADC
# (gcloud auth application-default login) and leaves this as None.
BQ_CREDENTIALS_PATH: str | None = None  # CHANGE FOR PRODUCTION if not using ADC

# ---------------------------------------------------------------------------
# Raw source table (single source of truth for the trillion-row table name)
# ---------------------------------------------------------------------------

RAW_TABLE_NAME: str = "bxb_dw"

# SQLite-only junction table modeling BigQuery ARRAY<STRING> execution_result.
EXECUTION_RESULTS_TABLE: str = "execution_results"

# ---------------------------------------------------------------------------
# Column mappings (verify on Samsung VM against real INFORMATION_SCHEMA)
# ---------------------------------------------------------------------------

PARTITION_COLUMN: str = "yyyymmddhh"
TIMESTAMP_COLUMN: str = "local_timestamp"  # derived alias kept for LLD SQL compatibility
CONVERSATION_ID_COLUMN: str = "conversation_id"
DEVICE_TYPE_COLUMN: str = "device_type"
REGION_COLUMN: str = "region"
COUNTRY_COLUMN: str = "country"
KPI_COMPLETION_COLUMN: str = "kpi_completion"
UTTERANCE_COLUMN: str = "utterance"

EXECUTION_RESULT_SUCCESS_VALUE: str = "SUCCESS"

# ---------------------------------------------------------------------------
# LLM provider
# LLM_PROVIDER = "gemini"  → uses Google Gemini API (local dev)
# LLM_PROVIDER = "vllm"    → uses OpenAI-compatible vLLM endpoint (production)
# ---------------------------------------------------------------------------

LLM_PROVIDER: str = "gemini"  # CHANGE FOR PRODUCTION to "vllm"

# Gemini (local dev) — CHANGE FOR PRODUCTION: move key to a secrets file
# with restricted file permissions; never commit the real key.
GEMINI_API_KEY: str = "YOUR_GEMINI_API_KEY_HERE"  # CHANGE FOR PRODUCTION: set via env var or secrets file
GEMINI_MODEL: str = "gemini-1.5-flash"

# vLLM / OpenAI-compatible endpoint (production on Samsung GPU machine)
LLM_BASE_URL: str = "http://localhost:8000/v1"  # CHANGE FOR PRODUCTION
LLM_MODEL_NAME: str = "gpt-oss-20b"  # CHANGE FOR PRODUCTION
LLM_API_KEY: str = "not-needed-for-local-vllm"  # CHANGE FOR PRODUCTION if required

# ---------------------------------------------------------------------------
# Superset
# ---------------------------------------------------------------------------

SUPERSET_URL: str = "http://localhost:8088"  # CHANGE FOR PRODUCTION
SUPERSET_USERNAME: str = "admin"  # CHANGE FOR PRODUCTION
SUPERSET_PASSWORD_PATH: str = "secrets/superset_password.txt"  # CHANGE FOR PRODUCTION
SUPERSET_DATABASE_ID: int = 1  # CHANGE FOR PRODUCTION

# ---------------------------------------------------------------------------
# API server
# ---------------------------------------------------------------------------

API_HOST: str = "0.0.0.0"
API_PORT: int = 8000

# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

MAX_SQL_RETRIES: int = 3
SQL_RESULT_LIMIT: int = 100

DOMAIN_NAME: str = "Bixby"  # used in Planner system prompts; swap via config for other domains

# ---------------------------------------------------------------------------
# File paths
# ---------------------------------------------------------------------------

SCHEMA_CACHE_PATH: str = "schema/schema_cache.json"
SCHEDULES_FILE: str = "schedules/recurring_dashboards.json"
LOG_DIR: str = "logs"
CHROMADB_PATH: str = "chromadb_store"

# ---------------------------------------------------------------------------
# Rollup table specs
#
# Naming discipline (LLD §2.4): each rollup table name appears as a string in
# EXACTLY ONE place — the "name" field below. All other code must read
# spec["name"] from ROLLUP_TABLE_SPECS; never duplicate these literals.
#
# Convention: <grain>_<subject>_summary
# Additive-counts principle: store raw counts/sums only; compute rates at query time.
# ---------------------------------------------------------------------------


class RollupTableSpec(TypedDict):
    """Spec for one pre-aggregated datamart maintained by the Scheduler."""

    name: str
    grain_columns: list[str]
    partition_column: str
    source_table: str
    output_columns: list[str]
    aggregations: dict[str, str]


ROLLUP_TABLE_SPECS: list[RollupTableSpec] = [
    {
        # Answers: success/failure trends, KPI averages by execution result type.
        "name": "daily_kpi_summary",
        "grain_columns": ["execution_result"],
        "partition_column": "day",
        "source_table": RAW_TABLE_NAME,
        "output_columns": [
            "day",
            "execution_result",
            "total_conversations",
            "sum_kpi_completion",
        ],
        "aggregations": {
            "total_conversations": "count_conversations",
            "sum_kpi_completion": "sum_kpi_completion",
        },
    },
    {
        # Answers: device performance comparisons and device trends.
        "name": "daily_device_summary",
        "grain_columns": [DEVICE_TYPE_COLUMN],
        "partition_column": "day",
        "source_table": RAW_TABLE_NAME,
        "output_columns": [
            "day",
            DEVICE_TYPE_COLUMN,
            "total_conversations",
            "successful_conversations",
            "sum_kpi_completion",
        ],
        "aggregations": {
            "total_conversations": "count_conversations",
            "successful_conversations": "count_successful_conversations",
            "sum_kpi_completion": "sum_kpi_completion",
        },
    },
    {
        # Answers: conversation volume by hour of day, busiest hours.
        "name": "hourly_volume_summary",
        "grain_columns": [],
        "partition_column": "hour_timestamp",
        "source_table": RAW_TABLE_NAME,
        "output_columns": [
            "hour_timestamp",
            "total_conversations",
        ],
        "aggregations": {
            "total_conversations": "count_conversations",
        },
    },
    {
        # Answers: regional KPI comparison and geographic breakdowns.
        "name": "daily_region_summary",
        "grain_columns": [REGION_COLUMN],
        "partition_column": "day",
        "source_table": RAW_TABLE_NAME,
        "output_columns": [
            "day",
            REGION_COLUMN,
            "total_conversations",
            "successful_conversations",
            "sum_kpi_completion",
        ],
        "aggregations": {
            "total_conversations": "count_conversations",
            "successful_conversations": "count_successful_conversations",
            "sum_kpi_completion": "sum_kpi_completion",
        },
    },
]


def get_rollup_spec_by_name(name: str) -> RollupTableSpec:
    """Return the rollup spec whose name matches (for scheduler / wrangler lookups)."""
    for spec in ROLLUP_TABLE_SPECS:
        if spec["name"] == name:
            return spec
    raise KeyError(f"No rollup table spec named {name!r}")


def rollup_names() -> list[str]:
    """All rollup table names, read from the single source of truth in each spec."""
    return [spec["name"] for spec in ROLLUP_TABLE_SPECS]
