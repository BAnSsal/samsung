"""
Production Vanna training script — BigQuery backend.

THIS SCRIPT CANNOT RUN LOCALLY.
Run it on Samsung's VM after setting DB_BACKEND = "bigquery" in config.py.

Pre-requisites on Samsung VM
------------------------------
1. gcloud auth application-default login     (ADC credentials already set up)
2. pip install "vanna==0.7.9" chromadb google-generativeai google-cloud-bigquery
3. Edit config.py:
       DB_BACKEND    = "bigquery"
       BQ_PROJECT    = "bixby2-analytics-dev"
       BQ_DATASET    = "bxb4_dw"
       LLM_PROVIDER  = "vllm"              # switch to vLLM for production
       LLM_BASE_URL  = "http://<gpu>:8000/v1"
4. Run:  python -m training.train_bigquery

What this script trains on
---------------------------
1. BigQuery DDL         — CREATE TABLE statements for bxb_dw and the 4 datamarts
2. Live INFORMATION_SCHEMA DDL  — introspected at runtime from the real schema
3. Business documentation       — same rules as train_local.py (shared source)
4. BigQuery Q→SQL pairs         — from common_pairs.iter_bigquery_pairs()

Notes
------
- Training only writes to ChromaDB (local vector store). No BigQuery data is
  sent to any external service.
- The LLM is called only at QUERY TIME, not during training.
- If switching from Gemini (local dev) to vLLM (production), update LLM_PROVIDER
  and the LLM_ settings in config.py. The BixbyVanna class below must be updated
  to use the OpenAI-compatible client instead of GoogleGeminiChat.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config

# Guard — refuse to run locally.
if config.DB_BACKEND != "bigquery":
    print(
        "ERROR: This script requires DB_BACKEND = 'bigquery' in config.py.\n"
        "       For local training, use:  python -m training.train_local"
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Vanna class for production
#
# In production, the LLM is vLLM (OpenAI-compatible), not Gemini.
# Switch by changing LLM_PROVIDER in config.py and updating the mixin below.
#
# Option A — keep Gemini (if the Samsung VM has internet + Gemini API key):
#   from vanna.google.gemini_chat import GoogleGeminiChat
#   class BixbyVannaBQ(ChromaDB_VectorStore, GoogleGeminiChat): ...
#
# Option B — use vLLM / OpenAI-compatible endpoint (recommended for production):
#   from vanna.openai import OpenAI_Chat
#   class BixbyVannaBQ(ChromaDB_VectorStore, OpenAI_Chat): ...
#   config = {"api_key": config.LLM_API_KEY, "model": config.LLM_MODEL_NAME,
#             "base_url": config.LLM_BASE_URL, "path": str(chromadb_path)}
# ---------------------------------------------------------------------------

from vanna.chromadb import ChromaDB_VectorStore          # noqa: E402


def _build_vanna_bq(chromadb_path: Path):
    """
    Returns a Vanna instance configured for BigQuery.
    Automatically selects Gemini or vLLM based on config.LLM_PROVIDER.
    """
    if config.LLM_PROVIDER == "gemini":
        from vanna.google.gemini_chat import GoogleGeminiChat  # noqa: PLC0415

        class BixbyVannaBQ(ChromaDB_VectorStore, GoogleGeminiChat):
            def __init__(self, cfg):
                ChromaDB_VectorStore.__init__(self, config=cfg)
                GoogleGeminiChat.__init__(self, config=cfg)

        return BixbyVannaBQ({
            "api_key":    config.GEMINI_API_KEY,
            "model_name": config.GEMINI_MODEL,
            "temperature": 0.1,
            "path": str(chromadb_path),
        })

    elif config.LLM_PROVIDER == "vllm":
        from vanna.openai import OpenAI_Chat  # noqa: PLC0415

        class BixbyVannaBQ(ChromaDB_VectorStore, OpenAI_Chat):  # type: ignore[no-redef]
            def __init__(self, cfg):
                ChromaDB_VectorStore.__init__(self, config=cfg)
                OpenAI_Chat.__init__(self, config=cfg)

        return BixbyVannaBQ({
            "api_key":    config.LLM_API_KEY,
            "model":      config.LLM_MODEL_NAME,
            "base_url":   config.LLM_BASE_URL,
            "temperature": 0.1,
            "path": str(chromadb_path),
        })

    else:
        raise ValueError(f"Unsupported LLM_PROVIDER: {config.LLM_PROVIDER!r}")


# ---------------------------------------------------------------------------
# Introspect live BigQuery schema for supplemental DDL
# ---------------------------------------------------------------------------

def _introspect_bq_ddl() -> str:
    """
    Pull CREATE TABLE–style DDL from BigQuery INFORMATION_SCHEMA.
    This captures ALL 161 columns of bxb_dw as they actually exist in production,
    which is more accurate than the hardcoded DDL in common_pairs.py.
    """
    from google.cloud import bigquery  # noqa: PLC0415

    client = bigquery.Client(project=config.BQ_PROJECT)
    sql = f"""
    SELECT
        table_name,
        column_name,
        data_type,
        is_nullable
    FROM `{config.BQ_PROJECT}.{config.BQ_DATASET}.INFORMATION_SCHEMA.COLUMNS`
    ORDER BY table_name, ordinal_position
    """
    rows = list(client.query(sql).result())

    ddl_parts: dict[str, list[str]] = {}
    for row in rows:
        tname = row["table_name"]
        nullable = "NULL" if row["is_nullable"] == "YES" else "NOT NULL"
        col_def = f"    {row['column_name']} {row['data_type']} {nullable}"
        ddl_parts.setdefault(tname, []).append(col_def)

    lines = []
    for tname, cols in ddl_parts.items():
        lines.append(
            f"CREATE TABLE `{config.BQ_PROJECT}.{config.BQ_DATASET}.{tname}` (\n"
            + ",\n".join(cols)
            + "\n);"
        )
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Training run
# ---------------------------------------------------------------------------

def train_bigquery() -> None:
    chromadb_path = _ROOT / config.CHROMADB_PATH
    chromadb_path.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Bixby Dashboard AI — Vanna BigQuery training")
    print(f"  Project      : {config.BQ_PROJECT}")
    print(f"  Dataset      : {config.BQ_DATASET}")
    print(f"  LLM provider : {config.LLM_PROVIDER}")
    print(f"  ChromaDB     : {chromadb_path}")
    print("=" * 60)

    vn = _build_vanna_bq(chromadb_path)

    from training.common_pairs import DDL_BIGQUERY, DOCUMENTATION, iter_bigquery_pairs

    # 1. Hardcoded BigQuery DDL (rollup tables + known schema)
    print("\n[1/4] Training on hardcoded BigQuery DDL…")
    vn.train(ddl=DDL_BIGQUERY)

    # 2. Live INFORMATION_SCHEMA DDL (bxb_dw real 161 columns)
    print("\n[2/4] Introspecting live BigQuery schema for supplemental DDL…")
    try:
        live_ddl = _introspect_bq_ddl()
        vn.train(ddl=live_ddl)
        print(f"      Live DDL ({len(live_ddl)} chars) stored.")
    except Exception as exc:  # noqa: BLE001
        print(f"      WARNING: Could not introspect live schema — {exc}")
        print("      Continuing with hardcoded DDL only.")

    # 3. Documentation
    print(f"\n[3/4] Training on {len(DOCUMENTATION)} documentation strings…")
    for i, doc in enumerate(DOCUMENTATION, 1):
        vn.train(documentation=doc)
        print(f"      doc {i}/{len(DOCUMENTATION)} stored.")

    # 4. Q→SQL pairs (BigQuery dialect)
    pairs = iter_bigquery_pairs()
    print(f"\n[4/4] Training on {len(pairs)} BigQuery Q→SQL pairs…")
    for i, (question, sql) in enumerate(pairs, 1):
        vn.train(question=question, sql=sql)
        print(f"      [{i:02d}/{len(pairs)}] {question[:70]}")

    print("\nBigQuery training complete.")
    print(f"ChromaDB stored at: {chromadb_path}")

    # Verification
    try:
        data = vn.get_training_data()
        if data is not None and not data.empty:
            counts = data["training_data_type"].value_counts().to_dict()
            print(f"Stored training data: {counts}")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    train_bigquery()
