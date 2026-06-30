"""
Shared training data for Vanna AI -- both SQLite (local) and BigQuery (production).

Structure
---------
DDL_SQLITE / DDL_BIGQUERY   : CREATE TABLE statements for each backend
DOCUMENTATION               : Plain-English business rules injected into Vanna
TRAINING_PAIRS              : List of {question, sql_sqlite, sql_bigquery} dicts

Critical patterns taught by these pairs
----------------------------------------
1. ROLLUP PREFERENCE  -- always hit daily_*/hourly_* summary tables, never bxb_dw,
                         for any question covering a historic range.
2. UNION ALL TODAY    -- when the requested window includes today, UNION ALL the
                         rollup (day < today) with a live raw-table slice (day = today).
3. ADDITIVE COUNTS    -- rollup columns store RAW COUNTS (total_conversations,
                         successful_conversations, sum_kpi_completion). Rates are
                         computed at query time by dividing sums -- never stored or
                         averaged directly.
4. JUNCTION TABLE     -- in SQLite, execution_result is modelled in a separate
                         execution_results table joined on conversation_id.
                         In BigQuery it is ARRAY<STRING> accessed via UNNEST.
5. SUCCESS = 'SUCCESS'-- the value is always uppercase 'SUCCESS'.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# DDL -- fed to Vanna so it knows the schema precisely
# ---------------------------------------------------------------------------

DDL_SQLITE = """
CREATE TABLE bxb_dw (
    conversation_id TEXT PRIMARY KEY,
    yyyymmddhh      TEXT NOT NULL,   -- partition column (ISO timestamp string locally)
    local_timestamp TEXT NOT NULL,   -- derived human-readable timestamp
    device_id       TEXT NOT NULL,
    device_type     TEXT NOT NULL,   -- PHONE | SPEAKER | TV | WATCH | TABLET | CAR | OTHER
    country         TEXT NOT NULL,
    region          TEXT NOT NULL,   -- APAC | AMERICAS | EMEA | KOREA
    utterance       TEXT NOT NULL,
    kpi_completion  REAL NOT NULL    -- 0.0-1.0 task-completion score
);

-- Junction table modelling BigQuery ARRAY<STRING> execution_result.
-- One row per (conversation, result_value). JOIN on conversation_id.
CREATE TABLE execution_results (
    conversation_id TEXT NOT NULL REFERENCES bxb_dw(conversation_id),
    result          TEXT NOT NULL    -- SUCCESS | EXECUTION_FAILED | EXECUTION_DEEPLINK_REQUESTED | DEVICE_FEATURE_NOT_SUPPORTED | ...
);

-- Pre-aggregated rollup datamarts (PREFER THESE for historical queries).
-- All numeric columns are ADDITIVE COUNTS -- compute rates at query time.

CREATE TABLE daily_kpi_summary (
    day               TEXT,      -- YYYY-MM-DD
    execution_result  TEXT,      -- one result type per row
    total_conversations INTEGER,
    sum_kpi_completion  REAL
);

CREATE TABLE daily_device_summary (
    day                      TEXT,
    device_type              TEXT,
    total_conversations      INTEGER,
    successful_conversations INTEGER,  -- conversations where execution_results.result = 'SUCCESS'
    sum_kpi_completion       REAL
);

CREATE TABLE hourly_volume_summary (
    hour_timestamp      TEXT,    -- truncated to the hour
    total_conversations INTEGER
);

CREATE TABLE daily_region_summary (
    day                      TEXT,
    region                   TEXT,
    total_conversations      INTEGER,
    successful_conversations INTEGER,
    sum_kpi_completion       REAL
);
"""

DDL_BIGQUERY = """
-- Production table (BigQuery). Partition column yyyymmddhh MUST appear in WHERE.
CREATE TABLE `bixby2-analytics-dev.bxb4_dw.bxb_dw` (
    conversation_id  STRING,
    yyyymmddhh       TIMESTAMP,  -- partition column; format 20260615120000
    device_id        STRING,
    device_type      STRING,     -- PHONE | SPEAKER | TV | WATCH | TABLET | CAR | OTHER
    country          STRING,
    execution_result ARRAY<STRING>,  -- may contain multiple values per conversation
    utterance        STRING,
    kpi_completion   FLOAT64
);

-- Rollup datamarts (same logical schema as SQLite, stored in BigQuery).
CREATE TABLE `bixby2-analytics-dev.bxb4_dw.daily_kpi_summary` (
    day                TEXT,
    execution_result   TEXT,
    total_conversations INTEGER,
    sum_kpi_completion  FLOAT64
);

CREATE TABLE `bixby2-analytics-dev.bxb4_dw.daily_device_summary` (
    day                      TEXT,
    device_type              TEXT,
    total_conversations      INTEGER,
    successful_conversations INTEGER,
    sum_kpi_completion       FLOAT64
);

CREATE TABLE `bixby2-analytics-dev.bxb4_dw.hourly_volume_summary` (
    hour_timestamp      TEXT,
    total_conversations INTEGER
);

CREATE TABLE `bixby2-analytics-dev.bxb4_dw.daily_region_summary` (
    day                      TEXT,
    region                   TEXT,
    total_conversations      INTEGER,
    successful_conversations INTEGER,
    sum_kpi_completion       FLOAT64
);
"""

# ---------------------------------------------------------------------------
# Documentation -- business rules for Vanna's context
# ---------------------------------------------------------------------------

DOCUMENTATION: list[str] = [
    # --- Datamart preference ---
    "RULE: Always use the pre-aggregated rollup tables (daily_kpi_summary, "
    "daily_device_summary, hourly_volume_summary, daily_region_summary) for any "
    "question covering a historical time range. Query the raw bxb_dw table ONLY "
    "for today's live data or session-level detail.",

    # --- UNION ALL today pattern ---
    "RULE: When a question's time range includes today (e.g. 'last 7 days', "
    "'last 30 days', 'this month'), combine historical rollup data with today's "
    "raw data using UNION ALL.  The rollup part filters day < CURRENT_DATE(); the "
    "raw-table part filters for today only.  Then re-aggregate over the combined "
    "result.",

    # --- Additive counts / no stored rates ---
    "RULE: Rollup tables store additive raw counts (total_conversations, "
    "successful_conversations) and additive sums (sum_kpi_completion). "
    "NEVER average pre-stored rates. To compute success rate, "
    "SUM(successful_conversations) / SUM(total_conversations). "
    "To compute average KPI, SUM(sum_kpi_completion) / SUM(total_conversations).",

    # --- SUCCESS value ---
    "The execution result values are always uppercase: SUCCESS, EXECUTION_FAILED, "
    "EXECUTION_DEEPLINK_REQUESTED, DEVICE_FEATURE_NOT_SUPPORTED, EXECUTION_CANCELLED.",

    # --- daily_kpi_summary usage ---
    "daily_kpi_summary has one row per (day, execution_result_type). "
    "To get the total conversations on a day, SUM(total_conversations) across "
    "all execution_result values. "
    "To get success rate, filter WHERE execution_result = 'SUCCESS' "
    "or use SUM(CASE WHEN execution_result = 'SUCCESS' THEN total_conversations ELSE 0 END).",

    # --- daily_device_summary usage ---
    "daily_device_summary has one row per (day, device_type). "
    "successful_conversations is already a count of conversations that had at "
    "least one SUCCESS result. To compute device success rate: "
    "SUM(successful_conversations) / SUM(total_conversations).",

    # --- hourly_volume_summary usage ---
    "hourly_volume_summary stores conversation volume per hour. "
    "Use it for peak-hour charts and intra-day traffic patterns. "
    "Do NOT use bxb_dw for hourly volume questions covering more than 1 day.",

    # --- daily_region_summary usage ---
    "daily_region_summary has one row per (day, region). "
    "Region values: APAC, AMERICAS, EMEA, KOREA. "
    "India maps to APAC (~60% of traffic). US maps to AMERICAS (~15%).",

    # --- SQLite junction table ---
    "In SQLite (local dev), execution_result is stored in a separate table "
    "execution_results with columns (conversation_id, result). "
    "To check if a conversation succeeded: "
    "EXISTS (SELECT 1 FROM execution_results er WHERE er.conversation_id = t.conversation_id AND er.result = 'SUCCESS'). "
    "To count successful conversations from raw bxb_dw: join execution_results and count DISTINCT conversation_ids.",

    # --- BigQuery partition filter ---
    "In BigQuery, every query on bxb_dw MUST include a partition filter on "
    "yyyymmddhh, e.g.: WHERE yyyymmddhh >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY). "
    "Queries without this filter are rejected by BigQuery.",

    # --- Partition filter not needed on rollup tables ---
    "The rollup tables (daily_kpi_summary etc.) do NOT require a partition filter. "
    "Filter by the 'day' TEXT column using standard date comparisons.",
]

# ---------------------------------------------------------------------------
# Training pairs -- 17 Q->SQL pairs covering all 4 datamarts and key patterns
# ---------------------------------------------------------------------------

TRAINING_PAIRS: list[dict[str, str]] = [

    # -- daily_kpi_summary ------------------------------------------------

    {
        "question": "What is the overall Bixby success rate over the last 30 days?",
        "sql_sqlite": """
SELECT
    SUM(CASE WHEN execution_result = 'SUCCESS' THEN total_conversations ELSE 0 END)
        AS successful_conversations,
    SUM(total_conversations) AS total_conversations,
    ROUND(
        100.0
        * SUM(CASE WHEN execution_result = 'SUCCESS' THEN total_conversations ELSE 0 END)
        / NULLIF(SUM(total_conversations), 0),
        2
    ) AS success_rate_pct
FROM daily_kpi_summary
WHERE day >= date('now', '-30 days')
  AND day < date('now');
""",
        "sql_bigquery": """
SELECT
    SUM(IF(execution_result = 'SUCCESS', total_conversations, 0))
        AS successful_conversations,
    SUM(total_conversations) AS total_conversations,
    ROUND(
        100.0
        * SUM(IF(execution_result = 'SUCCESS', total_conversations, 0))
        / NULLIF(SUM(total_conversations), 0),
        2
    ) AS success_rate_pct
FROM `bixby2-analytics-dev.bxb4_dw.daily_kpi_summary`
WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
  AND day < CURRENT_DATE();
""",
    },

    {
        "question": "Show the daily success rate trend for the last 30 days.",
        "sql_sqlite": """
SELECT
    day,
    SUM(CASE WHEN execution_result = 'SUCCESS' THEN total_conversations ELSE 0 END)
        AS successful_conversations,
    SUM(total_conversations) AS total_conversations,
    ROUND(
        100.0
        * SUM(CASE WHEN execution_result = 'SUCCESS' THEN total_conversations ELSE 0 END)
        / NULLIF(SUM(total_conversations), 0),
        2
    ) AS success_rate_pct
FROM daily_kpi_summary
WHERE day >= date('now', '-30 days')
  AND day < date('now')
GROUP BY day
ORDER BY day;
""",
        "sql_bigquery": """
SELECT
    day,
    SUM(IF(execution_result = 'SUCCESS', total_conversations, 0))
        AS successful_conversations,
    SUM(total_conversations) AS total_conversations,
    ROUND(
        100.0
        * SUM(IF(execution_result = 'SUCCESS', total_conversations, 0))
        / NULLIF(SUM(total_conversations), 0),
        2
    ) AS success_rate_pct
FROM `bixby2-analytics-dev.bxb4_dw.daily_kpi_summary`
WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
  AND day < CURRENT_DATE()
GROUP BY day
ORDER BY day;
""",
    },

    {
        "question": "What is the daily success rate trend for the last 30 days including today?",
        "sql_sqlite": """
-- UNION ALL pattern: rollup for history, raw table for today's live data.
SELECT
    day,
    SUM(successful) AS successful_conversations,
    SUM(total)      AS total_conversations,
    ROUND(100.0 * SUM(successful) / NULLIF(SUM(total), 0), 2) AS success_rate_pct
FROM (
    -- Historical: from rollup (day < today)
    SELECT
        day,
        SUM(CASE WHEN execution_result = 'SUCCESS' THEN total_conversations ELSE 0 END) AS successful,
        SUM(total_conversations) AS total
    FROM daily_kpi_summary
    WHERE day >= date('now', '-30 days')
      AND day < date('now')
    GROUP BY day

    UNION ALL

    -- Today: live from raw table (junction table pattern)
    SELECT
        date(t.local_timestamp)                                             AS day,
        COUNT(DISTINCT CASE WHEN er.result = 'SUCCESS' THEN t.conversation_id END) AS successful,
        COUNT(DISTINCT t.conversation_id)                                   AS total
    FROM bxb_dw t
    LEFT JOIN execution_results er ON er.conversation_id = t.conversation_id
    WHERE t.local_timestamp >= date('now')
) combined
GROUP BY day
ORDER BY day;
""",
        "sql_bigquery": """
-- UNION ALL pattern: rollup for history, raw table for today's live data.
SELECT
    day,
    SUM(successful) AS successful_conversations,
    SUM(total)      AS total_conversations,
    ROUND(100.0 * SUM(successful) / NULLIF(SUM(total), 0), 2) AS success_rate_pct
FROM (
    -- Historical: from rollup (day < today)
    SELECT
        day,
        SUM(IF(execution_result = 'SUCCESS', total_conversations, 0)) AS successful,
        SUM(total_conversations) AS total
    FROM `bixby2-analytics-dev.bxb4_dw.daily_kpi_summary`
    WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
      AND day < CURRENT_DATE()
    GROUP BY day

    UNION ALL

    -- Today: live from raw table (partition filter mandatory)
    SELECT
        DATE(yyyymmddhh) AS day,
        COUNTIF(EXISTS (
            SELECT 1 FROM UNNEST(execution_result) r WHERE r = 'SUCCESS'
        )) AS successful,
        COUNT(*) AS total
    FROM `bixby2-analytics-dev.bxb4_dw.bxb_dw`
    WHERE yyyymmddhh >= TIMESTAMP(CURRENT_DATE())
) combined
GROUP BY day
ORDER BY day;
""",
    },

    {
        "question": "What is the breakdown of conversations by execution result type for the last 7 days?",
        "sql_sqlite": """
SELECT
    execution_result,
    SUM(total_conversations) AS total_conversations,
    ROUND(
        100.0 * SUM(total_conversations)
        / NULLIF((SELECT SUM(total_conversations) FROM daily_kpi_summary
                  WHERE day >= date('now', '-7 days') AND day < date('now')), 0),
        2
    ) AS share_pct
FROM daily_kpi_summary
WHERE day >= date('now', '-7 days')
  AND day < date('now')
GROUP BY execution_result
ORDER BY total_conversations DESC;
""",
        "sql_bigquery": """
SELECT
    execution_result,
    SUM(total_conversations) AS total_conversations,
    ROUND(
        100.0 * SUM(total_conversations)
        / NULLIF(SUM(SUM(total_conversations)) OVER (), 0),
        2
    ) AS share_pct
FROM `bixby2-analytics-dev.bxb4_dw.daily_kpi_summary`
WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
  AND day < CURRENT_DATE()
GROUP BY execution_result
ORDER BY total_conversations DESC;
""",
    },

    {
        "question": "What is the average KPI completion score for the last 30 days?",
        "sql_sqlite": """
-- SUM then divide -- never average pre-stored rates.
SELECT
    ROUND(
        SUM(sum_kpi_completion) / NULLIF(SUM(total_conversations), 0),
        4
    ) AS avg_kpi_completion
FROM daily_kpi_summary
WHERE day >= date('now', '-30 days')
  AND day < date('now');
""",
        "sql_bigquery": """
SELECT
    ROUND(
        SUM(sum_kpi_completion) / NULLIF(SUM(total_conversations), 0),
        4
    ) AS avg_kpi_completion
FROM `bixby2-analytics-dev.bxb4_dw.daily_kpi_summary`
WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
  AND day < CURRENT_DATE();
""",
    },

    {
        "question": "Show daily total conversation volume for the last 7 days.",
        "sql_sqlite": """
SELECT
    day,
    SUM(total_conversations) AS total_conversations
FROM daily_kpi_summary
WHERE day >= date('now', '-7 days')
  AND day < date('now')
GROUP BY day
ORDER BY day;
""",
        "sql_bigquery": """
SELECT
    day,
    SUM(total_conversations) AS total_conversations
FROM `bixby2-analytics-dev.bxb4_dw.daily_kpi_summary`
WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
  AND day < CURRENT_DATE()
GROUP BY day
ORDER BY day;
""",
    },

    # -- daily_device_summary ---------------------------------------------

    {
        "question": "Which device type has the highest success rate in the last 30 days?",
        "sql_sqlite": """
SELECT
    device_type,
    SUM(successful_conversations) AS successful_conversations,
    SUM(total_conversations)      AS total_conversations,
    ROUND(
        100.0 * SUM(successful_conversations) / NULLIF(SUM(total_conversations), 0),
        2
    ) AS success_rate_pct
FROM daily_device_summary
WHERE day >= date('now', '-30 days')
  AND day < date('now')
GROUP BY device_type
ORDER BY success_rate_pct DESC;
""",
        "sql_bigquery": """
SELECT
    device_type,
    SUM(successful_conversations) AS successful_conversations,
    SUM(total_conversations)      AS total_conversations,
    ROUND(
        100.0 * SUM(successful_conversations) / NULLIF(SUM(total_conversations), 0),
        2
    ) AS success_rate_pct
FROM `bixby2-analytics-dev.bxb4_dw.daily_device_summary`
WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
  AND day < CURRENT_DATE()
GROUP BY device_type
ORDER BY success_rate_pct DESC;
""",
    },

    {
        "question": "Show conversation volume by device type for the last 7 days.",
        "sql_sqlite": """
SELECT
    device_type,
    SUM(total_conversations) AS total_conversations
FROM daily_device_summary
WHERE day >= date('now', '-7 days')
  AND day < date('now')
GROUP BY device_type
ORDER BY total_conversations DESC;
""",
        "sql_bigquery": """
SELECT
    device_type,
    SUM(total_conversations) AS total_conversations
FROM `bixby2-analytics-dev.bxb4_dw.daily_device_summary`
WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
  AND day < CURRENT_DATE()
GROUP BY device_type
ORDER BY total_conversations DESC;
""",
    },

    {
        "question": "Show daily conversation volume by device type for the last 14 days.",
        "sql_sqlite": """
SELECT
    day,
    device_type,
    SUM(total_conversations) AS total_conversations
FROM daily_device_summary
WHERE day >= date('now', '-14 days')
  AND day < date('now')
GROUP BY day, device_type
ORDER BY day, total_conversations DESC;
""",
        "sql_bigquery": """
SELECT
    day,
    device_type,
    SUM(total_conversations) AS total_conversations
FROM `bixby2-analytics-dev.bxb4_dw.daily_device_summary`
WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL 14 DAY)
  AND day < CURRENT_DATE()
GROUP BY day, device_type
ORDER BY day, total_conversations DESC;
""",
    },

    {
        "question": "What is the average KPI completion score by device type for the last 30 days?",
        "sql_sqlite": """
-- Use SUM then divide -- never average pre-stored rates.
SELECT
    device_type,
    ROUND(
        SUM(sum_kpi_completion) / NULLIF(SUM(total_conversations), 0),
        4
    ) AS avg_kpi_completion
FROM daily_device_summary
WHERE day >= date('now', '-30 days')
  AND day < date('now')
GROUP BY device_type
ORDER BY avg_kpi_completion DESC;
""",
        "sql_bigquery": """
SELECT
    device_type,
    ROUND(
        SUM(sum_kpi_completion) / NULLIF(SUM(total_conversations), 0),
        4
    ) AS avg_kpi_completion
FROM `bixby2-analytics-dev.bxb4_dw.daily_device_summary`
WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
  AND day < CURRENT_DATE()
GROUP BY device_type
ORDER BY avg_kpi_completion DESC;
""",
    },

    {
        "question": "Show device type conversation volume including today.",
        "sql_sqlite": """
-- UNION ALL: rollup for history, raw table for today's live counts.
SELECT
    device_type,
    SUM(total_conversations) AS total_conversations
FROM (
    SELECT device_type, total_conversations
    FROM daily_device_summary
    WHERE day >= date('now', '-30 days')
      AND day < date('now')

    UNION ALL

    SELECT device_type, 1 AS total_conversations
    FROM bxb_dw
    WHERE local_timestamp >= date('now')
) combined
GROUP BY device_type
ORDER BY total_conversations DESC;
""",
        "sql_bigquery": """
-- UNION ALL: rollup for history, raw table for today's live counts.
SELECT
    device_type,
    SUM(total_conversations) AS total_conversations
FROM (
    SELECT device_type, total_conversations
    FROM `bixby2-analytics-dev.bxb4_dw.daily_device_summary`
    WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
      AND day < CURRENT_DATE()

    UNION ALL

    SELECT device_type, 1 AS total_conversations
    FROM `bixby2-analytics-dev.bxb4_dw.bxb_dw`
    WHERE yyyymmddhh >= TIMESTAMP(CURRENT_DATE())
) combined
GROUP BY device_type
ORDER BY total_conversations DESC;
""",
    },

    # -- hourly_volume_summary --------------------------------------------

    {
        "question": "What are the peak conversation hours over the last 7 days?",
        "sql_sqlite": """
SELECT
    strftime('%H:00', hour_timestamp) AS hour_of_day,
    SUM(total_conversations)          AS total_conversations
FROM hourly_volume_summary
WHERE hour_timestamp >= datetime('now', '-7 days')
GROUP BY strftime('%H', hour_timestamp)
ORDER BY total_conversations DESC
LIMIT 5;
""",
        "sql_bigquery": """
SELECT
    FORMAT_TIMESTAMP('%H:00', PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', hour_timestamp)) AS hour_of_day,
    SUM(total_conversations) AS total_conversations
FROM `bixby2-analytics-dev.bxb4_dw.hourly_volume_summary`
WHERE hour_timestamp >= FORMAT_TIMESTAMP(
        '%Y-%m-%d %H:%M:%S',
        TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
      )
GROUP BY hour_of_day
ORDER BY total_conversations DESC
LIMIT 5;
""",
    },

    {
        "question": "Show the total conversation volume by hour of day averaged over the last 30 days.",
        "sql_sqlite": """
-- Use additive SUM then divide by number of distinct days for a per-hour average.
SELECT
    strftime('%H:00', hour_timestamp)   AS hour_of_day,
    SUM(total_conversations)            AS total_conversations,
    ROUND(
        1.0 * SUM(total_conversations)
        / (SELECT COUNT(DISTINCT date(hour_timestamp))
           FROM hourly_volume_summary
           WHERE hour_timestamp >= datetime('now', '-30 days')),
        1
    ) AS avg_conversations_per_day
FROM hourly_volume_summary
WHERE hour_timestamp >= datetime('now', '-30 days')
GROUP BY strftime('%H', hour_timestamp)
ORDER BY hour_of_day;
""",
        "sql_bigquery": """
SELECT
    FORMAT_TIMESTAMP('%H:00', PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', hour_timestamp)) AS hour_of_day,
    SUM(total_conversations) AS total_conversations,
    ROUND(
        SUM(total_conversations) / COUNT(DISTINCT DATE(PARSE_TIMESTAMP('%Y-%m-%d %H:%M:%S', hour_timestamp))),
        1
    ) AS avg_conversations_per_day
FROM `bixby2-analytics-dev.bxb4_dw.hourly_volume_summary`
WHERE hour_timestamp >= FORMAT_TIMESTAMP(
        '%Y-%m-%d %H:%M:%S',
        TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
      )
GROUP BY hour_of_day
ORDER BY hour_of_day;
""",
    },

    # -- daily_region_summary ---------------------------------------------

    {
        "question": "Which region has the highest Bixby success rate in the last 30 days?",
        "sql_sqlite": """
SELECT
    region,
    SUM(successful_conversations) AS successful_conversations,
    SUM(total_conversations)      AS total_conversations,
    ROUND(
        100.0 * SUM(successful_conversations) / NULLIF(SUM(total_conversations), 0),
        2
    ) AS success_rate_pct
FROM daily_region_summary
WHERE day >= date('now', '-30 days')
  AND day < date('now')
GROUP BY region
ORDER BY success_rate_pct DESC;
""",
        "sql_bigquery": """
SELECT
    region,
    SUM(successful_conversations) AS successful_conversations,
    SUM(total_conversations)      AS total_conversations,
    ROUND(
        100.0 * SUM(successful_conversations) / NULLIF(SUM(total_conversations), 0),
        2
    ) AS success_rate_pct
FROM `bixby2-analytics-dev.bxb4_dw.daily_region_summary`
WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
  AND day < CURRENT_DATE()
GROUP BY region
ORDER BY success_rate_pct DESC;
""",
    },

    {
        "question": "Show conversation volume by region for the last 30 days.",
        "sql_sqlite": """
SELECT
    region,
    SUM(total_conversations) AS total_conversations
FROM daily_region_summary
WHERE day >= date('now', '-30 days')
  AND day < date('now')
GROUP BY region
ORDER BY total_conversations DESC;
""",
        "sql_bigquery": """
SELECT
    region,
    SUM(total_conversations) AS total_conversations
FROM `bixby2-analytics-dev.bxb4_dw.daily_region_summary`
WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
  AND day < CURRENT_DATE()
GROUP BY region
ORDER BY total_conversations DESC;
""",
    },

    {
        "question": "Compare average KPI completion across regions for the last 30 days.",
        "sql_sqlite": """
SELECT
    region,
    ROUND(
        SUM(sum_kpi_completion) / NULLIF(SUM(total_conversations), 0),
        4
    ) AS avg_kpi_completion,
    SUM(total_conversations) AS total_conversations
FROM daily_region_summary
WHERE day >= date('now', '-30 days')
  AND day < date('now')
GROUP BY region
ORDER BY avg_kpi_completion DESC;
""",
        "sql_bigquery": """
SELECT
    region,
    ROUND(
        SUM(sum_kpi_completion) / NULLIF(SUM(total_conversations), 0),
        4
    ) AS avg_kpi_completion,
    SUM(total_conversations) AS total_conversations
FROM `bixby2-analytics-dev.bxb4_dw.daily_region_summary`
WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
  AND day < CURRENT_DATE()
GROUP BY region
ORDER BY avg_kpi_completion DESC;
""",
    },

    {
        "question": "Show the daily regional success rate trend for the last 14 days including today.",
        "sql_sqlite": """
-- UNION ALL: rollup for historical days, raw table for today.
SELECT
    day,
    region,
    SUM(successful) AS successful_conversations,
    SUM(total)      AS total_conversations,
    ROUND(100.0 * SUM(successful) / NULLIF(SUM(total), 0), 2) AS success_rate_pct
FROM (
    -- Historical from rollup
    SELECT
        day,
        region,
        successful_conversations AS successful,
        total_conversations      AS total
    FROM daily_region_summary
    WHERE day >= date('now', '-14 days')
      AND day < date('now')

    UNION ALL

    -- Today from raw table (junction table pattern)
    SELECT
        date(t.local_timestamp)                                                   AS day,
        t.region,
        COUNT(DISTINCT CASE WHEN er.result = 'SUCCESS' THEN t.conversation_id END) AS successful,
        COUNT(DISTINCT t.conversation_id)                                          AS total
    FROM bxb_dw t
    LEFT JOIN execution_results er ON er.conversation_id = t.conversation_id
    WHERE t.local_timestamp >= date('now')
    GROUP BY date(t.local_timestamp), t.region
) combined
GROUP BY day, region
ORDER BY day, region;
""",
        "sql_bigquery": """
-- UNION ALL: rollup for historical days, raw table for today.
SELECT
    day,
    region,
    SUM(successful) AS successful_conversations,
    SUM(total)      AS total_conversations,
    ROUND(100.0 * SUM(successful) / NULLIF(SUM(total), 0), 2) AS success_rate_pct
FROM (
    -- Historical from rollup
    SELECT
        day,
        region,
        successful_conversations AS successful,
        total_conversations      AS total
    FROM `bixby2-analytics-dev.bxb4_dw.daily_region_summary`
    WHERE day >= DATE_SUB(CURRENT_DATE(), INTERVAL 14 DAY)
      AND day < CURRENT_DATE()

    UNION ALL

    -- Today from raw table (partition filter mandatory)
    SELECT
        DATE(yyyymmddhh) AS day,
        region,
        COUNTIF(EXISTS (SELECT 1 FROM UNNEST(execution_result) r WHERE r = 'SUCCESS')) AS successful,
        COUNT(*)          AS total
    FROM `bixby2-analytics-dev.bxb4_dw.bxb_dw`
    WHERE yyyymmddhh >= TIMESTAMP(CURRENT_DATE())
    GROUP BY day, region
) combined
GROUP BY day, region
ORDER BY day, region;
""",
    },

]

# ---------------------------------------------------------------------------
# Helpers -- used by train_local.py and train_bigquery.py
# ---------------------------------------------------------------------------


def iter_sqlite_pairs() -> list[tuple[str, str]]:
    """Yield (question, sql_sqlite) for every training pair."""
    return [(p["question"], p["sql_sqlite"]) for p in TRAINING_PAIRS]


def iter_bigquery_pairs() -> list[tuple[str, str]]:
    """Yield (question, sql_bigquery) for every training pair."""
    return [(p["question"], p["sql_bigquery"]) for p in TRAINING_PAIRS]
