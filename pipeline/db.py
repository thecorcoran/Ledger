"""Database schema and connection management for Loretta's Ledger."""

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional
import yaml

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "ledger.db"
DEFAULT_UPSTREAM_CONFIG = Path(__file__).resolve().parent.parent / "config" / "upstream.yaml"

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    jurisdiction TEXT NOT NULL CHECK (jurisdiction IN ('olympia', 'thurston')),
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    published_at TEXT,
    body_text TEXT,
    meeting_date TEXT,
    comment_deadline TEXT,
    status TEXT DEFAULT 'active',
    display_title TEXT,
    hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_jurisdiction ON items(jurisdiction);
CREATE INDEX IF NOT EXISTS idx_items_meeting_date ON items(meeting_date);
CREATE INDEX IF NOT EXISTS idx_items_created_at ON items(created_at);
CREATE INDEX IF NOT EXISTS idx_items_hash ON items(hash);

CREATE TABLE IF NOT EXISTS item_topics (
    item_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    PRIMARY KEY (item_id, topic),
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_item_topics_topic ON item_topics(topic);

CREATE TABLE IF NOT EXISTS upstream_actors (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    upstream_type TEXT NOT NULL CHECK (upstream_type IN (
        'state_law',
        'state_agency',
        'regional_body',
        'funding_condition',
        'model_policy',
        'outside_group',
        'peer_jurisdiction',
        'court_or_board',
        'federal'
    )),
    aliases TEXT, -- JSON array of string aliases
    home_url TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS upstream_docs (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    published_at TEXT,
    body_text TEXT,
    doc_kind TEXT NOT NULL CHECK (doc_kind IN (
        'statute',
        'rule',
        'guidance',
        'model_policy',
        'ruling',
        'peer_ordinance',
        'other'
    )),
    FOREIGN KEY (actor_id) REFERENCES upstream_actors(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_upstream_docs_actor ON upstream_docs(actor_id);

CREATE TABLE IF NOT EXISTS upstream_refs (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    upstream_doc_id TEXT,
    mechanism TEXT NOT NULL CHECK (mechanism IN (
        'mandate',
        'funding_strings',
        'template',
        'approval_gate',
        'litigation_threat_or_ruling',
        'advisory',
        'unknown'
    )),
    evidence_url TEXT,
    evidence_ref TEXT,
    confidence TEXT NOT NULL CHECK (confidence IN ('documented', 'inferred')),
    parent_ref_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE,
    FOREIGN KEY (actor_id) REFERENCES upstream_actors(id) ON DELETE RESTRICT,
    FOREIGN KEY (upstream_doc_id) REFERENCES upstream_docs(id) ON DELETE SET NULL,
    FOREIGN KEY (parent_ref_id) REFERENCES upstream_refs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_upstream_refs_item ON upstream_refs(item_id);
CREATE INDEX IF NOT EXISTS idx_upstream_refs_actor ON upstream_refs(actor_id);

CREATE TABLE IF NOT EXISTS similarity_matches (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    upstream_doc_id TEXT NOT NULL,
    score REAL NOT NULL,
    matched_passages TEXT, -- JSON array
    reviewed INTEGER NOT NULL DEFAULT 0 CHECK (reviewed IN (0, 1)),
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE,
    FOREIGN KEY (upstream_doc_id) REFERENCES upstream_docs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_similarity_matches_item ON similarity_matches(item_id);

CREATE TABLE IF NOT EXISTS test_cases (
    item_id TEXT PRIMARY KEY,
    flagged_reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('watching', 'decided', 'appealed', 'closed')),
    notes TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('action_page', 'brief', 'test_case', 'influence_page')),
    markdown TEXT NOT NULL,
    reviewed INTEGER NOT NULL DEFAULT 0 CHECK (reviewed IN (0, 1)),
    reviewed_at TEXT,
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_drafts_item ON drafts(item_id);

CREATE TABLE IF NOT EXISTS matters (
    id TEXT PRIMARY KEY,
    stable_id TEXT,
    title TEXT NOT NULL,
    plain_title TEXT,
    official_title TEXT,
    jurisdiction TEXT NOT NULL CHECK (jurisdiction IN ('olympia', 'thurston')),
    matter_type TEXT,
    primary_identifier TEXT,
    origin_summary TEXT,
    current_stage TEXT,
    lifecycle_status TEXT,
    archive_state TEXT NOT NULL DEFAULT 'active',
    manual_archive_override TEXT,
    last_action_date TEXT,
    last_action_body TEXT,
    last_action_summary TEXT,
    last_action_url TEXT,
    next_action_date TEXT,
    next_action_body TEXT,
    next_action_text TEXT NOT NULL DEFAULT 'not yet posted',
    likely_next_step TEXT,
    is_test_case INTEGER NOT NULL DEFAULT 0,
    test_case_reason TEXT,
    needs_review INTEGER NOT NULL DEFAULT 1,
    whats_new_summary TEXT,
    woke_from_dormant INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_matters_jurisdiction ON matters(jurisdiction);
CREATE INDEX IF NOT EXISTS idx_matters_primary_identifier ON matters(primary_identifier);
CREATE INDEX IF NOT EXISTS idx_matters_archive_state ON matters(archive_state);

CREATE TABLE IF NOT EXISTS matter_events (
    id TEXT PRIMARY KEY,
    matter_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    action_date TEXT NOT NULL,
    body_name TEXT,
    action_type TEXT,
    outcome_text TEXT,
    motion_text TEXT,
    vote_tally TEXT,
    record_url TEXT NOT NULL,
    minutes_url TEXT,
    confidence TEXT NOT NULL DEFAULT 'confirmed' CHECK (confidence IN ('confirmed', 'proposed', 'rejected')),
    match_reason TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (matter_id) REFERENCES matters(id) ON DELETE CASCADE,
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_matter_events_matter ON matter_events(matter_id);
CREATE INDEX IF NOT EXISTS idx_matter_events_item ON matter_events(item_id);
CREATE INDEX IF NOT EXISTS idx_matter_events_confidence ON matter_events(confidence);

CREATE TABLE IF NOT EXISTS matter_versions (
    id TEXT PRIMARY KEY,
    matter_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    version_number INTEGER NOT NULL,
    version_date TEXT NOT NULL,
    version_label TEXT NOT NULL,
    doc_url TEXT NOT NULL,
    what_changed TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (matter_id) REFERENCES matters(id) ON DELETE CASCADE,
    FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_matter_versions_matter ON matter_versions(matter_id);

CREATE TABLE IF NOT EXISTS matter_relations (
    parent_matter_id TEXT NOT NULL,
    child_matter_id TEXT NOT NULL,
    relation_type TEXT NOT NULL DEFAULT 'parent_child' CHECK (relation_type IN ('parent_child', 'companion')),
    notes TEXT,
    PRIMARY KEY (parent_matter_id, child_matter_id),
    FOREIGN KEY (parent_matter_id) REFERENCES matters(id) ON DELETE CASCADE,
    FOREIGN KEY (child_matter_id) REFERENCES matters(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS matter_briefs (
    matter_id TEXT PRIMARY KEY,
    item_id TEXT,
    markdown TEXT NOT NULL,
    prior_markdown TEXT,
    updated_date TEXT NOT NULL,
    changelog_json TEXT NOT NULL DEFAULT '[]',
    reviewed INTEGER NOT NULL DEFAULT 0 CHECK (reviewed IN (0, 1)),
    reviewed_at TEXT,
    approved_hash TEXT,
    published_hash TEXT,
    FOREIGN KEY (matter_id) REFERENCES matters(id) ON DELETE CASCADE
);
"""


def _ensure_migrations(conn: sqlite3.Connection) -> None:
    """Ensures all new columns and tables exist on an existing SQLite database."""
    cursor = conn.cursor()

    existing_tables = {
        r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }

    if "matter_briefs" in existing_tables:
        cursor.execute("PRAGMA table_info(matter_briefs)")
        mb_cols = {row[1] for row in cursor.fetchall()}
        if "prior_markdown" not in mb_cols:
            cursor.execute("ALTER TABLE matter_briefs ADD COLUMN prior_markdown TEXT")

    if "items" in existing_tables:
        cursor.execute("PRAGMA table_info(items)")
        item_cols = {row[1] for row in cursor.fetchall()}
        if "display_title" not in item_cols:
            cursor.execute("ALTER TABLE items ADD COLUMN display_title TEXT")

    if "matters" in existing_tables:
        cursor.execute("PRAGMA table_info(matters)")
        matter_cols = {row[1] for row in cursor.fetchall()}
        new_matter_cols = [
            ("stable_id", "TEXT"),
            ("plain_title", "TEXT"),
            ("official_title", "TEXT"),
            ("lifecycle_status", "TEXT"),
            ("archive_state", "TEXT DEFAULT 'active'"),
            ("manual_archive_override", "TEXT"),
            ("last_action_date", "TEXT"),
            ("last_action_body", "TEXT"),
            ("last_action_summary", "TEXT"),
            ("last_action_url", "TEXT"),
            ("next_action_date", "TEXT"),
            ("next_action_body", "TEXT"),
            ("next_action_text", "TEXT DEFAULT 'not yet posted'"),
            ("needs_review", "INTEGER DEFAULT 1"),
            ("whats_new_summary", "TEXT"),
            ("woke_from_dormant", "INTEGER DEFAULT 0"),
        ]
        for col_name, col_type in new_matter_cols:
            if col_name not in matter_cols:
                cursor.execute(f"ALTER TABLE matters ADD COLUMN {col_name} {col_type}")

    if "matter_events" in existing_tables:
        cursor.execute("PRAGMA table_info(matter_events)")
        ev_cols = {row[1] for row in cursor.fetchall()}
        new_ev_cols = [
            ("body_name", "TEXT"),
            ("outcome_text", "TEXT"),
            ("motion_text", "TEXT"),
            ("vote_tally", "TEXT"),
            ("minutes_url", "TEXT"),
            ("match_reason", "TEXT"),
        ]
        for col_name, col_type in new_ev_cols:
            if col_name not in ev_cols:
                cursor.execute(f"ALTER TABLE matter_events ADD COLUMN {col_name} {col_type}")

    conn.executescript(SCHEMA_SQL)

    # Check if matter_events table has old CHECK constraint disallowing 'rejected'
    table_sql_row = cursor.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='matter_events'"
    ).fetchone()
    if table_sql_row and "'rejected'" not in (table_sql_row[0] or ""):
        cursor.executescript(
            """
            PRAGMA foreign_keys = OFF;
            CREATE TABLE matter_events_new (
                id TEXT PRIMARY KEY,
                matter_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                action_date TEXT NOT NULL,
                body_name TEXT,
                action_type TEXT,
                outcome_text TEXT,
                motion_text TEXT,
                vote_tally TEXT,
                record_url TEXT NOT NULL,
                minutes_url TEXT,
                confidence TEXT NOT NULL DEFAULT 'confirmed' CHECK (confidence IN ('confirmed', 'proposed', 'rejected')),
                match_reason TEXT,
                notes TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (matter_id) REFERENCES matters(id) ON DELETE CASCADE,
                FOREIGN KEY (item_id) REFERENCES items(id) ON DELETE CASCADE
            );
            INSERT INTO matter_events_new (
                id, matter_id, item_id, action_date, body_name, action_type, outcome_text,
                motion_text, vote_tally, record_url, minutes_url, confidence, match_reason, notes, created_at
            )
            SELECT
                id, matter_id, item_id, action_date, body_name, action_type, outcome_text,
                motion_text, vote_tally, record_url, minutes_url, confidence, match_reason, notes, created_at
            FROM matter_events;
            DROP TABLE matter_events;
            ALTER TABLE matter_events_new RENAME TO matter_events;
            CREATE INDEX IF NOT EXISTS idx_matter_events_matter ON matter_events(matter_id);
            CREATE INDEX IF NOT EXISTS idx_matter_events_item ON matter_events(item_id);
            CREATE INDEX IF NOT EXISTS idx_matter_events_confidence ON matter_events(confidence);
            PRAGMA foreign_keys = ON;
            """
        )

    conn.commit()


def get_db_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Returns a SQLite connection with row factory configured."""
    target_path = Path(db_path) if db_path else DEFAULT_DB_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    _ensure_migrations(conn)
    return conn


def init_db(db_path: Optional[Path] = None) -> None:
    """Initializes the database schema."""
    conn = get_db_connection(db_path)
    try:
        _ensure_migrations(conn)
    finally:
        conn.close()


def seed_upstream_actors(db_path: Optional[Path] = None, config_path: Optional[Path] = None) -> int:
    """Seeds upstream_actors from config/upstream.yaml."""
    conf_path = Path(config_path) if config_path else DEFAULT_UPSTREAM_CONFIG
    if not conf_path.exists():
        return 0

    with open(conf_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    actors = data.get("upstream_actors", [])
    if not actors:
        return 0

    conn = get_db_connection(db_path)
    count = 0
    try:
        for actor in actors:
            conn.execute(
                """
                INSERT INTO upstream_actors (id, name, upstream_type, aliases, home_url, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    upstream_type = excluded.upstream_type,
                    aliases = excluded.aliases,
                    home_url = excluded.home_url,
                    notes = excluded.notes
                """,
                (
                    actor["id"],
                    actor["name"],
                    actor["upstream_type"],
                    json.dumps(actor.get("aliases", [])),
                    actor.get("home_url"),
                    actor.get("notes"),
                ),
            )
            count += 1
        conn.commit()
    finally:
        conn.close()
    return count

