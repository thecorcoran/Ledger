"""Resolves and registers cited upstream documents into upstream_docs table."""

import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional
import urllib.parse

from pipeline.db import DEFAULT_DB_PATH, get_db_connection

logger = logging.getLogger(__name__)

RCW_BASE_URL = "https://app.leg.wa.gov/rcw/default.aspx?cite="
WAC_BASE_URL = "https://app.leg.wa.gov/wac/default.aspx?cite="


def get_or_create_statute_doc(
    conn,
    citation: str,
    actor_id: str,
    doc_kind: str = "statute",
) -> str:
    """Creates or fetches an upstream_docs entry for a Washington statute or administrative code."""
    clean_cite = citation.strip()
    is_wac = doc_kind == "rule" or "WAC" in clean_cite.upper()

    # Extract clean citation number (e.g. "36.70A.040" or "246-290")
    num_match = re.search(r"([0-9A-Za-z]+(?:[\.\-][0-9A-Za-z]+)+)", clean_cite)
    cite_num = num_match.group(1) if num_match else clean_cite

    doc_id = f"doc-{'wac' if is_wac else 'rcw'}-{cite_num.replace('.', '-').replace('/', '-')}".lower()
    title = f"{'WAC' if is_wac else 'RCW'} {cite_num}"
    url = f"{WAC_BASE_URL if is_wac else RCW_BASE_URL}{cite_num}"

    cursor = conn.execute("SELECT id FROM upstream_docs WHERE id = ?", (doc_id,))
    existing = cursor.fetchone()
    if existing:
        return existing["id"]

    conn.execute(
        """
        INSERT INTO upstream_docs (id, actor_id, title, url, published_at, body_text, doc_kind)
        VALUES (?, ?, ?, ?, NULL, NULL, ?)
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            url = excluded.url
        """,
        (doc_id, actor_id, title, url, "rule" if is_wac else "statute"),
    )
    return doc_id
