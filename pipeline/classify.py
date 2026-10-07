"""Topic classification, comment deadline extraction, and test-case watch flagging."""

import argparse
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import yaml

from pipeline.db import DEFAULT_DB_PATH, get_db_connection

logger = logging.getLogger(__name__)

DEFAULT_TOPICS_PATH = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"

# Regex rules for detecting test-case watch criteria
TEST_CASE_PATTERNS = [
    (
        r"\b(hearing examiner|growth management hearings board|gmhb|appeal|lawsuit|petition for review)\b",
        "Hearing examiner or GMHB decision/appeal involving local policy",
    ),
    (
        r"\b(variance|exception|conditional use permit|special use permit)\b",
        "Variance or exception that others could cite as precedent",
    ),
    (
        r"\b(pilot\s+(?:program|project)|demonstration project|interlocal agreement.*funding.*tiny home)\b",
        "Pilot program or one-off agreement that could become standard",
    ),
    (
        r"\b(family farm|permit-exempt well|private well|group b well|water availability|agricultural lease)\b",
        "Land-use, well, or water decision directly affecting small owners or family agriculture",
    ),
    (
        r"\b(revisions to drinking water code|critical areas ordinance|wetlands.*draft code|fish and wildlife habitat.*draft code|new uses\s*&\s*standards)\b",
        "First application or fundamental revision of major regulatory code",
    ),
]


def load_topics_config(config_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    path = Path(config_path) if config_path else DEFAULT_TOPICS_PATH
    if not path.exists():
        logger.warning("Topics config not found at %s", path)
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("topics", [])


def tag_item_topics(
    title: str,
    body_text: str,
    topics_config: List[Dict[str, Any]],
) -> List[str]:
    """Tags an item with one or more topic IDs based on keyword matching."""
    matched_topics = []
    combined_text = f"{title}\n{body_text}".lower()

    for topic in topics_config:
        topic_id = topic.get("id")
        keywords = topic.get("keywords", [])
        for kw in keywords:
            # Word boundary regex matching to avoid substring false positives
            pattern = r"\b" + re.escape(kw.lower()) + r"\b"
            if re.search(pattern, combined_text):
                matched_topics.append(topic_id)
                break  # Tagged with this topic, move to next topic

    return matched_topics


def extract_comment_deadline(
    title: str,
    body_text: str,
    meeting_date: Optional[str],
) -> Optional[str]:
    """Extracts explicit public comment deadline dates or relative submission cutoffs."""
    combined = f"{title}\n{body_text}"

    # Pattern: "written comment deadline: October 15, 2026" or "comments accepted until October 15, 2026"
    m_explicit = re.search(
        r"(?:comment deadline|comments accepted until|submit comments by)[:\s]+([A-Za-z]+\s+\d{1,2},\s+\d{4})",
        combined,
        re.IGNORECASE,
    )
    if m_explicit:
        date_str = m_explicit.group(1)
        for fmt in ("%B %d, %Y", "%b %d, %Y"):
            try:
                dt = datetime.strptime(date_str, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                pass

    # Thurston 2 hours prior rule
    if "submitted up to 2 hours prior to meeting start" in combined.lower() and meeting_date:
        return f"{meeting_date} (2 hrs prior to start)"

    return None


def detect_test_case(
    title: str,
    body_text: str,
) -> Optional[Tuple[str, str]]:
    """Evaluates rule-based criteria for flagging an item as a precedent-setting test case."""
    combined = f"{title}\n{body_text}".lower()

    for pattern, reason in TEST_CASE_PATTERNS:
        match = re.search(pattern, combined)
        if match:
            note = f"Flagged by rule matching keyword '{match.group(0)}' in title or document body."
            return (reason, note)

    return None


def classify_item(
    conn,
    item_id: str,
    topics_config: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Classifies a single item: tags topics, updates deadline, and checks test case watch."""
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return {"error": "not_found"}

    title = row["title"]
    body_text = row["body_text"] or ""
    meeting_date = row["meeting_date"]

    # 1. Topic tagging
    topics = tag_item_topics(title, body_text, topics_config)
    # Remove existing topics for this item and re-insert
    conn.execute("DELETE FROM item_topics WHERE item_id = ?", (item_id,))
    for topic_id in topics:
        conn.execute(
            "INSERT INTO item_topics (item_id, topic) VALUES (?, ?)",
            (item_id, topic_id),
        )

    # 2. Comment deadline extraction
    deadline = extract_comment_deadline(title, body_text, meeting_date)
    if deadline and deadline != row["comment_deadline"]:
        conn.execute(
            "UPDATE items SET comment_deadline = ? WHERE id = ?",
            (deadline, item_id),
        )

    # 3. Test-case detection
    test_case_match = detect_test_case(title, body_text)
    is_test_case = False
    if test_case_match:
        is_test_case = True
        flagged_reason, notes = test_case_match
        conn.execute(
            """
            INSERT INTO test_cases (item_id, flagged_reason, status, notes, updated_at)
            VALUES (?, ?, 'watching', ?, datetime('now'))
            ON CONFLICT(item_id) DO UPDATE SET
                flagged_reason = excluded.flagged_reason,
                notes = excluded.notes,
                updated_at = datetime('now')
            """,
            (item_id, flagged_reason, notes),
        )

    return {
        "item_id": item_id,
        "topics": topics,
        "deadline": deadline,
        "is_test_case": is_test_case,
    }


def run_classify(
    db_path: Optional[Path] = None,
    topics_path: Optional[Path] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Runs classification across items in the database."""
    conn = get_db_connection(db_path)
    topics_config = load_topics_config(topics_path)

    stats = {
        "items_processed": 0,
        "topics_assigned": 0,
        "deadlines_found": 0,
        "test_cases_flagged": 0,
    }

    try:
        query = "SELECT id FROM items ORDER BY meeting_date DESC, id DESC"
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = conn.execute(query).fetchall()

        for r in rows:
            res = classify_item(conn, r["id"], topics_config)
            stats["items_processed"] += 1
            stats["topics_assigned"] += len(res.get("topics", []))
            if res.get("deadline"):
                stats["deadlines_found"] += 1
            if res.get("is_test_case"):
                stats["test_cases_flagged"] += 1

        conn.commit()
    finally:
        conn.close()

    return stats


def main():
    parser = argparse.ArgumentParser(description="Classify items by topic, deadline, and test-case watch")
    parser.add_argument("--all", action="store_true", help="Classify all items in the database")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of items to process")
    parser.add_argument("--item-id", type=str, default=None, help="Classify a specific item ID")

    args = parser.parse_args()

    if not args.all and not args.item_id:
        print("Please specify --all or --item-id <id>")
        return

    conn = get_db_connection()
    topics_config = load_topics_config()

    if args.item_id:
        res = classify_item(conn, args.item_id, topics_config)
        conn.commit()
        conn.close()
        print(f"\nClassification result for {args.item_id}:")
        print(f"  Topics: {', '.join(res['topics']) if res['topics'] else 'None'}")
        print(f"  Deadline: {res['deadline'] or 'None'}")
        print(f"  Test Case: {'YES' if res['is_test_case'] else 'NO'}")
    else:
        print("Running classification...")
        stats = run_classify(limit=args.limit)
        print("\nClassification Summary:")
        print(f"  Items processed:    {stats['items_processed']}")
        print(f"  Topics assigned:    {stats['topics_assigned']}")
        print(f"  Deadlines detected: {stats['deadlines_found']}")
        print(f"  Test cases flagged: {stats['test_cases_flagged']}")


if __name__ == "__main__":
    main()

