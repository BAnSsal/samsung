"""
Generate synthetic Bixby conversation data and pre-built datamart rollups.

Writes to config.SQLITE_PATH (synthetic/local.db). Deterministic (seed=42).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import config
from config import ROLLUP_TABLE_SPECS, get_rollup_spec_by_name

RANDOM_SEED = 42
TARGET_CONVERSATIONS = 106_000
DAYS_SPAN = 365
END_DATE = date(2026, 6, 28)

DEVICE_TYPES = ["PHONE", "SPEAKER", "TV", "WATCH", "TABLET", "CAR", "OTHER"]
DEVICE_WEIGHTS = [0.45, 0.25, 0.12, 0.08, 0.06, 0.03, 0.01]

COUNTRY_REGION = [
    ("India", "APAC", 0.60),
    ("United States", "AMERICAS", 0.15),
    ("South Korea", "APAC", 0.10),
    ("United Kingdom", "EMEA", 0.04),
    ("Germany", "EMEA", 0.03),
    ("Japan", "APAC", 0.03),
    ("Brazil", "LATAM", 0.02),
    ("Australia", "APAC", 0.02),
    ("Canada", "AMERICAS", 0.01),
]
COUNTRY_NAMES = [c[0] for c in COUNTRY_REGION]
COUNTRY_PROBS = [c[2] for c in COUNTRY_REGION]
COUNTRY_TO_REGION = {c[0]: c[1] for c in COUNTRY_REGION}

OTHER_RESULTS = [
    "EXECUTION_CANCELLED",
    "TIMEOUT",
    "PERMISSION_DENIED",
]

UTTERANCES = [
    "What's the weather today?",
    "Set an alarm for 7 AM",
    "Play my favorite playlist",
    "Turn off the living room lights",
    "Send a message to Mom",
    "What meetings do I have tomorrow?",
    "Navigate to the nearest coffee shop",
    "Open YouTube",
    "What's on my calendar?",
    "Remind me to buy groceries at 5 PM",
    "Call John",
    "What's the news?",
    "Increase the volume",
    "Turn on do not disturb",
    "What's the capital of France?",
    "Start a timer for 10 minutes",
    "Read my unread messages",
    "What's my step count today?",
    "Lock the front door",
    "Take a screenshot",
    "Translate hello to Spanish",
    "What's the stock price of Samsung?",
    "Dim the bedroom lights to 50%",
    "Skip this song",
    "Tell me a joke",
    "How long until my next meeting?",
    "Turn on the TV",
    "What's the exchange rate for USD to INR?",
    "Add milk to my shopping list",
    "Stop the music",
]

HOUR_WEIGHTS = np.array(
    [
        0.35,
        0.25,
        0.08,
        0.05,
        0.05,
        0.08,  # 0-5 AM (low 2-5)
        0.45,
        0.70,
        0.95,
        1.20,
        1.35,
        1.25,  # morning peak 9-11
        1.00,
        0.90,
        0.85,
        0.80,
        0.85,
        0.95,
        1.10,
        1.30,
        1.35,
        1.15,  # evening peak 7-9
        0.85,
        0.55,
    ],
    dtype=float,
)
HOUR_WEIGHTS /= HOUR_WEIGHTS.sum()


def _db_path() -> Path:
    path = Path(config.SQLITE_PATH)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _create_schema(conn: sqlite3.Connection) -> None:
    raw = config.RAW_TABLE_NAME
    er = config.EXECUTION_RESULTS_TABLE
    conv = config.CONVERSATION_ID_COLUMN
    ts = config.TIMESTAMP_COLUMN
    part = config.PARTITION_COLUMN
    device_type = config.DEVICE_TYPE_COLUMN
    country = config.COUNTRY_COLUMN
    region = config.REGION_COLUMN
    utterance = config.UTTERANCE_COLUMN
    kpi = config.KPI_COMPLETION_COLUMN

    conn.executescript(
        f"""
        DROP TABLE IF EXISTS {er};
        DROP TABLE IF EXISTS {raw};
        """
    )
    for spec in ROLLUP_TABLE_SPECS:
        conn.execute(f"DROP TABLE IF EXISTS {spec['name']}")

    conn.executescript(
        f"""
        CREATE TABLE {raw} (
            {conv} TEXT PRIMARY KEY,
            {part} TEXT NOT NULL,
            {ts} TEXT NOT NULL,
            device_id TEXT NOT NULL,
            {device_type} TEXT NOT NULL,
            {country} TEXT NOT NULL,
            {region} TEXT NOT NULL,
            {utterance} TEXT NOT NULL,
            {kpi} REAL NOT NULL
        );

        CREATE TABLE {er} (
            {conv} TEXT NOT NULL,
            result TEXT NOT NULL,
            FOREIGN KEY ({conv}) REFERENCES {raw}({conv})
        );
        CREATE INDEX idx_er_conv ON {er}({conv});
        CREATE INDEX idx_er_result ON {er}(result);
        CREATE INDEX idx_er_result_conv ON {er}(result, {conv});
        CREATE INDEX idx_bxb_ts ON {raw}({ts});
        CREATE INDEX idx_bxb_device ON {raw}({device_type});
        CREATE INDEX idx_bxb_region ON {raw}({region});
        """
    )

    for spec in ROLLUP_TABLE_SPECS:
        cols = ", ".join(f"{col} {'REAL' if col.startswith('sum_') else 'INTEGER' if col.startswith('total_') or col.startswith('successful_') else 'TEXT'}" for col in spec["output_columns"])
        conn.execute(f"CREATE TABLE {spec['name']} ({cols})")


def _daily_conversation_counts(rng: np.random.Generator) -> list[int]:
    start = END_DATE - timedelta(days=DAYS_SPAN - 1)
    raw_targets: list[float] = []
    for offset in range(DAYS_SPAN):
        d = start + timedelta(days=offset)
        trend = 1.0 + 0.30 * (offset / max(DAYS_SPAN - 1, 1))
        if d.weekday() >= 5:
            base = rng.integers(100, 201)
        else:
            base = rng.integers(200, 401)
        raw_targets.append(float(base) * trend)

    scale = TARGET_CONVERSATIONS / sum(raw_targets)
    exact = [v * scale for v in raw_targets]
    counts = [int(x) for x in exact]
    remainder = TARGET_CONVERSATIONS - sum(counts)
    fractional = sorted(
        ((i, exact[i] - counts[i]) for i in range(DAYS_SPAN)),
        key=lambda item: item[1],
        reverse=True,
    )
    for i in range(remainder):
        counts[fractional[i][0]] += 1
    assert sum(counts) == TARGET_CONVERSATIONS
    return counts


def _generate_conversations(rng: np.random.Generator) -> tuple[list[tuple], list[tuple]]:
    daily_counts = _daily_conversation_counts(rng)
    n = TARGET_CONVERSATIONS
    start = END_DATE - timedelta(days=DAYS_SPAN - 1)

    day_offsets = np.repeat(np.arange(DAYS_SPAN), daily_counts)
    hours = rng.choice(24, size=n, p=HOUR_WEIGHTS)
    minutes = rng.integers(0, 60, size=n)
    seconds = rng.integers(0, 60, size=n)

    timestamps: list[str] = []
    for i in range(n):
        day = start + timedelta(days=int(day_offsets[i]))
        timestamps.append(
            f"{day.isoformat()}T{int(hours[i]):02d}:{int(minutes[i]):02d}:{int(seconds[i]):02d}"
        )

    conv_ids = [f"conv_{i:08d}" for i in range(n)]
    countries = rng.choice(COUNTRY_NAMES, size=n, p=COUNTRY_PROBS)
    regions = [COUNTRY_TO_REGION[c] for c in countries]
    devices = rng.choice(DEVICE_TYPES, size=n, p=DEVICE_WEIGHTS)
    kpis = rng.beta(5, 2, size=n)
    device_ids = [f"dev_{x}" for x in rng.integers(10_000_000, 99_999_999, size=n)]
    utterances = rng.choice(UTTERANCES, size=n)

    raw_rows = [
        (
            conv_ids[i],
            timestamps[i],
            timestamps[i],
            device_ids[i],
            devices[i],
            countries[i],
            regions[i],
            utterances[i],
            float(kpis[i]),
        )
        for i in range(n)
    ]

    er_rows: list[tuple] = []
    success_flags = rng.random(n) < 0.35
    failed_flags = rng.random(n) < 0.15
    deeplink_flags = rng.random(n) < 0.40
    unsupported_flags = rng.random(n) < 0.08
    other_flags = rng.random(n) < 0.02
    target_lens = rng.integers(1, 4, size=n)

    for i in range(n):
        results: list[str] = []
        if success_flags[i]:
            results.append(config.EXECUTION_RESULT_SUCCESS_VALUE)
        if failed_flags[i]:
            results.append("EXECUTION_FAILED")
        if deeplink_flags[i]:
            results.append("EXECUTION_DEEPLINK_REQUESTED")
        if unsupported_flags[i]:
            results.append("DEVICE_FEATURE_NOT_SUPPORTED")
        if other_flags[i]:
            results.append(rng.choice(OTHER_RESULTS))
        if not results:
            results.append(
                rng.choice(
                    [
                        "EXECUTION_DEEPLINK_REQUESTED",
                        "EXECUTION_FAILED",
                        "DEVICE_FEATURE_NOT_SUPPORTED",
                    ],
                    p=[0.6, 0.25, 0.15],
                )
            )
        results = list(dict.fromkeys(results))
        filler_pool = [
            "EXECUTION_DEEPLINK_REQUESTED",
            "EXECUTION_FAILED",
            "DEVICE_FEATURE_NOT_SUPPORTED",
        ]
        attempts = 0
        while len(results) < int(target_lens[i]) and attempts < 10:
            candidate = rng.choice(filler_pool)
            if candidate not in results:
                results.append(candidate)
            attempts += 1
        for result in results[:3]:
            er_rows.append((conv_ids[i], result))

    return raw_rows, er_rows


def _bulk_rollup_sql(spec_name: str) -> str:
    """Bulk-build SQL for one rollup — mirrors Scheduler bulk-build logic on SQLite."""
    spec = get_rollup_spec_by_name(spec_name)
    name = spec["name"]
    raw = config.RAW_TABLE_NAME
    er = config.EXECUTION_RESULTS_TABLE
    conv = config.CONVERSATION_ID_COLUMN
    ts = config.TIMESTAMP_COLUMN
    kpi = config.KPI_COMPLETION_COLUMN
    success = config.EXECUTION_RESULT_SUCCESS_VALUE

    if spec["grain_columns"] == ["execution_result"]:
        return f"""
            INSERT INTO {name}
            SELECT
                date(t.{ts}) AS day,
                er.result AS execution_result,
                COUNT(*) AS total_conversations,
                SUM(t.{kpi}) AS sum_kpi_completion
            FROM {raw} t
            INNER JOIN {er} er ON t.{conv} = er.{conv}
            GROUP BY day, execution_result
        """

    if spec["partition_column"] == "hour_timestamp":
        return f"""
            INSERT INTO {name}
            SELECT
                strftime('%Y-%m-%d %H:00:00', t.{ts}) AS hour_timestamp,
                COUNT(*) AS total_conversations
            FROM {raw} t
            GROUP BY hour_timestamp
        """

    grain = spec["grain_columns"][0]
    return f"""
        INSERT INTO {name}
        SELECT
            date(t.{ts}) AS day,
            t.{grain},
            COUNT(*) AS total_conversations,
            SUM(CASE WHEN er_ok.{conv} IS NOT NULL THEN 1 ELSE 0 END) AS successful_conversations,
            SUM(t.{kpi}) AS sum_kpi_completion
        FROM {raw} t
        LEFT JOIN _success_conversations er_ok ON t.{conv} = er_ok.{conv}
        GROUP BY day, t.{grain}
    """


def _prepare_success_lookup(conn: sqlite3.Connection) -> None:
    er = config.EXECUTION_RESULTS_TABLE
    conv = config.CONVERSATION_ID_COLUMN
    success = config.EXECUTION_RESULT_SUCCESS_VALUE
    conn.execute("DROP TABLE IF EXISTS _success_conversations")
    conn.execute(
        f"""
        CREATE TEMP TABLE _success_conversations AS
        SELECT DISTINCT {conv}
        FROM {er}
        WHERE result = '{success}'
        """
    )
    conn.execute(
        f"CREATE INDEX idx_success_conv ON _success_conversations({conv})"
    )


def _build_rollups(conn: sqlite3.Connection) -> None:
    _prepare_success_lookup(conn)
    for spec in ROLLUP_TABLE_SPECS:
        name = spec["name"]
        print(f"  building {name}...", flush=True)
        conn.execute(f"DELETE FROM {name}")
        sql = _bulk_rollup_sql(name)
        conn.execute(sql)
        conn.commit()


def _print_row_counts(conn: sqlite3.Connection) -> None:
    tables = [config.RAW_TABLE_NAME, config.EXECUTION_RESULTS_TABLE]
    tables.extend(spec["name"] for spec in ROLLUP_TABLE_SPECS)

    print("\n=== Row counts ===")
    for table in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count:,}")

    raw = config.RAW_TABLE_NAME
    er = config.EXECUTION_RESULTS_TABLE
    success = config.EXECUTION_RESULT_SUCCESS_VALUE
    conv = config.CONVERSATION_ID_COLUMN

    success_convs = conn.execute(
        f"""
        SELECT COUNT(DISTINCT {conv}) FROM {er} WHERE result = ?
        """,
        (success,),
    ).fetchone()[0]
    total_convs = conn.execute(f"SELECT COUNT(*) FROM {raw}").fetchone()[0]
    pct = 100.0 * success_convs / total_convs if total_convs else 0.0
    print(f"\n  Conversations with SUCCESS: {success_convs:,} ({pct:.1f}%)")
    print(f"  Avg execution_results per conversation: "
          f"{conn.execute(f'SELECT COUNT(*) FROM {er}').fetchone()[0] / total_convs:.2f}")


def _cleanup_stale_build_artifacts(db_path: Path) -> None:
    for pattern in ("local.db.tmp*", "local.db-journal", "local.db-wal", "local.db-shm"):
        for path in db_path.parent.glob(pattern):
            try:
                path.unlink()
            except OSError:
                pass


def main() -> None:
    rng = np.random.default_rng(RANDOM_SEED)
    db_path = _db_path()
    work_path = Path(tempfile.gettempdir()) / f"bixby_synth_{os.getpid()}.db"

    print(f"Generating {TARGET_CONVERSATIONS:,} conversations over {DAYS_SPAN} days...", flush=True)
    raw_rows, er_rows = _generate_conversations(rng)
    assert len(raw_rows) == TARGET_CONVERSATIONS

    raw = config.RAW_TABLE_NAME
    er = config.EXECUTION_RESULTS_TABLE

    if work_path.exists():
        work_path.unlink()

    with sqlite3.connect(work_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA temp_store = MEMORY")
        _create_schema(conn)
        conn.commit()
        conn.executemany(
            f"""
            INSERT INTO {raw} (
                {config.CONVERSATION_ID_COLUMN},
                {config.PARTITION_COLUMN},
                {config.TIMESTAMP_COLUMN},
                device_id,
                {config.DEVICE_TYPE_COLUMN},
                {config.COUNTRY_COLUMN},
                {config.REGION_COLUMN},
                {config.UTTERANCE_COLUMN},
                {config.KPI_COMPLETION_COLUMN}
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            raw_rows,
        )
        conn.executemany(
            f"INSERT INTO {er} ({config.CONVERSATION_ID_COLUMN}, result) VALUES (?, ?)",
            er_rows,
        )
        conn.commit()
        print("Building datamart rollups...", flush=True)
        _build_rollups(conn)
        conn.commit()
        _print_row_counts(conn)

    _cleanup_stale_build_artifacts(db_path)
    try:
        staging_path = db_path.with_suffix(".db.new")
        shutil.copy2(work_path, staging_path)
        if db_path.exists():
            db_path.unlink()
        staging_path.replace(db_path)
        try:
            work_path.unlink()
        except OSError:
            pass
        print(f"\nWrote {db_path}")
    except OSError as exc:
        try:
            shutil.copy2(work_path, db_path)
            work_path.unlink(missing_ok=True)
            print(f"\nWrote {db_path} (overwritten in place)")
        except OSError:
            print(f"\nCould not copy database to {db_path}: {exc}")
            print(f"Fresh database written to {work_path}")


if __name__ == "__main__":
    main()
