# Bixby Dashboard AI — Handoff Context for New Chat

Paste this whole document into a new chat to continue the design work without re-explaining everything.

---

## What's Being Built

A natural-language → live Superset dashboard system for Samsung Bixby production data on BigQuery, used by a small Samsung India team (10–50 users).

A user types a request (vague like *"how are KPIs doing"* or specific like *"first chart should be total conversations"*), and the system reasons through it, queries pre-aggregated BigQuery datamarts (unioned with today's live data when needed), decides chart types using deterministic rules, lays them out, and publishes a finished Superset dashboard — without manual chart configuration.

## Hard Constraints

- **Data scale:** trillions of rows in the raw BigQuery table. Cannot scan it per-question.
- **Team scale:** 10–50 users. Single VM is sufficient.
- **Containers not allowed.** No Docker, no Kubernetes. Native OS-supervised Python service only.
- **Open data access** within the team — no role-based access control needed.
- **One BigQuery database now** but may change later — system must be database-swappable via config + retraining only, never code change.
- **Cannot take data out of Samsung's environment.** Developer is building locally in Cursor with no access to real BigQuery. Must use synthetic data in a local SQLite (or DuckDB) setup, then deploy code (not data) into Samsung's system where it points at real BigQuery.
- **GPU machine separate from CPU VM.** Local GPT OSS 20B served via vLLM on the GPU machine, 24GB VRAM at 4-bit quantization is enough.

## Stack Decisions Already Locked In

| Layer | Choice | Reason |
|---|---|---|
| Agent orchestration | **LangGraph** | Branches, retries, pauses for clarification — fits sequential agent flow with conditional logic. CrewAI/AutoGen rejected as they're for multi-agent debate, not sequential branching. |
| Text-to-SQL | **Vanna AI** (MIT, open-source) | Purpose-built; runs locally; learns from past queries; framework only, plug any LLM in. |
| Vector memory for Vanna | **ChromaDB** local | Stores schema + past Q→SQL pairs on disk. |
| LLM | **GPT OSS 20B** locally via vLLM | OpenAI-compatible API so Vanna and agents work unchanged. ONE model serves every AI-driven agent. |
| Web service | **FastAPI** | Lightweight, async-capable. |
| Scheduler | **APScheduler** in-process | Airflow/Prefect rejected — overkill at this scale, no extra infra needed. |
| Process supervision | **systemd** on Linux / **Task Scheduler** on Windows | Built into OS, no extra tools. |
| BigQuery client | google-cloud-bigquery |

## The Datamart Strategy (CRITICAL)

This is the foundation of the design because of the trillion-row scale. Skipping or skimping on this breaks everything.

**Approach: Option 1 — Per-question-type rollup datamarts** (out of 5 options we discussed; user committed to this one).

**Four datamarts maintained:**

| Datamart | Grain | Columns |
|---|---|---|
| `daily_kpi_summary` | (day, execution_result) | day, execution_result, total_conversations, sum_kpi_completion |
| `daily_device_summary` | (day, device_type) | day, device_type, total_conversations, successful_conversations, sum_kpi_completion |
| `hourly_volume_summary` | (hour) | hour_timestamp, total_conversations |
| `daily_region_summary` | (day, region) | day, region, total_conversations, successful_conversations, sum_kpi_completion |

**Naming convention (strict):** `<grain>_<subject>_summary`. Each name appears as a string in **exactly one place** — the config spec. Never as a string literal anywhere else. This prevents the silent-duplicate-table failure mode.

**Additive-counts principle:** stores raw counts/sums, never pre-computed rates. Rates computed at query time. Averaging stored rates is mathematically wrong (Day 1 of 100 conversations at 90% + Day 2 of 10,000 at 50% does NOT average to 70%).

**Lifecycle:**
1. **Bulk build once:** scans full raw history. Must NOT be filtered to a recent slice — otherwise long-range queries silently undercount.
2. **Nightly incremental append:** scans only yesterday, INSERT one new row per group. Historical rows never re-touched.
3. **Query-time UNION pattern:** for questions covering today, the Data Wrangler's SQL unions completed days from rollup with a tight `WHERE DATE(local_timestamp) = today` query against raw.

**Existence check (Approach 1 — sufficient at this scale):**
```sql
SELECT 1 FROM `project.dataset.INFORMATION_SCHEMA.TABLES`
WHERE table_name = '{rollup_name_from_config}'
```
If row returned → run incremental append. If not → bulk build. The single source-of-truth `name` field in config makes this reliable.

**Partitioning:** each rollup partitioned by the `day` column (or `hour_timestamp`).

**Boundary constraint:** conversations must belong to exactly one row — i.e., timestamps must be single points (conversation start), not spans. Worth verifying with raw-schema owner.

## Agent Architecture

```
INPUT (prompt, data sources, deployment config, user preference)
        ↓
ORCHESTRATION AGENT (LangGraph state machine — conductor, no LLM reasoning itself)
   ├─→ Planner Agent (LLM) — decomposes intent into sub-questions, handles pinned charts
   │      ↓ (conditional: needs clarification?)
   ├─→ Ask-Clarification Node — suspends graph, waits for user reply
   │
   │   PER SUB-QUESTION LOOP:
   ├─→ Data Wrangler Agent (Vanna + LLM) — generates SQL preferring rollups + today-UNION
   │      ↓ (conditional: SQL succeeded?)
   ├─→ Diagnose Agent (LLM) — reasons about SQL error, feeds diagnosis back as retry context
   │   (retries up to 3, then skips this sub-question)
   ├─→ Chart Type Agent (RULES, no LLM) — DataFrame shape → viz type
   │      ↓ (conditional: more sub-questions?)
   │
   ├─→ UI Designer Agent (RULES, no LLM) — 12-column grid, same row = same height
   ├─→ Dashboard Code Gen Agent (Superset REST API)
   └─→ Insights Agent (LLM) — 2-4 plain-language observations
        ↓
OUTPUT (Superset dashboard URL + insights)

PARALLEL:
SCHEDULER AGENT (APScheduler in-process)
   ├─→ Daily schema cache refresh
   ├─→ Nightly per-rollup: existence check → bulk build OR incremental append
   └─→ Recurring user dashboards (cron-triggered full runs)

SUPPORTING:
- Schema Awareness Component (cached daily, fetched from INFORMATION_SCHEMA)
- Pre-Aggregation Layer (the 4 datamarts themselves)
- Audit Logger (timestamped plain text logs, daily rotated)
- OS supervisor (systemd or Task Scheduler)
```

## OPEN DECISION — Data Profiler Agent (NEW, not yet committed)

In the most recent message, user raised the valid point that *"a real analyst doesn't just look at column types, they peek at actual data values to decide what makes sense."* This is currently a gap — the Planner only sees schema, not actual values, before deciding charts.

**Proposed addition: Data Profiler Agent**, slots between Planner and Data Wrangler. For each sub-question, runs cheap profiling queries (against rollups when possible, tiny TABLESAMPLE from raw otherwise):
- min/max/avg/percentiles for numeric columns
- distinct count + top values + distribution for categorical columns
- date range + span for temporal columns
- null percentage
- sample rows
- skew detection

Profile then refines the Planner's chart decisions — e.g. *"execution_result is 82/18 split"* → suggest a headline KPI + a pie; *"device_type has 47 values, top 3 cover 85%"* → top 5 + Other bucket; *"kpi_completion heavily skewed"* → distribution chart instead of just average.

**Status:** offered to user, awaiting yes/no to fold into the architecture. User has indicated interest in this analyst-like behavior. Final task in last message: *"Want me to update the README and LLD to formally add the Data Profiler Agent — including Mermaid diagram, per-agent LLD section, profiling queries, how Planner consumes profile, and build/verify step?"* — user is expected to say yes.

## Local Development Approach (DECIDED)

Developer cannot access real BigQuery from local machine. Solution:

1. **Local dev:** SQLite (or DuckDB) with **fully synthetic data** generated by a small Python script. Schema mirrors production, distributions deliberately realistic (80/20 Success/Failure, KPI skewed high with low-end tail, dominant + rare device types, etc.).
2. **Dialect abstraction:** thin module wraps the 5-6 SQL fragments that differ between BigQuery and SQLite (`TIMESTAMP_SUB` vs `datetime('now', '-30 days')`, `COUNTIF` vs `COUNT(*) FILTER (WHERE ...)`, etc.). Rest of code is dialect-agnostic.
3. **Two Vanna training scripts:** one for local SQLite schema, one for real BigQuery schema. Same example Q→SQL pairs in both; only the SQL dialect differs. Driven by config flag.
4. **Deploy:** code (not data) goes into Samsung's system. There, real BigQuery training script runs. Same orchestrator code now runs against real data.

**Critical clarification on Vanna training:** Vanna trains on **schema metadata only** — DDL, business documentation, and hand-written Q→SQL example pairs. It does NOT train on row values. So nothing sensitive ever needs to leave Samsung's environment in the trained model. Local synthetic data is only used for testing the rest of the orchestrator and verifying example SQL doesn't have typos.

## Other Decisions Worth Remembering

- **Vanna is a framework, not a model.** It plugs into whatever LLM is configured. For this system, all agents share ONE local GPT OSS 20B via vLLM.
- **Schema awareness updates daily** — handles new/added columns automatically. New tables auto-detected. Renamed columns require manual cleanup of stale ChromaDB entries OR wiping and re-training (additive ChromaDB doesn't self-clean).
- **New data daily ≠ retraining needed.** Vanna doesn't store row data, so adding rows never requires retraining. Only schema changes do.
- **Audit logs:** plain text, daily rotated. At this scale a database for logs would be overkill. Logs include every agent's decisions, all external calls (BigQuery, Superset, LLM) with timing, and full error traces — all interleaved by timestamp.
- **Multi-turn refinement** is supported via LangGraph's checkpointer keyed by `thread_id` — follow-up requests resume mid-graph.
- **"First chart should be X" pinning** is in scope — Planner respects pinned charts in declared order, fills in remaining slots with own judgment.
- **Chart Type Agent grain adjustment:** for time spans >1 year, prefer month/quarter buckets over daily.

## What's Already Been Written

Two markdown deliverables exist (these are the working docs for the senior):

1. **README.md** — architecture overview, datamart catalog, Mermaid agent diagram, framework decision rationale, per-agent summary table, setup & operation, what's deliberately excluded.

2. **LLD.md** — input layer (config module structure, HTTP contract, naming discipline), orchestration layer (state object, graph structure, persistence, failure modes), per-agent LLD (12 sections covering each agent's inputs/outputs/dependencies/behavior), output layer, cross-cutting concerns (logging, secrets, concurrency, data volumes, failure visibility, multi-database readiness), build & verify sequence (23 steps).

Both documents incorporate every decision listed above. Both kept code-free and container-free.

## What's Deliberately Excluded (Don't Re-Suggest)

- Kubernetes / Docker Swarm / any container orchestration
- Docker / Podman containers
- Airflow / Prefect / Dagster
- CrewAI / AutoGen multi-agent frameworks
- Celery / RabbitMQ / Redis task queues
- Role-based access control
- Separate audit database
- Multiple LLMs (only one shared model)
- Multi-region or HA setup

## What to Do First in the New Chat

The pending action is: **fold the Data Profiler Agent into the README and LLD** (Mermaid diagram update, new per-agent LLD section, profiling queries documented, how Planner consumes profile, build/verify step). User is expected to confirm yes — once confirmed, regenerate both documents with this addition.

After that, likely next topic the user wanted to address: **synthetic data generator design** — what distributions, value ranges, edge cases to include so local testing realistically exercises the Data Profiler and the rest of the system.

---

End of handoff context.
