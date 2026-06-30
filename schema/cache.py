"""
Schema Awareness Component (LLD §4.10).

Introspects the configured database via the dialect layer and writes a rich
JSON cache to config.SCHEMA_CACHE_PATH.  Works against SQLite locally and
BigQuery in production — all backend-specific code lives in the dialect.

Each table entry includes:
  - AI-generated table and column descriptions (via Gemini locally, vLLM in prod)
  - Primary key columns (from PRAGMA / INFORMATION_SCHEMA)
  - Foreign key constraints
  - Sample values (pulled live from the DB)
  - is_categorical flag (heuristic: string column with low cardinality)

Usage:
    python -m schema.cache            # refresh + print summary
    python -m schema.cache --quiet    # refresh, no output
    python -m schema.cache --no-ai    # skip Gemini, structural only

Public API:
    from schema.cache import load_schema_cache, schema_as_prompt_string
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config
from db import get_dialect

# ---------------------------------------------------------------------------
# Rich TypedDicts matching the target schema format
# ---------------------------------------------------------------------------


class ColumnMeta(TypedDict):
    name: str
    data_type: str
    nullable: bool
    description: str
    sample_values: list[Any]
    is_categorical: bool


class ForeignKey(TypedDict):
    column: str
    references_table: str
    references_column: str


class TableMeta(TypedDict):
    table_name: str
    table_description: str
    primary_key: list[str]
    foreign_keys: list[ForeignKey]
    columns: list[ColumnMeta]


class SchemaCache(TypedDict):
    generated_at: str
    db_backend: str
    tables: list[TableMeta]


# ---------------------------------------------------------------------------
# Structural introspection helpers
# ---------------------------------------------------------------------------

_CATEGORICAL_CARDINALITY_THRESHOLD = 0.02   # < 2% distinct → categorical
_SAMPLE_LIMIT = 5


def _get_primary_keys_sqlite(dialect, table_name: str) -> list[str]:
    df = dialect.execute(f"PRAGMA table_info({table_name})")
    return df[df["pk"] > 0]["name"].tolist()


def _get_foreign_keys_sqlite(dialect, table_name: str) -> list[ForeignKey]:
    try:
        df = dialect.execute(f"PRAGMA foreign_key_list({table_name})")
        if df.empty:
            return []
        return [
            {
                "column": str(row["from"]),
                "references_table": str(row["table"]),
                "references_column": str(row["to"]),
            }
            for _, row in df.iterrows()
        ]
    except Exception:  # noqa: BLE001
        return []


def _get_sample_values(dialect, table_name: str, col_name: str) -> list[Any]:
    try:
        df = dialect.execute(
            f"SELECT DISTINCT {col_name} FROM {table_name} "
            f"WHERE {col_name} IS NOT NULL LIMIT {_SAMPLE_LIMIT}"
        )
        return [v for v in df[col_name].tolist() if v is not None]
    except Exception:  # noqa: BLE001
        return []


def _is_categorical(
    dialect,
    table_name: str,
    col_name: str,
    data_type: str,
) -> bool:
    if data_type.upper() not in ("TEXT", "VARCHAR", "STRING"):
        return False
    try:
        total = int(
            dialect.execute(f"SELECT COUNT(*) AS n FROM {table_name}").iloc[0, 0]
        )
        distinct = int(
            dialect.execute(
                f"SELECT COUNT(DISTINCT {col_name}) AS n FROM {table_name}"
            ).iloc[0, 0]
        )
        return total > 0 and (distinct / total) < _CATEGORICAL_CARDINALITY_THRESHOLD
    except Exception:  # noqa: BLE001
        return False


def _introspect_structure(dialect) -> list[dict]:
    """
    Return raw structural metadata for all tables.
    Each entry has: table_name, primary_key, foreign_keys, columns
    (columns include name, data_type, nullable, sample_values, is_categorical).
    No AI descriptions yet.
    """
    tables_df = dialect.execute(dialect.list_tables_sql())
    result: list[dict] = []

    for table_name in tables_df["table_name"].tolist():
        cols_df = dialect.execute(dialect.list_columns_sql(table_name))

        if config.DB_BACKEND == "bigquery":
            primary_keys: list[str] = []
            foreign_keys: list[ForeignKey] = []
            columns = []
            for _, row in cols_df.iterrows():
                col_name = str(row["column_name"])
                data_type = str(row.get("data_type", "TEXT")).upper()
                nullable = str(row.get("is_nullable", "YES")).upper() == "YES"
                samples = _get_sample_values(dialect, table_name, col_name)
                categorical = _is_categorical(dialect, table_name, col_name, data_type)
                columns.append({
                    "name": col_name,
                    "data_type": data_type,
                    "nullable": nullable,
                    "sample_values": samples,
                    "is_categorical": categorical,
                })
        else:
            primary_keys = _get_primary_keys_sqlite(dialect, table_name)
            foreign_keys = _get_foreign_keys_sqlite(dialect, table_name)
            columns = []
            for _, row in cols_df.iterrows():
                col_name = str(row["name"])
                data_type = (str(row["type"]) or "TEXT").upper()
                nullable = int(row["notnull"]) == 0
                samples = _get_sample_values(dialect, table_name, col_name)
                categorical = _is_categorical(dialect, table_name, col_name, data_type)
                columns.append({
                    "name": col_name,
                    "data_type": data_type,
                    "nullable": nullable,
                    "sample_values": samples,
                    "is_categorical": categorical,
                })

        result.append({
            "table_name": table_name,
            "primary_key": primary_keys,
            "foreign_keys": foreign_keys,
            "columns": columns,
        })

    return sorted(result, key=lambda t: t["table_name"])


# ---------------------------------------------------------------------------
# AI enrichment (Gemini locally, vLLM in production)
# ---------------------------------------------------------------------------

def _build_enrichment_prompt(tables: list[dict]) -> str:
    """Build the prompt sent to the AI to generate table/column descriptions."""
    schema_summary = json.dumps(
        [
            {
                "table_name": t["table_name"],
                "primary_key": t["primary_key"],
                "foreign_keys": t["foreign_keys"],
                "columns": [
                    {
                        "name": c["name"],
                        "data_type": c["data_type"],
                        "nullable": c["nullable"],
                        "sample_values": c["sample_values"][:3],
                        "is_categorical": c["is_categorical"],
                    }
                    for c in t["columns"]
                ],
            }
            for t in tables
        ],
        indent=2,
    )

    return f"""You are a senior data engineer documenting a Samsung Bixby voice-assistant analytics database.

Below is the raw schema of all tables. For each table and each column, write clear, concise descriptions that would help an AI analyst generate correct SQL.

Context:
- bxb_dw is the raw events table (trillions of rows in production, SQLite locally). Each row is one Bixby conversation.
- execution_results is a junction table modelling BigQuery ARRAY<STRING> execution_result (one row per result value per conversation).
- daily_kpi_summary, daily_device_summary, hourly_volume_summary, daily_region_summary are pre-aggregated rollup datamarts. Prefer these for trend and aggregate queries instead of the raw table.
- All counts in rollup tables are additive raw counts — never pre-computed rates. Compute rates at query time by dividing sums.

Return ONLY valid JSON — no markdown fences, no extra text. The JSON must be an array with one object per table:

[
  {{
    "table_name": "<exact table name>",
    "table_description": "<1-2 sentences describing purpose and when to use it>",
    "columns": [
      {{
        "name": "<exact column name>",
        "description": "<1 sentence: what this column means and how to use it in SQL>"
      }}
    ]
  }}
]

Schema to document:
{schema_summary}"""


def _call_gemini(prompt: str) -> str:
    import re as _re  # noqa: PLC0415
    import google.generativeai as genai  # noqa: PLC0415

    genai.configure(api_key=config.GEMINI_API_KEY)

    # Try each model in order; fall through on quota/access errors.
    models_to_try = [config.GEMINI_MODEL, "gemini-1.5-flash", "gemini-1.5-pro", "gemini-pro"]
    models_to_try = list(dict.fromkeys(models_to_try))  # deduplicate, preserve order

    last_exc: Exception | None = None
    for model_name in models_to_try:
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(prompt)
                return response.text
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                err_str = str(exc)
                # Extract retry delay from error message if present
                match = _re.search(r"retry in ([\d.]+)s", err_str)
                wait = float(match.group(1)) + 1.0 if match else 5.0 * (attempt + 1)
                is_quota = "quota" in err_str.lower() or "429" in err_str
                is_not_found = "404" in err_str or "not found" in err_str.lower()

                if is_not_found:
                    # Model not available — try next model immediately
                    break
                if is_quota and attempt < max_retries:
                    print(f"    Rate-limited on {model_name}, waiting {wait:.0f}s before retry {attempt + 1}/{max_retries}...")
                    time.sleep(wait)
                    continue
                # Permanent error or exhausted retries for this model
                break

    raise RuntimeError(f"All Gemini models exhausted. Last error: {last_exc}") from last_exc


def _call_vllm(prompt: str) -> str:
    from openai import OpenAI  # noqa: PLC0415

    client = OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY)
    response = client.chat.completions.create(
        model=config.LLM_MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
    )
    return response.choices[0].message.content or ""


def _parse_ai_response(raw: str) -> list[dict]:
    """Extract the JSON array from AI response, tolerating markdown fences."""
    text = raw.strip()
    # Strip ```json ... ``` fences if present
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


# ---------------------------------------------------------------------------
# Hardcoded fallback descriptions (used when AI is unavailable)
# ---------------------------------------------------------------------------

_FALLBACK_TABLE_DESCRIPTIONS: dict[str, str] = {
    "bxb_dw": (
        "Main raw events table storing one row per Bixby conversation. "
        "Contains 161 columns in production (BigQuery). "
        "Use for session-level or today's live data only; "
        "prefer the daily_*/hourly_* rollup tables for historical trends."
    ),
    "execution_results": (
        "Junction table modelling BigQuery's ARRAY<STRING> execution_result column. "
        "Each row links a conversation_id to one execution result value. "
        "JOIN to bxb_dw on conversation_id; use to filter or count by result type."
    ),
    "daily_kpi_summary": (
        "Pre-aggregated daily rollup by execution_result type. "
        "Use for KPI trend charts and success-rate calculations over historical periods. "
        "Stores additive counts — divide sum at query time, never store pre-computed rates."
    ),
    "daily_device_summary": (
        "Pre-aggregated daily rollup by device_type. "
        "Use for device breakdown charts and per-device success analysis. "
        "Stores total_conversations and successful_conversations as additive counts."
    ),
    "hourly_volume_summary": (
        "Pre-aggregated hourly rollup of total conversation volume. "
        "Use for intra-day traffic patterns and peak-hour analysis."
    ),
    "daily_region_summary": (
        "Pre-aggregated daily rollup by geographic region. "
        "Use for regional breakdown charts and per-region success analysis."
    ),
}

_FALLBACK_COLUMN_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "bxb_dw": {
        "conversation_id": "Unique identifier for one end-to-end Bixby session. Use for DISTINCT counts.",
        "yyyymmddhh": "Partition key (format YYYYMMDDHH as ISO timestamp locally). MUST appear in BigQuery WHERE clause.",
        "local_timestamp": "Derived local timestamp string; kept for LLD SQL compatibility alongside yyyymmddhh.",
        "device_id": "Hardware device identifier. NOTE: verify if device_type exists separately in the 161-column production schema.",
        "device_type": "Category of device (PHONE, SPEAKER, TV, WATCH, TABLET, CAR, OTHER). High-value grouping dimension.",
        "country": "User's country. Maps to region for rollup tables. India ~60%, US ~15%, Korea ~10%.",
        "region": "Geographic region derived from country (APAC, AMERICAS, EMEA, KOREA). Used in daily_region_summary.",
        "utterance": "Raw text of what the user said to Bixby. Not used in aggregations.",
        "kpi_completion": "Numeric task-completion score 0.0–1.0. SUM this column in rollups; compute averages at query time.",
    },
    "execution_results": {
        "conversation_id": "Foreign key to bxb_dw.conversation_id. One conversation can have multiple rows here.",
        "result": "Execution result value: SUCCESS, EXECUTION_FAILED, EXECUTION_DEEPLINK_REQUESTED, DEVICE_FEATURE_NOT_SUPPORTED, etc. Always uppercase.",
    },
    "daily_kpi_summary": {
        "day": "Date string YYYY-MM-DD. Group by this for daily trend charts.",
        "execution_result": "One of the execution result values (SUCCESS, EXECUTION_FAILED, etc.).",
        "total_conversations": "Count of conversations with this execution_result on this day. Additive — SUM across days.",
        "sum_kpi_completion": "Sum of kpi_completion scores for this group. Divide by total_conversations for average.",
    },
    "daily_device_summary": {
        "day": "Date string YYYY-MM-DD.",
        "device_type": "Device category (PHONE, SPEAKER, TV, WATCH, TABLET, CAR, OTHER).",
        "total_conversations": "Total conversations for this device type on this day. Additive.",
        "successful_conversations": "Conversations that contained SUCCESS in execution_results. Additive.",
        "sum_kpi_completion": "Sum of kpi_completion for this device+day group. Additive.",
    },
    "hourly_volume_summary": {
        "hour_timestamp": "Truncated timestamp string representing the start of the hour.",
        "total_conversations": "Total conversations that started in this hour bucket. Additive.",
    },
    "daily_region_summary": {
        "day": "Date string YYYY-MM-DD.",
        "region": "Geographic region: APAC, AMERICAS, EMEA, KOREA.",
        "total_conversations": "Total conversations for this region on this day. Additive.",
        "successful_conversations": "Conversations containing SUCCESS for this region+day. Additive.",
        "sum_kpi_completion": "Sum of kpi_completion for this region+day group. Additive.",
    },
}


def _apply_fallback_descriptions(tables: list[dict]) -> list[dict]:
    """Fill in descriptions from the hardcoded fallback dictionary."""
    for table in tables:
        tname = table["table_name"]
        if not table.get("table_description"):
            table["table_description"] = _FALLBACK_TABLE_DESCRIPTIONS.get(tname, "")
        col_descs = _FALLBACK_COLUMN_DESCRIPTIONS.get(tname, {})
        for col in table["columns"]:
            if not col.get("description"):
                col["description"] = col_descs.get(col["name"], "")
    return tables


def _enrich_with_ai(tables: list[dict], *, quiet: bool = False) -> list[dict]:
    """
    Call the configured LLM to generate table/column descriptions.
    Returns the same list with 'table_description' and per-column 'description' added.
    Falls back to empty strings if the AI call fails.
    """
    if not quiet:
        print(f"  Calling {config.LLM_PROVIDER} ({config.GEMINI_MODEL if config.LLM_PROVIDER == 'gemini' else config.LLM_MODEL_NAME}) for descriptions...")

    prompt = _build_enrichment_prompt(tables)
    raw = ""
    try:
        if config.LLM_PROVIDER == "gemini":
            raw = _call_gemini(prompt)
        else:
            raw = _call_vllm(prompt)

        ai_data = _parse_ai_response(raw)
        ai_by_table = {entry["table_name"]: entry for entry in ai_data}

        for table in tables:
            ai_entry = ai_by_table.get(table["table_name"], {})
            table["table_description"] = ai_entry.get("table_description", "")
            ai_cols = {c["name"]: c.get("description", "") for c in ai_entry.get("columns", [])}
            for col in table["columns"]:
                col["description"] = ai_cols.get(col["name"], "")

    except Exception as exc:  # noqa: BLE001
        if not quiet:
            print(f"  WARNING: AI enrichment failed ({exc}).")
            print("  Falling back to built-in descriptions.")
        for table in tables:
            table.setdefault("table_description", "")
            for col in table["columns"]:
                col.setdefault("description", "")

    # Always fill any remaining empty descriptions from the fallback dict
    tables = _apply_fallback_descriptions(tables)
    return tables


# ---------------------------------------------------------------------------
# Assemble final TableMeta list
# ---------------------------------------------------------------------------

def _assemble(tables: list[dict]) -> list[TableMeta]:
    result: list[TableMeta] = []
    for t in tables:
        columns: list[ColumnMeta] = [
            {
                "name": c["name"],
                "data_type": c["data_type"],
                "nullable": c["nullable"],
                "description": c.get("description", ""),
                "sample_values": c["sample_values"],
                "is_categorical": c["is_categorical"],
            }
            for c in t["columns"]
        ]
        result.append({
            "table_name": t["table_name"],
            "table_description": t.get("table_description", ""),
            "primary_key": t["primary_key"],
            "foreign_keys": t["foreign_keys"],
            "columns": columns,
        })
    return result


# ---------------------------------------------------------------------------
# Cache read / write
# ---------------------------------------------------------------------------

def _cache_path() -> Path:
    path = Path(config.SCHEMA_CACHE_PATH)
    if not path.is_absolute():
        path = _ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def refresh_schema_cache(*, quiet: bool = False, use_ai: bool = True) -> SchemaCache:
    """
    Introspect the database, optionally enrich with AI descriptions, write
    schema_cache.json, and return the cache object.

    Parameters
    ----------
    quiet:   suppress console output.
    use_ai:  if False, skip the AI call (structural metadata only).
    """
    dialect = get_dialect()

    if not quiet:
        print("Introspecting database structure...")
    tables = _introspect_structure(dialect)

    if use_ai:
        tables = _enrich_with_ai(tables, quiet=quiet)
    else:
        for t in tables:
            t.setdefault("table_description", "")
            for c in t["columns"]:
                c.setdefault("description", "")
        tables = _apply_fallback_descriptions(tables)

    assembled = _assemble(tables)

    cache: SchemaCache = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_backend": config.DB_BACKEND,
        "tables": assembled,
    }

    path = _cache_path()
    path.write_text(json.dumps(cache, indent=2), encoding="utf-8")

    if not quiet:
        print(f"\nSchema cache written to {path}")
        print(f"  backend : {cache['db_backend']}")
        print(f"  tables  : {len(assembled)}")
        for t in assembled:
            ai_flag = " (AI-enriched)" if t["table_description"] else ""
            print(f"    {t['table_name']} ({len(t['columns'])} cols){ai_flag}")

    return cache


def load_schema_cache() -> SchemaCache:
    """Return the cached schema. Raises FileNotFoundError if missing."""
    path = _cache_path()
    if not path.exists():
        raise FileNotFoundError(
            f"Schema cache not found at {path}. "
            "Run `python -m schema.cache` to generate it."
        )
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# LLM prompt string (used by Planner system prompt)
# ---------------------------------------------------------------------------

def schema_as_prompt_string(cache: SchemaCache | None = None) -> str:
    """
    Return a rich, human-readable schema summary for injection into LLM prompts.
    Includes table descriptions, column descriptions, sample values, and PK/FK info.
    """
    if cache is None:
        cache = load_schema_cache()

    lines: list[str] = [
        f"Database backend: {cache['db_backend']}",
        f"Last refreshed:   {cache['generated_at']}",
        "",
        "IMPORTANT QUERY RULES:",
        "  - Prefer rollup summary tables (daily_*/hourly_*) for trend/aggregate queries.",
        "  - Only query bxb_dw directly for session-level or today's live data.",
        "  - Rollup counts are additive — SUM then divide for rates; never average stored rates.",
        "  - In BigQuery every query on bxb_dw MUST include a partition filter on yyyymmddhh.",
        "",
    ]

    for table in cache["tables"]:
        pk_str = f"  PK: {table['primary_key']}" if table["primary_key"] else ""
        fk_str = (
            "  FK: " + ", ".join(
                f"{fk['column']} -> {fk['references_table']}.{fk['references_column']}"
                for fk in table["foreign_keys"]
            )
            if table["foreign_keys"]
            else ""
        )
        lines.append(f"TABLE {table['table_name']}")
        if table["table_description"]:
            lines.append(f"  {table['table_description']}")
        if pk_str:
            lines.append(pk_str)
        if fk_str:
            lines.append(fk_str)

        for col in table["columns"]:
            null_flag = "" if col["nullable"] else " NOT NULL"
            cat_flag = " [categorical]" if col["is_categorical"] else ""
            sample_str = (
                f"  samples={col['sample_values'][:3]}" if col["sample_values"] else ""
            )
            desc = f"  -- {col['description']}" if col["description"] else ""
            lines.append(
                f"  {col['name']:<35} {col['data_type']}{null_flag}{cat_flag}{sample_str}{desc}"
            )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    quiet = "--quiet" in sys.argv
    use_ai = "--no-ai" not in sys.argv

    cache = refresh_schema_cache(quiet=quiet, use_ai=use_ai)

    if not quiet:
        print("\n--- LLM prompt string preview (first 60 lines) ---")
        preview = schema_as_prompt_string(cache).splitlines()
        print("\n".join(preview[:60]))
        if len(preview) > 60:
            print(f"  ... ({len(preview) - 60} more lines)")
