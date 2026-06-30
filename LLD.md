# Low-Level Design (LLD) — Bixby Dashboard AI

This is the implementation-level companion to the README. The README covers the *what* and *why*. This document covers the *how* — concrete data shapes, control flow, agent contracts, the trillion-row pre-aggregation strategy, and the operational disciplines that keep the system reliable.

---

## 1. System Context

### Actors
- **End user (Samsung India team member)** — submits a natural-language prompt via HTTP and receives a Superset dashboard URL.
- **Scheduler (internal)** — submits prompts on a recurring schedule without a human in the loop.
- **BigQuery** — the data source (raw + datamart rollup tables).
- **Superset** — the dashboard rendering and serving layer (already deployed separately).
- **Local GPT OSS 20B model server** — hosts the language model used by every AI-driven agent, exposed over an OpenAI-compatible HTTP API, running on a separate GPU machine.

### Deployment Topology
- One CPU VM running the orchestration service.
- One GPU machine hosting the GPT OSS 20B model (24GB VRAM at 4-bit quantization is sufficient for this team size).
- One OS-level supervisor (systemd on Linux, Task Scheduler on Windows) keeping the orchestration service alive.
- BigQuery and Superset reached over HTTPS.

### Database Multiplicity
The system currently connects to one BigQuery database. The design treats this as a configurable connection — switching databases requires changing config values and re-running the Vanna training script, but no code changes anywhere else.

---

## 2. Input Layer — LLD

### 2.1 Input Sources

| Input | Origin | Storage Format | Used By |
|---|---|---|---|
| Business Requirement (prompt) | HTTP request body | Plain string field in JSON | Planner Agent |
| Data Sources | BigQuery (live introspection) | Cached as JSON file on disk, refreshed daily | Planner Agent, Data Wrangler Agent |
| Deployment Config | Single Python config module on disk | Named constants | Every component |
| User Preference | Optional fields in HTTP body | Same JSON payload as the prompt | Planner Agent |

### 2.2 HTTP Request Contract

A request to `/generate-dashboard` carries:
- `prompt` — required, the natural-language dashboard request.
- `thread_id` — optional. When omitted, the server generates one. When present, the request resumes an earlier paused conversation.
- `preferences` — optional. Time range hints, pinned chart hints, layout density.

A response always carries:
- `status` — one of `done`, `needs_clarification`, `failed`.
- `thread_id`.
- `dashboard_url` — present only when `status == done`.
- `clarification_question` — present only when `status == needs_clarification`.
- `insights` — list of `{title, detail}`, present when `status == done`.

### 2.3 Configuration Module Contents

All deployment-time settings live in one Python config module — never read directly from environment variables inside agent code. Fields:

- BigQuery project ID, dataset name, path to service-account credentials.
- Superset URL, login, database connection ID.
- Local model endpoint URL and model name.
- File paths for the schema cache, schedules file, log directory, ChromaDB store.
- API host and port.
- Maximum SQL retry count.
- **Rollup table specs** — for each datamart: `name` (single source of truth — must match the actual BigQuery table exactly), grain columns, source table, aggregation expression.

### 2.4 The Rollup Naming Discipline

This is operational hygiene worth pinning down as a hard rule, because the failure mode it prevents is silent and expensive.

**Rule:** every rollup table name appears as a string in **exactly one place** — the `name` field of its spec in the config module. Every other reference (Scheduler existence check, bulk build SQL, incremental append SQL, Vanna training documentation, audit log entries, Data Wrangler runtime SQL) reads from that variable.

**Why this matters:** if the existence check looks for `kpi_summary_daily` while the actual table is `daily_kpi_summary`, the check returns "not found," the Scheduler dutifully bulk-builds again under the new name, and BigQuery now holds two tables with the same data under different names. Storage doubles, queries become ambiguous, and the divergence is invisible without manual inspection.

**Naming convention:** `<grain>_<subject>_summary` — e.g. `daily_kpi_summary`. Pick this once, stick to it for every rollup added later. The convention itself matters less than the consistency.

---

## 3. Orchestration Layer — LLD

### 3.1 Shared State Object

One state dictionary flows through orchestration. Every agent reads relevant fields and writes back only fields it modifies. Fields:

- `prompt` — the original prompt plus any clarification merged in.
- `interpreted_intent` — Planner's summary of what the user actually wants.
- `needs_clarification` — bool.
- `clarification_question` — present only when needs_clarification.
- `sub_questions` — list of `{question, purpose, pinned}` dicts.
- `current_question_index` — pointer into sub_questions.
- `current_sql` — the SQL currently being attempted.
- `last_sql_error` — most recent error string, or None.
- `sql_attempts` — retry counter for the current sub-question.
- `chart_results` — growing list of `{sql, dataframe_meta, chart_config, title}` per completed sub-question.
- `layouts` — per-chart `{row, width, height}`.
- `chart_ids` — list of Superset chart IDs created.
- `dashboard_id` — final Superset dashboard ID.
- `insights` — list of `{title, detail}` from the Insights Agent.
- `status` — `running` | `awaiting_clarification` | `done` | `failed`.

### 3.2 Graph Structure (LangGraph)

Nodes are agents; edges are control transitions.

- **Entry:** Planner Agent.
- **After Planner — conditional branch:**
  - if `needs_clarification` → Ask-Clarification node (suspends).
  - else → Data Wrangler.
- **After Data Wrangler — conditional branch:**
  - On SQL success → Chart Type Agent.
  - On SQL failure within retry budget → Diagnose Agent.
  - On retry budget exhausted → skip this question, continue to "more questions?" check.
- **After Diagnose** — always returns to Data Wrangler with the diagnosis appended to context.
- **After Chart Type — conditional branch:**
  - If more sub-questions remain → loop back to Data Wrangler with the next question.
  - Else → UI Designer.
- UI Designer → Dashboard Code Gen → Insights → END.

### 3.3 Persistence and Resumption

The graph uses a checkpointer keyed by `thread_id`. State is snapshotted at every node boundary. This allows:
- The clarification-pause to survive a process restart.
- Multi-turn refinement — a follow-up request with the same `thread_id` resumes mid-graph.

At this scale an in-memory checkpointer is sufficient. A file-backed or SQLite-backed checkpointer can be swapped in via a single configuration change later.

### 3.4 Failure Modes Handled

- **Repeated SQL failure for one sub-question** — skip it, log the failure, continue with the others.
- **Planner returns unparsable output** — retry once with corrective instruction; on second failure, return `failed`.
- **Superset API call fails** — retry twice with backoff. If still failing, return `failed`.
- **Model server unreachable** — fail fast with a clear message.

---

## 4. Per-Agent LLD

### 4.1 Planner Agent

- **Inputs from state:** `prompt`, the cached schema context, any `preferences`.
- **Outputs to state:** `interpreted_intent`, `needs_clarification`, optionally `clarification_question`, `sub_questions` (each with `question`, `purpose`, `pinned`).
- **External dependencies:** GPT OSS 20B endpoint, schema cache.
- **Behavior:** sends one LLM call with a system prompt combining (a) Chain-of-Thought intent interpretation, (b) pinned-chart respect, (c) column grounding. Output is JSON-parsed. On parse failure, one retry with a corrective instruction.
- **Domain genericity:** the system prompt references the database/domain by name pulled from config — never hardcoded. Changing the prompt to a new domain is a config value change.

### 4.2 Ask-Clarification Node

- **Inputs from state:** `clarification_question`.
- **Outputs to state:** `status = "awaiting_clarification"`. The graph suspends.
- **External dependencies:** the checkpointer.
- **Behavior:** writes the clarification question into the HTTP response and stops. When a follow-up request arrives with the same `thread_id` plus an answer field, the orchestrator merges the answer into `prompt` and re-runs the Planner.

### 4.3 Data Wrangler Agent

- **Inputs from state:** the current sub-question, schema context, the rollup table catalog, most recent `last_sql_error` if this is a retry.
- **Outputs to state:** `current_sql`, the executed DataFrame metadata, `last_sql_error = None` on success.
- **External dependencies:** Vanna AI client (which depends on GPT OSS 20B and the ChromaDB store), the BigQuery client.
- **Behavior:**
  - Calls Vanna's text-to-SQL function with the sub-question.
  - On retry, the previous SQL and error message are prepended to the question as context.
  - Vanna's training instructs it to **prefer the rollup datamarts** for trend/aggregate questions, and to add a `UNION ALL` with today's data from the raw table when the question's time range includes today.
  - Returned SQL is executed against BigQuery.
  - On successful execution, `vn.train(question, sql)` adds this successful pair to ChromaDB so future similar questions benefit.

### 4.4 Diagnose Agent

- **Inputs from state:** the current sub-question, `current_sql`, `last_sql_error`.
- **Outputs to state:** an augmented `last_sql_error` with a one-sentence diagnosis appended.
- **External dependencies:** GPT OSS 20B endpoint.
- **Behavior:** one focused LLM call: *"What likely went wrong? One sentence."* The diagnosis is not used to write a corrected SQL directly — it is fed back into the Data Wrangler as additional context so the model produces a corrected query on the next attempt.

### 4.5 Chart Type Agent (Framework Selection)

- **Inputs from state:** the executed DataFrame (shape + column dtypes).
- **Outputs to state:** appends to `chart_results`: `{chart_type, metric_col, groupby_cols, x_axis_col, number_format, title}`.
- **External dependencies:** none beyond pandas.
- **Behavior:** pure rules — no LLM call. Classifies each column as numeric/categorical/temporal. Counts each type. Matches against an ordered ruleset (first match wins):
  - 1 row, 1 numeric, 0 categorical → big number.
  - Has time + has numeric → line chart.
  - 1 categorical + 1 numeric + categorical has ≤5 unique values → pie.
  - 1 categorical + 1 numeric + categorical has >5 unique values → bar.
  - 2 categorical + 1 numeric → stacked bar.
  - Fallback → table.
- **Grain adjustment for long time spans:** if the result's time range exceeds ~1 year, prefer month or quarter buckets rather than daily — improves both query speed and chart readability.
- Number format is inferred from value range.

### 4.6 UI Designer Agent

- **Inputs from state:** full `chart_results` list.
- **Outputs to state:** `layouts` — per-chart `{row, width, height}`.
- **External dependencies:** none.
- **Behavior:** pure rules — no LLM call. Width/height per chart type from a fixed map. Charts placed left-to-right, sum of widths capped at 12 per row; overflow starts a new row. All charts in the same row get the same height.

### 4.7 Dashboard Code Gen Agent

- **Inputs from state:** `chart_results`, `layouts`, dashboard title (derived from `interpreted_intent`).
- **Outputs to state:** `chart_ids`, `dashboard_id`.
- **External dependencies:** Superset REST API.
- **Behavior:**
  - Authenticates to Superset (login → CSRF token → session cookie).
  - For each chart: creates a virtual dataset (SQL is the wrangler's query), then creates a chart from that dataset with the chart configuration.
  - Creates an empty dashboard with a slug guaranteed unique by appending a timestamp.
  - Builds the position JSON and attaches it.
  - Retries up to twice on transient HTTP errors.

### 4.8 Insights Agent

- **Inputs from state:** `chart_results` and a small sample of each chart's underlying data.
- **Outputs to state:** `insights` — list of `{title, detail}`.
- **External dependencies:** GPT OSS 20B endpoint.
- **Behavior:** one focused LLM call asking for 2-4 plain-language observations referencing actual numbers from the data.

### 4.9 Scheduler Agent

This is the most operationally critical agent in the system because it owns the datamart lifecycle. Detailed below.

#### 4.9.1 Jobs

- **Daily schema refresh** (1 AM) — re-introspects BigQuery's INFORMATION_SCHEMA, rewrites the schema cache file.
- **Nightly rollup maintenance** (per rollup, runs after schema refresh) — described in detail below.
- **Recurring user dashboards** — invokes the same orchestration graph with the saved prompt, on the user's specified cron.

#### 4.9.2 Existence Check Approach (the discipline)

For each rollup in `config.rollup_table_specs`, the Scheduler reads the **single source-of-truth `name`** from the spec and uses it in three places, every time, with no string-literal duplication:

1. The existence check.
2. The bulk build SQL's target table.
3. The incremental append SQL's target table.

This is what makes the existence check reliable. If a string literal anywhere drifted (`kpi_summary_daily` vs `daily_kpi_summary`), the existence check would mis-report and silent duplication would result — see section 2.4 for the failure mode.

#### 4.9.3 Bulk vs Incremental Decision

Per rollup, every night:

```
existence_check_sql:
    SELECT 1
    FROM `{project}.{dataset}.INFORMATION_SCHEMA.TABLES`
    WHERE table_name = '{rollup_name_from_config}'
```

Decision table:

| Existence Check Result | Action |
|---|---|
| Row returned (table exists) | Run **incremental append** of yesterday |
| Empty result (table missing) | Run **bulk build** covering full available history |

This is the existence-check-only approach (Approach 1 from the design discussion). It's sufficient for this scale because:
- The Scheduler runs nightly and is the only writer to these tables.
- Failures in the append job are logged with full error detail.
- Manual table truncation by a developer is rare and would be visible in the operator's audit log.

If operational experience reveals silent-staleness gaps (e.g. an append failed without anyone noticing), upgrading to a freshness check (`MAX(day)` comparison) or a tracking table is a small, isolated change to this one agent.

#### 4.9.4 Bulk Build SQL Pattern

For each rollup:

```sql
CREATE OR REPLACE TABLE `{project}.{dataset}.{rollup_name}` AS
SELECT
    DATE(local_timestamp) AS day,
    {grain_columns},
    COUNT(*) AS total_conversations,
    COUNTIF(execution_result = 'Success') AS successful_conversations,
    SUM(kpi_completion) AS sum_kpi_completion
FROM `{project}.{dataset}.{raw_table}`
GROUP BY day, {grain_columns}
```

**Critical requirement:** the bulk build SQL **must not** be filtered to a recent window. It must cover full history. Otherwise long-range questions (e.g. 7-year totals) silently undercount because the missing days have no rollup rows, and the Data Wrangler's queries return partial results with no error thrown.

#### 4.9.5 Incremental Append SQL Pattern

For each rollup, nightly:

```sql
INSERT INTO `{project}.{dataset}.{rollup_name}`
SELECT
    DATE(local_timestamp) AS day,
    {grain_columns},
    COUNT(*),
    COUNTIF(execution_result = 'Success'),
    SUM(kpi_completion)
FROM `{project}.{dataset}.{raw_table}`
WHERE DATE(local_timestamp) = DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
GROUP BY day, {grain_columns}
```

This scans exactly one day of raw data, not history.

#### 4.9.6 Query-Time UNION (Data Wrangler reads, not Scheduler writes)

The Data Wrangler's runtime SQL combines completed days from the rollup with today's live partial day from the raw table:

```sql
SELECT day, ... FROM `{project}.{dataset}.{rollup_name}`
WHERE day BETWEEN @start AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
UNION ALL
SELECT CURRENT_DATE() AS day, ... FROM `{project}.{dataset}.{raw_table}`
WHERE DATE(local_timestamp) = CURRENT_DATE()
GROUP BY ...
```

This pattern is documented in Vanna's training (per LLD 4.3) so the model generates it correctly.

### 4.10 Schema Awareness Component

- **Inputs:** BigQuery project ID, dataset name from config.
- **Outputs:** schema cache JSON file containing all tables → columns → types, plus a readable string form for LLM prompts.
- **External dependencies:** BigQuery client.
- **Behavior:** not an LLM-driven agent. Re-fetched daily by the Scheduler. Read on demand by the Planner and indirectly by the Data Wrangler.

### 4.11 Pre-Aggregation Layer (Datamart Catalog)

- **Inputs:** raw BigQuery table(s); rollup specs from config.
- **Outputs:** the four datamart tables themselves in BigQuery.
- **External dependencies:** BigQuery, the Scheduler Agent.

#### 4.11.1 The Four Datamarts

**Naming convention:** `<grain>_<subject>_summary`. Each name appears as a string literal exactly once — in the config spec.

**Additive-counts principle:** each row stores raw counts and sums, never pre-computed rates. Rates are computed at query time after summing the relevant rows. This is the only way multi-day aggregations come out mathematically correct.

| Datamart | Grain | Columns | Answers |
|---|---|---|---|
| `daily_kpi_summary` | (day, execution_result) | day, execution_result, total_conversations, sum_kpi_completion | Success/failure trends and KPI averages |
| `daily_device_summary` | (day, device_type) | day, device_type, total_conversations, successful_conversations, sum_kpi_completion | Device performance comparisons |
| `hourly_volume_summary` | (hour) | hour_timestamp, total_conversations | Hour-of-day volume patterns |
| `daily_region_summary` | (day, region) | day, region, total_conversations, successful_conversations, sum_kpi_completion | Regional comparisons |

#### 4.11.2 Why Raw Counts and Not Rates

If Day 1 has 100 conversations at 90% success and Day 2 has 10,000 conversations at 50% success, averaging the two stored rates gives 70% — which is wrong. The correct combined rate is `(90 + 5000) / (100 + 10000) ≈ 50.4%`. Storing raw counts and dividing at query time after summing across the requested range is the only correctness-preserving approach.

#### 4.11.3 BigQuery Partitioning

Each rollup table is **partitioned by the `day` column** (or `hour_timestamp` for hourly). This makes both the nightly append (touching only one day) and range queries (reading only the days actually requested) faster and cheaper on top of the row-count savings.

#### 4.11.4 Boundary Constraint Worth Verifying

Each conversation must belong to exactly one row across all rollups — i.e. the source's timestamp must be a single fixed point (e.g. `local_timestamp` at conversation start), not a span. If a conversation could straddle a boundary (starts at 11:58 PM, ends at 12:02 AM), counting it in both days' rows would double-count when summed. Worth confirming with the team that owns the raw schema.

### 4.12 Audit Logger

- **Inputs:** an agent name, a decision summary, relevant input/output fields.
- **Outputs:** timestamped lines in a daily-rotated log file.
- **External dependencies:** Python's standard logging module.
- **Behavior:** every agent calls this once before returning. Lines look like *"[12:14:22] Planner decomposed prompt into 4 sub-questions"* or *"[12:14:25] Data Wrangler SQL retry 2/3 after error: column not found."* Files rotate daily into `logs/agent-YYYY-MM-DD.log`.

---

## 5. Output Layer — LLD

### 5.1 Outputs and Producers

| Output | Producer | Format | Delivery |
|---|---|---|---|
| Rendered UX | Dashboard Code Gen | Superset dashboard URL string | HTTP response body |
| Scheduled Jobs | Scheduler Agent | APScheduler jobs + JSON file | Persisted across restarts |
| Deployment Artifacts | OS-level supervisor | The running Python process | Auto-restarts on crash |
| Smart Dashboard | All chart-producing agents combined | Superset dashboard + insights list | Returned alongside the URL |
| Audit Logs | Audit Logger | Plain text log files | Daily rotated in `logs/` |

### 5.2 Response Composition

- On `done` — `dashboard_url`, `interpreted_intent`, `charts_created`, `insights`, `thread_id`.
- On `needs_clarification` — only `thread_id` and `clarification_question`.
- On `failed` — `thread_id` and a human-readable message indicating which stage failed.

---

## 6. Cross-Cutting Concerns

### 6.1 Logging Strategy
Three logical streams in one daily rotating file:
- Agent decisions (one line per agent invocation).
- External calls (one line per BigQuery, Superset, LLM call, with timing).
- Errors (full stack traces and state at point of failure).
Interleaved by timestamp.

### 6.2 Configuration vs Secrets
- Non-sensitive config in the config module.
- Secrets (Superset password, BigQuery JSON) in separate files with restricted permissions, referenced by path.

### 6.3 Concurrency Model
FastAPI handles requests on a worker thread pool. LangGraph runs synchronously within each request thread. Scheduler runs its own background thread pool. ChromaDB handles concurrent reads safely.

### 6.4 Data Volumes
- BigQuery results bounded by SQL `LIMIT` — typically 100 rows.
- Schema cache file: few hundred KB.
- Audit log files: <1 MB per day at this user count.
- Working memory: tens of MB per request.

### 6.5 Failure Visibility
Every failure produces both a log line and a structured field in the HTTP response.

### 6.6 Multi-Database Readiness
To switch databases: update config, point ChromaDB at a new empty folder, re-run Vanna training, adjust rollup specs, restart the service. No code changes anywhere.

---

## 7. Build & Verify Sequence

Each step independently testable before adding the next.

| # | Component | Verification |
|---|---|---|
| 1 | **Schema Awareness Component** | Run introspection by hand; cache file lists all tables/columns. |
| 2 | **Datamart 1 — bulk build SQL** | Run `daily_kpi_summary` bulk build in BigQuery console manually. Verify row count makes sense vs raw, spot-check a few daily totals. |
| 3 | **Datamart 1 — incremental append SQL** | Manually run yesterday's append. Confirm exactly one day's worth of new rows. No historical rows touched. |
| 4 | **Datamarts 2-4 — bulk + incremental** | Repeat steps 2-3 for `daily_device_summary`, `hourly_volume_summary`, `daily_region_summary`. |
| 5 | **Naming discipline** | Audit the codebase: every rollup name appears only in the config spec. Grep for the table names — they should only appear in config + log messages. |
| 6 | **Vanna training script** | Train with the rollup catalog documented. Ask Vanna *"trend of success rate over last 30 days"* via Python API — confirm SQL targets `daily_kpi_summary`, not the raw table. |
| 7 | **Vanna union pattern** | Ask Vanna a question covering today — confirm generated SQL has UNION ALL with today's raw aggregation. |
| 8 | **Chart Type Agent** | Hand-construct DataFrames of each shape; confirm chart type rule matches. |
| 9 | **UI Designer Agent** | Pass mixed chart lists; confirm row/width/height respects the 12-column rule. |
| 10 | **Dashboard Code Gen Agent** | Create a single chart with hard-coded config — appears in Superset. Then a single-chart dashboard. |
| 11 | **Planner Agent** | Send vague + specific prompts; inspect sub-question decomposition. |
| 12 | **Insights Agent** | Pass a single chart's data; insights reference real numbers from the data. |
| 13 | **Orchestration graph — happy path** | Wire all above into LangGraph. End-to-end run with a known-good prompt. Multi-chart dashboard appears. |
| 14 | **Diagnose & retry branch** | Seed a wrong column name in a prompt. Confirm system catches the SQL error, diagnoses, retries, recovers or skips cleanly. |
| 15 | **Clarification pause/resume** | Send *"show me the numbers"*. Confirm `needs_clarification` response. Reply with `thread_id` + answer. Confirm dashboard generated. |
| 16 | **API server** | Verify HTTP contract for `/generate-dashboard`, `/resume`, `/schedules`. |
| 17 | **Scheduler — daily schema refresh** | Trigger manually. Cache file updates. |
| 18 | **Scheduler — existence check** | Drop a rollup table manually. Trigger nightly job. Confirm bulk build runs, not incremental. |
| 19 | **Scheduler — incremental append** | With all rollups in place, trigger nightly job. Confirm each rollup gains one day. |
| 20 | **Scheduler — recurring dashboards** | Register a schedule for a prompt firing in 1 minute. Confirm autonomous dashboard generation. |
| 21 | **OS supervisor** | Reboot VM. Service auto-starts. Kill process. Service auto-restarts. |
| 22 | **Audit logger** | Run a full request. Every agent's decision appears in the daily log. |
| 23 | **Multi-database swap test** | Repoint config at a different BigQuery database. Re-run training. Confirm working dashboard generated against new database without code changes. |

The system is usable from step 10 (manual single-chart dashboards). Fully autonomous from step 13. Production-complete after step 23.

---

## 8. What This LLD Deliberately Does Not Specify

- Source code itself — kept in separate files per the file structure outlined in the README.
- Container or image definitions — not permitted in this deployment.
- External orchestration platforms beyond LangGraph and APScheduler — out of scope for this scale.
- Authentication and authorization — data is open in this setup.
- Multi-region or HA topology — single-VM deployment was the constraint.

Each would be appropriate to specify if scale, security posture, or compliance requirements changed.
