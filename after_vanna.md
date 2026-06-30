# After Vanna — Production Deployment Guide (Samsung VM)

This guide picks up where local development ends. It walks you through deploying the Bixby Dashboard AI system on Samsung's VM against the real BigQuery database.

---

## Local Development Recap

The following build steps (from `.cursorrules` §9) have been completed locally:

| Step | Module | Status |
|------|--------|--------|
| 1 | `config.py` — all settings, DB_BACKEND flag, rollup table specs | Done |
| 2 | `db/dialect.py` + `db/sqlite_backend.py` — dialect abstraction layer | Done |
| 3 | `synthetic/generate.py` — generates `synthetic/local.db` with ~75 k synthetic conversations | Done |
| 4 | `schema/cache.py` — introspects SQLite and writes `schema_cache.json` | Done |
| 5 | `agents/chart_type.py` — rule-based chart type selector | Done |
| 6 | `agents/ui_designer.py` — rule-based 12-column layout engine | Done |
| 7 | `training/common_pairs.py` + `training/train_local.py` + `training/train_bigquery.py` — Vanna training pipeline (SQLite + BigQuery variants) | Done |

What **has not been built yet** (requires the VM or further dev work):

- `agents/planner.py`, `agents/data_wrangler.py`, `agents/diagnose.py`, `agents/insights.py`
- `agents/orchestrator.py` (LangGraph wiring)
- `agents/scheduler.py` (APScheduler bulk build + recurring jobs)
- `agents/dashboard_codegen.py` (Superset REST API)
- `api/server.py` (FastAPI)
- `db/bigquery_backend.py` (BigQueryDialect — written, not testable locally)

---

## Production Deployment Steps

### Step 1 — Clone the repo and install dependencies

```bash
git clone https://github.com/BAnSsal/samsung.git bixby-dashboard-ai
cd bixby-dashboard-ai
pip install -r requirements.txt --break-system-packages
```

> If `requirements.txt` is missing packages at runtime, install them individually with `--break-system-packages`. Do not use virtual environments unless the system already has one set up.

---

### Step 2 — Edit `config.py` for production

Open `config.py` and update every line marked `# CHANGE FOR PRODUCTION`:

```python
# Switch the database backend
DB_BACKEND = "bigquery"

# BigQuery connection (ADC handles auth — no JSON key needed)
BQ_PROJECT = "bixby2-analytics-dev"
BQ_DATASET = "bxb4_dw"

# Switch to vLLM (OpenAI-compatible endpoint on the GPU machine)
LLM_PROVIDER = "vllm"
LLM_BASE_URL  = "http://<gpu-machine-ip>:8000/v1"   # replace with actual IP
LLM_MODEL_NAME = "gpt-oss-20b"                       # or whatever model is loaded
LLM_API_KEY   = "not-needed"                          # set if the vLLM endpoint requires a key

# Gemini key — leave the existing value if you're using vLLM above, or replace
# with the production Gemini API key if keeping Gemini as the LLM.
# GEMINI_API_KEY = "your-production-key-here"         # only if LLM_PROVIDER = "gemini"

# Superset
SUPERSET_URL      = "http://<superset-host>:8088"     # replace with actual host
SUPERSET_USERNAME = "admin"                            # replace if different
SUPERSET_DATABASE_ID = 1                               # verify in Superset UI
```

> **Column mappings** — the following keys in `config.py` may need updating after Step 5 (schema verification):
> - `DEVICE_TYPE_COLUMN` (currently `"device_type"`) — verify this column exists in `bxb_dw`
> - `REGION_COLUMN` (currently `"region"`) — the real table may use `"country"` instead

---

### Step 3 — Authenticate with Google Cloud ADC

```bash
gcloud auth application-default login
```

Follow the browser prompt. This writes credentials to the default ADC path (`~/.config/gcloud/application_default_credentials.json`). The `google-cloud-bigquery` Python client discovers them automatically — no further configuration needed.

> If `gcloud` is not in PATH, verify it is installed: `gcloud --version`. If missing, install the Google Cloud SDK.

---

### Step 4 — Verify BigQuery access

Run a quick smoke test to confirm the credentials and project are working:

```bash
python -c "
from google.cloud import bigquery
c = bigquery.Client(project='bixby2-analytics-dev')
print('Datasets:', list(c.list_datasets())[:3])
"
```

Expected output: a list of at least `bxb4_dw` in the datasets. If you get an authentication error, re-run Step 3. If you get a permissions error, contact the GCP project owner.

---

### Step 5 — Verify the real schema

```bash
python -m schema.cache
```

This introspects `bixby2-analytics-dev.bxb4_dw` via BigQuery `INFORMATION_SCHEMA` and writes `schema/schema_cache.json`.

**After it runs, check:**

1. `schema_cache.json` contains a `"bxb_dw"` entry with approximately 161 columns.
2. Look for `device_type` in the column list:
   - **Found** → no action needed.
   - **Not found** → update `config.py`: `DEVICE_TYPE_COLUMN = "device_id"` (or the correct column name).
3. Look for `region` in the column list:
   - **Found** → no action needed.
   - **Not found** → update `config.py`: `REGION_COLUMN = "country"`.
4. Confirm `execution_result` appears as `ARRAY<STRING>` (not STRING). The `BigQueryDialect` already handles this with `UNNEST`, but verify the column is present.
5. Confirm `yyyymmddhh` is the partition column (type `TIMESTAMP`).

> If you update column mappings in Step 5, re-run `python -m schema.cache` to refresh `schema_cache.json`.

---

### Step 6 — Run Vanna BigQuery training

```bash
python -m training.train_bigquery
```

This script:
1. Reads BigQuery `INFORMATION_SCHEMA.COLUMNS` to get the live DDL for all 161 columns.
2. Trains Vanna on the hardcoded BigQuery DDL (rollup tables + known subset of `bxb_dw`).
3. Trains Vanna on the live INFORMATION_SCHEMA DDL.
4. Trains Vanna on 26+ business documentation strings explaining rules, additive-counts pattern, partition filters, and UNNEST patterns.
5. Trains Vanna on all BigQuery Q→SQL pairs (from `training/common_pairs.py`).

Training artifacts are stored in `chromadb_store/` (no data leaves the machine; the LLM is only called at query time, not during training).

Expected output ends with something like:
```
BigQuery training complete.
ChromaDB stored at: /path/to/chromadb_store
Stored training data: {'ddl': 4, 'documentation': 26, 'sql': 14}
```

> **Note on LLM_PROVIDER:** If `LLM_PROVIDER = "vllm"`, the script uses `vanna.openai.OpenAI_Chat` pointed at `LLM_BASE_URL`. If `LLM_PROVIDER = "gemini"`, it uses `GoogleGeminiChat`. The training itself (ChromaDB writes) works identically for both; only query-time SQL generation differs.

---

### Step 7 — Trigger bulk build of all 4 datamarts

> **WARNING: This step scans the full `bxb_dw` table (trillions of rows). It will be slow and potentially expensive. Run it once during initial setup. Subsequent incremental updates are handled by the Scheduler.**

```bash
python -m agents.scheduler --bulk-build
```

This builds (or rebuilds) the four pre-aggregated rollup tables:

| Table | Purpose |
|-------|---------|
| `daily_kpi_summary` | Success/failure trends, KPI averages by execution result |
| `daily_device_summary` | Device performance comparisons |
| `hourly_volume_summary` | Conversation volume by hour |
| `daily_region_summary` | Regional KPI comparison and geographic breakdowns |

After this completes, all agent queries should hit rollup tables rather than the raw trillion-row table.

> If `agents/scheduler.py` is not yet implemented, create it following the spec in `LLD.md`. The bulk build should use the BigQueryDialect's `CREATE OR REPLACE TABLE ... AS SELECT ...` pattern with partition filter on `yyyymmddhh`.

---

### Step 8 — Test SQL generation manually

Verify that Vanna generates correct BigQuery SQL for representative questions:

```python
python -c "
import sys, os
sys.path.insert(0, os.getcwd())
import config

# Make sure BigQuery config is set
assert config.DB_BACKEND == 'bigquery', 'Set DB_BACKEND=bigquery in config.py first'

from pathlib import Path
from vanna.chromadb import ChromaDB_VectorStore

chromadb_path = Path(config.CHROMADB_PATH)

if config.LLM_PROVIDER == 'vllm':
    from vanna.openai import OpenAI_Chat
    class BixbyVannaBQ(ChromaDB_VectorStore, OpenAI_Chat):
        def __init__(self, cfg):
            ChromaDB_VectorStore.__init__(self, config=cfg)
            OpenAI_Chat.__init__(self, config=cfg)
    vn = BixbyVannaBQ({
        'api_key': config.LLM_API_KEY,
        'model': config.LLM_MODEL_NAME,
        'base_url': config.LLM_BASE_URL,
        'path': str(chromadb_path),
    })
else:
    from vanna.google.gemini_chat import GoogleGeminiChat
    class BixbyVannaBQ(ChromaDB_VectorStore, GoogleGeminiChat):
        def __init__(self, cfg):
            ChromaDB_VectorStore.__init__(self, config=cfg)
            GoogleGeminiChat.__init__(self, config=cfg)
    vn = BixbyVannaBQ({
        'api_key': config.GEMINI_API_KEY,
        'model_name': config.GEMINI_MODEL,
        'path': str(chromadb_path),
    })

sql = vn.generate_sql('What is the success rate over the last 30 days?')
print('Generated SQL:')
print(sql)
"
```

**What to look for:**

- SQL targets `daily_kpi_summary`, **not** `bxb_dw` (rollup preference rule).
- Contains a `WHERE` clause with a date range (partition filter present).
- Uses `SUM(total_conversations)` and computes the rate by division (additive-counts rule, not averaging pre-computed rates).
- No `COUNTIF(execution_result = 'SUCCESS')` — that scalar pattern was replaced by the UNNEST pattern or rollup queries during training.

---

### Step 9 — Start the API server

```bash
python -m uvicorn api.server:app --host 0.0.0.0 --port 8000
```

Or, if `api/server.py` exports the app directly:

```bash
uvicorn api.server:app --host 0.0.0.0 --port 8000 --workers 1
```

The API will be available at `http://<vm-ip>:8000`. To confirm it started:

```bash
curl http://localhost:8000/health
```

Expected: `{"status": "ok"}` (or similar, depending on the FastAPI health endpoint implementation).

---

### Step 10 — Test end-to-end dashboard generation

Send a test prompt to the full pipeline:

```bash
curl -X POST http://localhost:8000/generate-dashboard \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Show me the KPI trends for the last 7 days"}'
```

Alternatively, to test a trend + breakdown:

```bash
curl -X POST http://localhost:8000/generate-dashboard \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Compare success rates by device type over the last 30 days"}'
```

**Expected response shape:**

```json
{
  "dashboard_url": "http://<superset-host>:8088/superset/dashboard/<id>",
  "charts": [...],
  "insights": ["...", "..."]
}
```

If the response is slow on the first request, that is expected — the first call warms the Vanna ChromaDB retrieval cache.

---

### Step 11 — Known gaps to validate on the VM

The following items were **not testable locally** and must be verified on the Samsung VM against the real BigQuery data:

| Item | What to Check | Action if Wrong |
|------|--------------|-----------------|
| `device_type` column | Does `bxb_dw` have a `device_type` column? (Step 5) | Update `DEVICE_TYPE_COLUMN` in `config.py`; retrain Vanna (Step 6) |
| `region` column | Does `bxb_dw` have a `region` column or only `country`? (Step 5) | Update `REGION_COLUMN` in `config.py`; retrain Vanna (Step 6) |
| `execution_result` ARRAY handling | Does `COUNTIF('SUCCESS' IN UNNEST(execution_result))` return correct counts? | Query directly in BigQuery console to verify; adjust `BigQueryDialect.array_contains()` if needed |
| Partition filter enforcement | Does BigQuery reject queries without `yyyymmddhh` filter? | Test: run a query without the filter in BQ console; confirm it fails; confirm the dialect always adds it |
| Conversation deduplication | Can one `conversation_id` span multiple `yyyymmddhh` hours? | If yes, add `DISTINCT conversation_id` deduplication to rollup queries; update `BigQueryDialect` |
| `kpi_completion` column | Does this numeric column exist? | Confirm via `schema_cache.json`; if missing, remove `sum_kpi_completion` from rollup specs |
| Real success rate | Real data shows ~35% SUCCESS (not the 80% some docs suggest) | Verify via BQ console: `SELECT COUNTIF('SUCCESS' IN UNNEST(execution_result)) / COUNT(*) FROM bxb_dw WHERE yyyymmddhh >= ...` |
| Partition column format | Is `yyyymmddhh` a TIMESTAMP or STRING? | The `BigQueryDialect.partition_filter()` uses `PARSE_TIMESTAMP('%Y%m%d%H', ...)` — verify this matches the actual stored format |
| Vanna SQL accuracy | Does Vanna generate correct BQ SQL for the 10 test questions in `common_pairs.py`? | Run each question from Step 8's snippet; compare generated SQL to expected SQL in `common_pairs.py` |

---

## Quick Reference

```bash
# Full setup sequence (first-time only)
git clone https://github.com/BAnSsal/samsung.git bixby-dashboard-ai
cd bixby-dashboard-ai
pip install -r requirements.txt --break-system-packages
# ... edit config.py ...
gcloud auth application-default login
python -m schema.cache
python -m training.train_bigquery
python -m agents.scheduler --bulk-build
uvicorn api.server:app --host 0.0.0.0 --port 8000

# Re-train Vanna (after schema changes or new Q→SQL pairs)
python -m training.train_bigquery

# Refresh schema cache only
python -m schema.cache

# Run incremental rollup update (daily cron)
python -m agents.scheduler --incremental

# Check training data stored in ChromaDB
python -c "
from training.train_bigquery import _build_vanna_bq
from pathlib import Path
import config
vn = _build_vanna_bq(Path(config.CHROMADB_PATH))
print(vn.get_training_data()['training_data_type'].value_counts())
"
```
