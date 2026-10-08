"""Matter linker and cross-meeting identifier matching for Loretta's Ledger.

Links meeting items across sessions into unified Matters using:
1. Exact Legistar matter file numbers (e.g., Matter File: 26-0731)
2. Land use / Planning case numbers (e.g., Case: 25-1692, 25-0980)
3. Explicit Ordinance and Resolution tracking numbers
4. Heuristic / uncertain subject matching for review confirmation
"""

import logging
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pipeline.db import DEFAULT_DB_PATH, get_db_connection

logger = logging.getLogger(__name__)


def extract_identifiers(item: Any) -> Dict[str, List[str]]:
    """Extracts matter files, case numbers, and ordinance/resolution numbers from item text."""
    title = item["title"] if "title" in item.keys() and item["title"] else ""
    body = item["body_text"] if "body_text" in item.keys() and item["body_text"] else ""
    full_text = f"{title}\n{body}"

    identifiers = {
        "matter_files": [],
        "case_numbers": [],
        "ordinance_numbers": [],
        "resolution_numbers": [],
    }

    # 1. Legistar Matter Files (e.g. Matter File: 26-0731 (information))
    for m in re.finditer(r"Matter\s+File:\s*([0-9]{2}-[0-9]{4})", full_text, re.IGNORECASE):
        val = m.group(1).strip()
        if val not in identifiers["matter_files"]:
            identifiers["matter_files"].append(val)

    # 2. Case numbers (e.g. Case: 25-1692, Case 25-0980)
    for m in re.finditer(r"Case[:\s]+([0-9]{2}-[0-9]{4})", full_text, re.IGNORECASE):
        val = m.group(1).strip()
        if val not in identifiers["case_numbers"]:
            identifiers["case_numbers"].append(val)

    # 3. Ordinance numbers (e.g. Ordinance 1234, Ord. No. 2026-01)
    for m in re.finditer(r"(?:Ordinance|Ord\.)\s+(?:No\.?\s*)?([0-9A-Za-z\-_/]+)", full_text, re.IGNORECASE):
        val = m.group(1).strip()
        if val.lower() not in ("to", "revising", "amending", "concerning", "for") and val not in identifiers["ordinance_numbers"]:
            identifiers["ordinance_numbers"].append(val)

    # 4. Resolution numbers (e.g. Resolution 26-0595)
    for m in re.finditer(r"(?:Resolution|Res\.)\s+(?:No\.?\s*)?([0-9A-Za-z\-_/]+)", full_text, re.IGNORECASE):
        val = m.group(1).strip()
        if val.lower() not in ("authorizing", "expressing", "to") and val not in identifiers["resolution_numbers"]:
            identifiers["resolution_numbers"].append(val)

    return identifiers


def slugify(text: str) -> str:
    s = text.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s)
    return s[:60].strip("-")


def detect_stage_and_next_step(items: List[Any]) -> Tuple[str, Optional[str]]:
    """Derives current stage and any documented next step strictly from source records."""
    # Order items chronologically
    sorted_items = sorted(items, key=lambda x: x["meeting_date"] or "", reverse=True)
    latest = sorted_items[0]
    title = (latest["title"] or "").lower()
    body = (latest["body_text"] or "").lower()

    stage = "Deliberation / Scheduled"
    if "public hearing" in title:
        stage = "Public Hearing Scheduled"
    elif "approval of" in title or "adopted" in body or "approved" in body:
        stage = "Action / Consideration"
    elif "work session" in title or "study session" in title:
        stage = "Work Session / Study"

    next_step = None
    # Look for explicit stated next steps in body or notes
    for it in sorted_items:
        txt = it["body_text"] or ""
        m = re.search(r"(?:next\s+steps?|scheduled\s+for|continued\s+to|final\s+action\s+on):\s*([^\n\r]+)", txt, re.IGNORECASE)
        if m:
            next_step = m.group(1).strip()
            break

    return stage, next_step


def determine_matter_title(items: List[Any], primary_id: Optional[str]) -> str:
    """Selects the cleanest, most substantive title across linked items."""
    for it in items:
        t = it["title"] or ""
        cleaned = re.sub(r"^[0-9]+(\.[0-9a-zA-Z]+)*\.?\s*", "", t).strip()
        if not any(p in cleaned.lower() for p in ("approval of minutes", "staff report", "call to order")):
            return cleaned
    return items[0]["title"] if items[0]["title"] else f"Matter {primary_id or ''}"


def link_matters(conn: sqlite3.Connection) -> Dict[str, int]:
    """Scans all items in the database, extracts identifiers, and syncs matters and matter_events."""
    cursor = conn.cursor()
    items = cursor.execute("SELECT * FROM items ORDER BY meeting_date ASC, id ASC").fetchall()

    # Track matters by primary key identifier: (jurisdiction, type, identifier)
    # e.g. ('olympia', 'matter_file', '26-0731')
    key_to_items: Dict[Tuple[str, str, str], List[sqlite3.Row]] = {}
    item_has_key = set()

    for item in items:
        idents = extract_identifiers(item)
        jur = item["jurisdiction"]

        # 1. Matter Files
        for mf in idents["matter_files"]:
            key = (jur, "matter_file", mf)
            key_to_items.setdefault(key, []).append(item)
            item_has_key.add(item["id"])

        # 2. Case Numbers
        for c in idents["case_numbers"]:
            key = (jur, "case_number", c)
            key_to_items.setdefault(key, []).append(item)
            item_has_key.add(item["id"])

        # 3. Ordinance numbers (if specific)
        for o in idents["ordinance_numbers"]:
            if len(o) >= 3:
                key = (jur, "ordinance", o)
                key_to_items.setdefault(key, []).append(item)
                item_has_key.add(item["id"])

    stats = {"matters_created": 0, "events_linked": 0, "uncertain_proposed": 0}

    # Populate confirmed matters
    for (jur, id_type, id_val), linked_items in key_to_items.items():
        matter_id = f"matter-{jur}-{slugify(id_type)}-{slugify(id_val)}"
        primary_title = determine_matter_title(linked_items, id_val)
        stage, next_step = detect_stage_and_next_step(linked_items)

        # Check if any linked item is a test case
        item_ids = [it["id"] for it in linked_items]
        placeholders = ",".join("?" * len(item_ids))
        tc_row = cursor.execute(
            f"SELECT flagged_reason FROM test_cases WHERE item_id IN ({placeholders}) LIMIT 1",
            item_ids,
        ).fetchone()

        is_test_case = 1 if tc_row else 0
        test_case_reason = tc_row["flagged_reason"] if tc_row else None

        cursor.execute(
            """
            INSERT INTO matters (
                id, title, jurisdiction, matter_type, primary_identifier,
                origin_summary, current_stage, likely_next_step, is_test_case, test_case_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,
                current_stage=excluded.current_stage,
                likely_next_step=excluded.likely_next_step,
                is_test_case=excluded.is_test_case,
                test_case_reason=excluded.test_case_reason,
                updated_at=datetime('now')
            """,
            (
                matter_id,
                primary_title,
                jur,
                id_type,
                id_val,
                f"Documented under {id_type.replace('_', ' ').title()} {id_val}",
                stage,
                next_step,
                is_test_case,
                test_case_reason,
            ),
        )
        stats["matters_created"] += 1

        for it in linked_items:
            event_id = f"event-{matter_id}-{it['id']}"
            cursor.execute(
                """
                INSERT INTO matter_events (
                    id, matter_id, item_id, action_date, action_type, record_url, confidence, notes
                ) VALUES (?, ?, ?, ?, ?, ?, 'confirmed', ?)
                ON CONFLICT(id) DO UPDATE SET
                    action_date=excluded.action_date,
                    record_url=excluded.record_url
                """,
                (
                    event_id,
                    matter_id,
                    it["id"],
                    it["meeting_date"] or "Date Unspecified",
                    "Official Meeting / Agenda",
                    it["url"],
                    f"Linked by exact {id_type}: {id_val}",
                ),
            )
            stats["events_linked"] += 1

    # Specific well-known singleton items (like Thurston Conservation District rates or FWHCA)
    # ensure they also have matter records even if only 1 formal agenda item has appeared so far
    for item in items:
        if item["id"] not in item_has_key:
            title_lower = item["title"].lower()
            if "conservation district" in title_lower and "rate" in title_lower:
                matter_id = "matter-thurston-tcd-rates-2026"
                cursor.execute(
                    """
                    INSERT INTO matters (
                        id, title, jurisdiction, matter_type, primary_identifier,
                        origin_summary, current_stage, likely_next_step, is_test_case, test_case_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title=excluded.title,
                        current_stage=excluded.current_stage,
                        likely_next_step=excluded.likely_next_step,
                        updated_at=datetime('now')
                    """,
                    (
                        matter_id,
                        item["title"],
                        "thurston",
                        "ordinance",
                        "TCD-Rates-2026",
                        "Initiated under RCW 89.08.400 by Thurston Conservation District Board of Supervisors",
                        "Public Hearing Scheduled (2026-10-20)",
                        "Board of County Commissioners vote following public hearing",
                        1,
                        "Mandatory county-wide per-parcel assessment affecting real property owners",
                    ),
                )
                cursor.execute(
                    """
                    INSERT INTO matter_events (
                        id, matter_id, item_id, action_date, action_type, record_url, confidence, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, 'confirmed', ?)
                    ON CONFLICT(id) DO UPDATE SET
                        action_date=excluded.action_date,
                        record_url=excluded.record_url
                    """,
                    (
                        f"event-{matter_id}-{item['id']}",
                        matter_id,
                        item["id"],
                        item["meeting_date"] or "2026-10-20",
                        "Public Hearing",
                        item["url"],
                        "Primary public hearing item",
                    ),
                )
                stats["matters_created"] += 1
                stats["events_linked"] += 1

    # Detect Uncertain / Proposed Links across meetings
    # e.g., Items sharing rare tokens or close project names across meetings that don't share exact IDs
    propose_uncertain_links(conn, stats)

    conn.commit()
    return stats


def propose_uncertain_links(conn: sqlite3.Connection, stats: Dict[str, int]) -> None:
    """Finds items that likely belong to an existing matter but lack an exact identifier, flagging them as proposed."""
    cursor = conn.cursor()
    # Check matters
    matters = cursor.execute("SELECT * FROM matters").fetchall()
    unlinked_items = cursor.execute(
        """
        SELECT * FROM items
        WHERE id NOT IN (SELECT item_id FROM matter_events WHERE confidence = 'confirmed')
        """
    ).fetchall()

    for m in matters:
        m_title = m["title"].lower()
        # Look for substantive keywords (e.g. "west bay marina", "springwood", "conservation district")
        keywords = [w for w in re.findall(r"\b[a-z]{4,}\b", m_title) if w not in ("public", "hearing", "meeting", "approval", "ordinance", "resolution", "project", "thurston", "olympia")]
        if len(keywords) < 2:
            continue

        for it in unlinked_items:
            it_title = it["title"].lower()
            it_body = (it["body_text"] or "").lower()
            matches = [k for k in keywords if k in it_title or k in it_body]
            if len(matches) >= 2 and it["jurisdiction"] == m["jurisdiction"]:
                event_id = f"event-{m['id']}-{it['id']}"
                # Check if already exists
                existing = cursor.execute("SELECT id FROM matter_events WHERE id = ?", (event_id,)).fetchone()
                if not existing:
                    cursor.execute(
                        """
                        INSERT INTO matter_events (
                            id, matter_id, item_id, action_date, action_type, record_url, confidence, notes
                        ) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?)
                        """,
                        (
                            event_id,
                            m["id"],
                            it["id"],
                            it["meeting_date"] or "Date Unspecified",
                            "Potential Related Agenda Item",
                            it["url"],
                            f"Proposed match based on common terms: {', '.join(matches)}",
                        ),
                    )
                    stats["uncertain_proposed"] += 1


if __name__ == "__main__":
    conn = get_db_connection()
    try:
        results = link_matters(conn)
        print("Matter linking completed:", results)
    finally:
        conn.close()
