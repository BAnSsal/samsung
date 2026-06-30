# Local Run Guide — Setup to Vanna Test

Run every command from the **project root** (`bixby-dashboard-ai/`).  
All steps assume Windows with Python 3.12 and PowerShell.

---

## Step 0 — Prerequisites (one time)

```powershell
# Clone the repo (skip if already cloned)
git clone https://github.com/BAnSsal/samsung.git bixby-dashboard-ai
cd bixby-dashboard-ai

# Install all dependencies
pip install -r requirements.txt --break-system-packages
```

> **requirements.txt must include at minimum:**
> ```
> pandas
> numpy
> vanna==0.7.9
> chromadb
> google-generativeai
> ```
> If the file is missing any of these, add them and re-run the install.

---

## Step 1 — Set your Gemini API key in config.py

Open `config.py` and make sure line 65 looks like this (it should already be set):

```python
GEMINI_API_KEY: str = "YOUR_GEMINI_API_KEY_HERE"
GEMINI_MODEL:   str = "gemini-1.5-flash"
```

Also confirm:

```python
DB_BACKEND: str = "sqlite"
SQLITE_PATH: str = "synthetic/local.db"
LLM_PROVIDER: str = "gemini"
```

---

## Step 2 — Generate synthetic data

```powershell
python -m synthetic.generate
```

**Expected output:**
```
Generating 106,000 conversations over 365 days...
Building datamart rollups...
  building daily_kpi_summary...
  building daily_device_summary...
  building hourly_volume_summary...
  building daily_region_summary...

=== Row counts ===
  bxb_dw: 106,000
  execution_results: ~223,000
  daily_kpi_summary: ~2,349
  daily_device_summary: ~2,523
  hourly_volume_summary: ~8,175
  daily_region_summary: ~1,455

  Conversations with SUCCESS: ~37,050 (35.0%)
```

**Verify:** file `synthetic/local.db` now exists and is > 20 MB.

---

## Step 3 — Verify the dialect layer (optional sanity check)

```powershell
python tests/test_dialect.py
```

**Expected:** 5 queries print, all return numbers (total ~106 k conversations, success rate ~35%, etc.). No errors.

---

## Step 4 — Refresh the schema cache

```powershell
python -m schema.cache
```

**Expected output (last few lines):**
```
Schema cache written to .../schema/schema_cache.json
  backend : sqlite
  tables  : 6
    bxb_dw (9 cols) (AI-enriched)
    daily_device_summary (5 cols) (AI-enriched)
    ...
```

> The "AI-enriched" flag means fallback descriptions were applied (Gemini quota may be 0 on free tier — this is fine, all descriptions are still present).

**Verify:** open `schema/schema_cache.json` — you should see `table_description`, `primary_key`, `sample_values`, and `description` fields on every table and column.

---

## Step 5 — Train Vanna locally

This step stores DDL, documentation, and 17 Q→SQL pairs into the local ChromaDB vector store. **No Gemini quota is consumed here** — training only writes embeddings.

```powershell
python -m training.train_local
```

**Expected output:**
```
============================================================
Bixby Dashboard AI - Vanna local training
  ChromaDB path : ...\chromadb_store
  LLM model     : gemini-1.5-flash
  Reset store   : False
============================================================

[1/3] Training on SQLite DDL...
      DDL stored.

[2/3] Training on business documentation...
      doc 1/11 stored.
      ...
      doc 11/11 stored.

[3/3] Training on 17 Q->SQL pairs...
      [01/17] What is the overall Bixby success rate over the last 30 days?
      [02/17] Show me the daily success rate trend for the last 30 days
      ...
      [17/17] What is the success rate trend for the last 7 days including today?

Training complete.
ChromaDB path: ...\chromadb_store

Verifying stored training data...
  Training data in ChromaDB: {'sql': 17, 'documentation': 11, 'ddl': 2}
```

**Verify:** folder `chromadb_store/` now exists with `*.bin` / `*.pkl` / `index/` files inside.

> If you see errors and want to start fresh:
> ```powershell
> python -m training.train_local --reset
> ```

---

## Step 6 — Test Vanna SQL generation (the real test)

This step calls Gemini. It **will consume quota**. If your free-tier quota is 0, see the note below.

### Option A — Interactive REPL

```powershell
python -m training.train_local --ask
```

Type a natural-language question. Vanna retrieves similar training examples from ChromaDB, sends them as few-shot context to Gemini, and returns SQL which is immediately run against `local.db`.

**Test questions to try (in order of complexity):**

| Question | Expected target table |
|---|---|
| `What is the overall success rate?` | `daily_kpi_summary` |
| `Show the daily success rate trend for the last 30 days` | `daily_kpi_summary` |
| `Which device type has the highest success rate?` | `daily_device_summary` |
| `What are the peak conversation hours?` | `hourly_volume_summary` |
| `Compare success rates by region` | `daily_region_summary` |
| `How many conversations happened today?` | `bxb_dw` (raw — today not in rollup yet) |

**What to look for:**
- SQL targets the rollup table (not `bxb_dw`) for historical questions ✓
- Rate is computed as `SUM(successful) / SUM(total)`, never as AVG of a pre-stored rate ✓
- For "today" questions it hits `bxb_dw` directly or uses UNION ALL ✓

### Option B — Single Python snippet

```python
# Run from project root: python test_vanna_quick.py
import sys
sys.path.insert(0, ".")
from training.train_local import build_vanna
import sqlite3, pandas as pd, config
from pathlib import Path

vn = build_vanna()
question = "What is the overall Bixby success rate over the last 30 days?"
sql = vn.generate_sql(question=question)
print("Generated SQL:\n", sql)

con = sqlite3.connect(str(Path(config.SQLITE_PATH)))
print(pd.read_sql_query(sql, con))
con.close()
```

Run it:

```powershell
python test_vanna_quick.py
```

**Pass criteria:**
1. `sql` contains `daily_kpi_summary` (not `bxb_dw`)
2. Result DataFrame has columns `successful_conversations`, `total_conversations`, `success_rate_pct` (or similar)
3. Success rate is between 30–40% (matching synthetic data distribution)

---

## Step 7 — What a PASS looks like end-to-end

```
Question : What is the overall Bixby success rate over the last 30 days?

Generated SQL:
  SELECT
      SUM(CASE WHEN execution_result = 'SUCCESS' THEN total_conversations ELSE 0 END)  AS successful_conversations,
      SUM(total_conversations)                                                           AS total_conversations,
      ROUND(
          100.0 * SUM(CASE WHEN execution_result = 'SUCCESS' THEN total_conversations ELSE 0 END)
          / NULLIF(SUM(total_conversations), 0),
          2
      ) AS success_rate_pct
  FROM daily_kpi_summary
  WHERE day >= date('now', '-30 days')

Result:
  successful_conversations  total_conversations  success_rate_pct
                      3043                 8680             35.06
```

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'config'` | Running from wrong directory | `cd` to project root first |
| `local.db not found` | Synthetic data not generated | Run Step 2 |
| `Training data in ChromaDB: {}` | Training never ran or reset wiped store | Run `python -m training.train_local --reset` |
| `429 quota exceeded` on `--ask` | Gemini free-tier limit | Either wait (resets daily) or use a paid key; training itself still works |
| `ModuleNotFoundError: No module named 'vertexai'` | Wrong vanna import path | Verify `vanna==0.7.9` is installed; the code imports from `vanna.google.gemini_chat` not `vanna.google` |
| SQL hits `bxb_dw` instead of rollup | Retrieval not matching | Run `--reset` and retrain; check `chromadb_store/` has all 30 items |
| `database is locked` on Windows/OneDrive | OneDrive syncing `local.db` | Pause OneDrive sync temporarily or move `synthetic/` outside OneDrive |

---

## What's NOT tested here (requires Samsung VM)

- BigQuery connection and partition filter enforcement
- Real `execution_result` as `ARRAY<STRING>` (UNNEST pattern)
- Whether `device_type` and `region` columns exist in production schema
- `train_bigquery.py` — runs only on the VM with ADC credentials

See `after_vanna.md` for the full production deployment checklist.
