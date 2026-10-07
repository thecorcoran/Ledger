"""Editorial review CLI for approving, editing, or rejecting drafts and similarity matches."""

import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from pipeline.db import DEFAULT_DB_PATH, get_db_connection

logger = logging.getLogger(__name__)


def list_pending_reviews(conn):
    """Lists drafts and similarity matches waiting for editorial review."""
    drafts = conn.execute(
        """
        SELECT d.id, d.item_id, d.kind, d.reviewed, i.title, i.jurisdiction, i.meeting_date
        FROM drafts d
        JOIN items i ON d.item_id = i.id
        WHERE d.reviewed = 0
        ORDER BY d.id ASC
        """
    ).fetchall()

    sim_matches = conn.execute(
        """
        SELECT s.id, s.item_id, s.score, s.matched_passages, d.title as doc_title, i.title as item_title
        FROM similarity_matches s
        JOIN upstream_docs d ON s.upstream_doc_id = d.id
        JOIN items i ON s.item_id = i.id
        WHERE s.reviewed = 0
        """
    ).fetchall()

    print("\n" + "=" * 95)
    print(f"PENDING DRAFTS REQUIRING EDITORIAL REVIEW (Total: {len(drafts)})")
    print("=" * 95)
    if not drafts:
        print("  (No drafts pending review)")
    for d in drafts:
        print(f"[{d['id']}] ({d['jurisdiction'].upper()}) Kind: {d['kind'].upper()} | Date: {d['meeting_date']}")
        print(f"     Item: {d['title']}")
        print(f"     ID:   {d['item_id']}\n")

    print("=" * 95)
    print(f"PENDING SIMILARITY MATCHES (Total: {len(sim_matches)})")
    print("=" * 95)
    if not sim_matches:
        print("  (No similarity matches pending review)")
    for s in sim_matches:
        print(f"[{s['id']}] Score: {s['score']} | Model Doc: {s['doc_title']}")
        print(f"     Local Item: {s['item_title']}")
        print(f"     Passages:   {s['matched_passages']}\n")


def show_draft(conn, draft_id: int):
    """Displays full markdown of a specific draft."""
    row = conn.execute(
        """
        SELECT d.*, i.title as item_title, i.jurisdiction, i.meeting_date
        FROM drafts d
        JOIN items i ON d.item_id = i.id
        WHERE d.id = ?
        """,
        (draft_id,),
    ).fetchone()

    if not row:
        print(f"Draft not found: {draft_id}")
        return

    print("\n" + "=" * 95)
    print(f"DRAFT #{row['id']} | Kind: {row['kind'].upper()} | Reviewed: {'YES' if row['reviewed'] else 'NO (PENDING)'}")
    print(f"Item: {row['item_title']} ({row['jurisdiction'].upper()})")
    print("=" * 95 + "\n")
    print(row["markdown"])
    print("\n" + "=" * 95)


def approve_draft(conn, draft_id: int):
    """Approves a draft, marking it ready for public publication."""
    cursor = conn.execute(
        """
        UPDATE drafts
        SET reviewed = 1, reviewed_at = datetime('now')
        WHERE id = ?
        """,
        (draft_id,),
    )
    conn.commit()
    if cursor.rowcount > 0:
        print(f"Draft #{draft_id} successfully APPROVED for publication.")
    else:
        print(f"Draft #{draft_id} not found.")


def reject_draft(conn, draft_id: int):
    """Rejects and deletes a draft."""
    cursor = conn.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))
    conn.commit()
    if cursor.rowcount > 0:
        print(f"Draft #{draft_id} successfully REJECTED and deleted.")
    else:
        print(f"Draft #{draft_id} not found.")


def edit_draft(conn, draft_id: int, content_file: Path):
    """Replaces draft markdown with contents from an edited file."""
    if not content_file.exists():
        print(f"File not found: {content_file}")
        return

    with open(content_file, "r", encoding="utf-8") as f:
        new_md = f.read()

    cursor = conn.execute(
        """
        UPDATE drafts
        SET markdown = ?, reviewed = 0
        WHERE id = ?
        """,
        (new_md, draft_id),
    )
    conn.commit()
    if cursor.rowcount > 0:
        print(f"Draft #{draft_id} successfully updated with content from {content_file}.")
    else:
        print(f"Draft #{draft_id} not found.")


def main():
    parser = argparse.ArgumentParser(description="Editorial Review CLI for Loretta's Ledger")
    parser.add_argument("--list", action="store_true", help="List all pending drafts and similarity matches")
    parser.add_argument("--show", type=int, default=None, help="Display markdown content of draft ID")
    parser.add_argument("--approve", type=int, default=None, help="Approve draft ID for publication")
    parser.add_argument("--reject", type=int, default=None, help="Reject draft ID")
    parser.add_argument("--edit", type=int, default=None, help="Draft ID to edit")
    parser.add_argument("--file", type=Path, default=None, help="Path to markdown file for editing draft")

    args = parser.parse_args()

    conn = get_db_connection()
    try:
        if args.show:
            show_draft(conn, args.show)
        elif args.approve:
            approve_draft(conn, args.approve)
        elif args.reject:
            reject_draft(conn, args.reject)
        elif args.edit and args.file:
            edit_draft(conn, args.edit, args.file)
        else:
            list_pending_reviews(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

