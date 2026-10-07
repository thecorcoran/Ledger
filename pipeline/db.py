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
    hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_items_jurisdiction ON items(jurisdiction);
CREATE INDEX IF NOT EXISTS idx_items_meeting_date ON items(meeting_date);
CREATE INDEX IF NOT EXISTS idx_items_created_at ON items(created_at);

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
"""


def get_db_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Returns a SQLite connection with row factory configured."""
    target_path = Path(db_path) if db_path else DEFAULT_DB_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Optional[Path] = None) -> None:
    """Initializes the database schema."""
    conn = get_db_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
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
