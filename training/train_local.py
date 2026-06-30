"""
Local Vanna training script - SQLite backend + ChromaDB + Google Gemini.

Trains Vanna on:
  1. SQLite DDL (schema structure)
  2. Business documentation strings (rules, patterns, caveats)
  3. SQLite question -> SQL training pairs from common_pairs.py

ChromaDB persists to config.CHROMADB_PATH (chromadb_store/).
Gemini is used as the LLM for SQL generation at query time;
training itself only stores embeddings (no Gemini quota consumed here).

Usage:
    python -m training.train_local           # full training run
    python -m training.train_local --reset   # wipe ChromaDB first, then train
    python -m training.train_local --ask     # interactive Q&A after training

Prerequisites:
    synthetic/local.db must exist (run python -m synthetic.generate first).
    pip install "vanna==0.7.9" chromadb google-generativeai
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import io
import config

# Force UTF-8 stdout so Unicode log messages (em-dashes, arrows) don't crash on Windows.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Vanna class -- ChromaDB vector store + Google Gemini LLM
# (import directly from submodule to avoid vertexai dependency in __init__.py)
# ---------------------------------------------------------------------------

from vanna.chromadb import ChromaDB_VectorStore          # noqa: E402
from vanna.google.gemini_chat import GoogleGeminiChat    # noqa: E402


class BixbyVanna(ChromaDB_VectorStore, GoogleGeminiChat):
    """
    Vanna subclass combining:
    - ChromaDB_VectorStore  → persistent local vector store for DDL/docs/SQL pairs
    - GoogleGeminiChat      → Gemini as the SQL-generation LLM

    At training time: only ChromaDB writes occur (no LLM calls, no quota used).
    At query time:    ChromaDB retrieval + Gemini call to generate SQL.
    """

    def __init__(self, config: dict):
        ChromaDB_VectorStore.__init__(self, config=config)
        GoogleGeminiChat.__init__(self, config=config)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def _get_chromadb_path() -> Path:
    path = Path(config.CHROMADB_PATH)
    if not path.is_absolute():
        path = _ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def build_vanna(*, reset: bool = False) -> BixbyVanna:
    """
    Instantiate BixbyVanna pointing at the persistent ChromaDB store.

    Parameters
    ----------
    reset : if True, delete and recreate the ChromaDB collection before returning.
    """
    chromadb_path = _get_chromadb_path()

    vn = BixbyVanna(config={
        # Gemini LLM (used at query time — not during training)
        "api_key":      config.GEMINI_API_KEY,
        "model_name":   config.GEMINI_MODEL,
        "temperature":  0.1,   # low temperature for deterministic SQL

        # ChromaDB persistent directory
        "path": str(chromadb_path),
    })

    if reset:
        print("Resetting ChromaDB collection…")
        try:
            vn.chroma_client.delete_collection("vanna")
        except Exception:  # noqa: BLE001
            pass  # collection may not exist yet

    return vn


# ---------------------------------------------------------------------------
# Training run
# ---------------------------------------------------------------------------

def train(vn: BixbyVanna, *, quiet: bool = False) -> None:
    """Feed DDL, documentation, and Q→SQL pairs into Vanna's ChromaDB store."""
    from training.common_pairs import DDL_SQLITE, DOCUMENTATION, iter_sqlite_pairs

    def _log(msg: str) -> None:
        if not quiet:
            print(msg)

    # 1. DDL ────────────────────────────────────────────────────────────────
    _log("\n[1/3] Training on SQLite DDL…")
    vn.train(ddl=DDL_SQLITE)
    _log("      DDL stored.")

    # 2. Documentation strings ───────────────────────────────────────────────
    _log("\n[2/3] Training on business documentation...")
    for i, doc in enumerate(DOCUMENTATION, 1):
        vn.train(documentation=doc)
        _log(f"      doc {i}/{len(DOCUMENTATION)} stored.")

    # 3. Question -> SQL pairs
    pairs = iter_sqlite_pairs()
    _log(f"\n[3/3] Training on {len(pairs)} Q->SQL pairs...")
    for i, (question, sql) in enumerate(pairs, 1):
        vn.train(question=question, sql=sql)
        _log(f"      [{i:02d}/{len(pairs)}] {question[:70]}")

    _log("\nTraining complete.")
    _log(f"ChromaDB path: {_get_chromadb_path()}")


def verify_training_data(vn: BixbyVanna) -> None:
    """Print a summary of what is stored in the ChromaDB collection."""
    try:
        data = vn.get_training_data()
        if data is None or data.empty:
            print("  (no training data found)")
            return
        counts = data["training_data_type"].value_counts().to_dict()
        print(f"  Training data in ChromaDB: {counts}")
    except Exception as exc:  # noqa: BLE001
        print(f"  WARNING: could not read training data — {exc}")


# ---------------------------------------------------------------------------
# Interactive Q&A (--ask mode)
# ---------------------------------------------------------------------------

def interactive_ask(vn: BixbyVanna) -> None:
    """Simple REPL to test SQL generation against the local SQLite database."""
    import sqlite3
    import pandas as pd

    db_path = _ROOT / config.SQLITE_PATH
    if not db_path.exists():
        print(f"ERROR: local.db not found at {db_path}")
        print("Run: python -m synthetic.generate")
        return

    print("\nBixby Dashboard AI — local SQL Q&A (Ctrl-C to quit)")
    print(f"Database: {db_path}")
    print("=" * 60)

    con = sqlite3.connect(str(db_path))

    while True:
        try:
            question = input("\nQuestion: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBye.")
            break

        if not question:
            continue

        try:
            sql = vn.generate_sql(question=question)
            print(f"\nSQL:\n{sql}\n")
            df = pd.read_sql_query(sql, con)
            print(df.to_string(index=False))
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: {exc}")

    con.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    reset  = "--reset" in sys.argv
    ask    = "--ask"   in sys.argv
    quiet  = "--quiet" in sys.argv

    print("=" * 60)
    print("Bixby Dashboard AI - Vanna local training")
    print(f"  ChromaDB path : {_get_chromadb_path()}")
    print(f"  LLM model     : {config.GEMINI_MODEL}")
    print(f"  Reset store   : {reset}")
    print("=" * 60)

    vn = build_vanna(reset=reset)

    train(vn, quiet=quiet)

    print("\nVerifying stored training data...")
    verify_training_data(vn)

    if ask:
        interactive_ask(vn)
