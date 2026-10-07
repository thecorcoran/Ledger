"""Draft generator for briefs, action pages, and test case dossiers."""

import argparse
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from pipeline.db import DEFAULT_DB_PATH, get_db_connection
from pipeline.upstream.chains import get_item_lineage, format_lineage_display

logger = logging.getLogger(__name__)


def generate_brief_markdown(item: Dict[str, Any], refs: List[Dict[str, Any]], lineage_display: str) -> str:
    """Generates a structured policy brief adhering strictly to Loretta's Ledger Litmus Test."""
    title = item["title"]
    jur = item["jurisdiction"].capitalize()
    date_str = item["meeting_date"] or "Date not specified"
    url = item["url"]
    body = item["body_text"] or ""
    deadline = item["comment_deadline"] or "Check meeting agenda for registration cutoff"

    # Upstream influence section
    if refs or lineage_display:
        upstream_parts = []
        if lineage_display:
            upstream_parts.append(f"**Documented Lineage Chain:**\n```\n{lineage_display}\n```")
        for r in refs:
            upstream_parts.append(
                f"- **{r['actor_name']}** ({r['upstream_type']}): Influence operating via `{r['mechanism']}`. "
                f"Evidence: \"{r['evidence_ref']}\" [Source Document]({r['evidence_url']})"
            )
        upstream_section = "\n\n".join(upstream_parts)
    else:
        upstream_section = "No upstream source identified in the documents reviewed."

    return f"""# Brief: {title}

**Jurisdiction**: {jur}  
**Meeting Date**: {date_str}  
**Original Source**: [{jur} Official Record]({url})  

## Summary (facts only)
The {jur} governing body has scheduled consideration of: {title}.
Official record details:
{body}

## Litmus Test Evaluation
1. **Subsidiarity**: [Rating: Neutral / unclear]. Is this decided at the most local level? Upstream tracking indicates: {upstream_section.splitlines()[0] if refs else 'local initiative'}. [Source: {url}]
2. **Ownership**: [Rating: Neutral / unclear]. Effect on ability of ordinary families to own and retain land, homes, or shops: not stated in source.
3. **Small and local vs. large and distant**: [Rating: Neutral / unclear]. Documents do not explicitly detail disparity between small local merchants versus institutional players.
4. **Family and household**: [Rating: Neutral / unclear]. Support for household self-reliance versus programmatic agency replacement: not stated in source.
5. **Cost and who pays**: [Rating: Neutral / unclear]. Specific distribution of fees or compliance burden among small versus large owners: not stated in source.
6. **Consent and process**: [Rating: Supports]. Matter noticed on public agenda with opportunity for public attendance and comment prior to final action. [Source: {url}]
7. **Reversibility and accountability**: [Rating: Supports]. Action remains under jurisdiction of elected local officials who remain publicly answerable. [Source: {url}]
8. **Place**: [Rating: Neutral / unclear]. Impact on unique character and longstanding residents of neighborhood: not stated in source.

## Who's Behind This? (Upstream Influence)
{upstream_section}

## Overall Analysis
*(Analysis)*: This proposal represents formal policy action by {jur}. Citizens should examine whether the financial and regulatory conditions align with local self-determination and people-first policy principles.

## Plain-Language Effect for Residents
This action establishes local regulatory or financial standards directly touching {jur} residents. Review supporting attachments to evaluate direct impacts on property taxes, service fees, and local land rights.

## Next Step for Citizens
- **Meeting / Public Hearing**: {date_str}
- **Public Comment Deadline**: {deadline}
- **Official Documentation**: [Review Complete Packet]({url})
"""


def generate_action_page_markdown(item: Dict[str, Any], refs: List[Dict[str, Any]]) -> str:
    """Generates a plain-language action page for community participation."""
    title = item["title"]
    jur = item["jurisdiction"].capitalize()
    date_str = item["meeting_date"] or "Upcoming"
    url = item["url"]
    deadline = item["comment_deadline"] or "Two hours before scheduled meeting start"

    upstream_line = "Local municipal initiative"
    if refs:
        actors = ", ".join([f"{r['actor_name']} ({r['mechanism']})" for r in refs])
        upstream_line = f"Upstream influence identified: {actors}"

    return f"""# Citizen Action: {title}

**Key Date**: {date_str}  
**Public Comment Cutoff**: {deadline}  
**Jurisdiction**: {jur}  

### What's Happening
The {jur} council or commission is scheduled to review and act upon:
> **{title}**

### How It Affects You & Your Household
Local decisions set the rules, utility rates, and fees paid by ordinary households and small independent businesses. Participating before decisions are enacted ensures resident concerns are recorded in the public record.

### Who's Behind This?
{upstream_line}

### How to Have Your Say
- **Meeting Date**: {date_str}
- **Public Comment Cutoff**: {deadline}
- **Submit Comment**: Email comments to the local clerk or register for virtual attendance.
- **Official Agenda Packet**: [View Documents & Supporting Attachments]({url})
"""


def draft_item(conn, item_id: str) -> Dict[str, int]:
    """Generates brief and action page drafts for an item and saves with reviewed = 0."""
    item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        return {"created": 0}

    item_dict = dict(item)

    # Get refs & lineage
    refs = conn.execute(
        """
        SELECT r.*, a.name as actor_name, a.upstream_type
        FROM upstream_refs r
        JOIN upstream_actors a ON r.actor_id = a.id
        WHERE r.item_id = ?
        """,
        (item_id,),
    ).fetchall()
    refs_list = [dict(r) for r in refs]

    lineage = get_item_lineage(conn, item_id)
    lineage_str = format_lineage_display(lineage) if lineage else ""

    brief_md = generate_brief_markdown(item_dict, refs_list, lineage_str)
    action_md = generate_action_page_markdown(item_dict, refs_list)

    # Insert or update drafts
    for kind, md in (("brief", brief_md), ("action_page", action_md)):
        conn.execute(
            """
            INSERT INTO drafts (item_id, kind, markdown, reviewed, reviewed_at)
            VALUES (?, ?, ?, 0, NULL)
            """,
            (item_id, kind, md),
        )

    conn.commit()
    return {"created": 2}


def run_drafts(conn, limit: int = 10) -> int:
    """Generates drafts for priority items (test cases, upcoming hearings, upstream influence items)."""
    # Select priority items first: items with upstream refs or test cases
    query = """
    SELECT DISTINCT i.id
    FROM items i
    LEFT JOIN upstream_refs r ON i.id = r.item_id
    LEFT JOIN test_cases t ON i.id = t.item_id
    WHERE i.id NOT IN (SELECT DISTINCT item_id FROM drafts)
    ORDER BY (CASE WHEN t.item_id IS NOT NULL THEN 1 WHEN r.item_id IS NOT NULL THEN 2 ELSE 3 END),
             i.meeting_date DESC
    LIMIT ?
    """
    rows = conn.execute(query, (limit,)).fetchall()
    total_created = 0

    for r in rows:
        res = draft_item(conn, r["id"])
        total_created += res["created"]

    return total_created


def main():
    parser = argparse.ArgumentParser(description="Generate drafts for Loretta's Ledger")
    parser.add_argument("--item-id", type=str, default=None, help="Generate drafts for specific item ID")
    parser.add_argument("--limit", type=int, default=10, help="Number of priority items to draft")

    args = parser.parse_args()

    conn = get_db_connection()
    try:
        if args.item_id:
            res = draft_item(conn, args.item_id)
            print(f"Drafts generated for {args.item_id}: {res['created']} drafts created.")
        else:
            print(f"Generating drafts for up to {args.limit} priority items...")
            count = run_drafts(conn, limit=args.limit)
            print(f"Draft generation complete. Total draft documents created: {count}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
