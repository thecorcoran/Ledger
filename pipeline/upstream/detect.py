"""Upstream influence detection for Loretta's Ledger."""

import argparse
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from pipeline.db import DEFAULT_DB_PATH, get_db_connection
from pipeline.upstream.fetch import get_or_create_statute_doc

logger = logging.getLogger(__name__)

# Mechanism detection patterns
MECHANISM_PATTERNS = [
    (
        r"\b(?:grant\s+(?:agreement|condition|contract|award)|funding\s+agreement|cdbg|community development block grant|interlocal agreement.*funding)\b",
        "funding_strings",
    ),
    (
        r"\b(?:required by|mandated by|pursuant to|in compliance with|shall adopt|statutory requirement|statute requires|state mandate|gma periodic update)\b",
        "mandate",
    ),
    (
        r"\b(?:model\s+(?:ordinance|policy|code)|template|sample\s+(?:ordinance|code)|mrsc model)\b",
        "template",
    ),
    (
        r"\b(?:subject to approval|approval of|commerce approval|ecology approval|certified by commerce|state approval|sign-off)\b",
        "approval_gate",
    ),
    (
        r"\b(?:board\s+(?:order|decision)|hearings board|appeal|remand|litigation|lawsuit|settlement|court order)\b",
        "litigation_threat_or_ruling",
    ),
    (
        r"\b(?:guidance|recommended by|recommendation|advisory|technical assistance|comment letter)\b",
        "advisory",
    ),
]


def detect_mechanism(text: str) -> str:
    """Detects the influence mechanism from surrounding text context."""
    text_lower = text.lower()
    for pattern, mechanism in MECHANISM_PATTERNS:
        if re.search(pattern, text_lower):
            return mechanism
    return "unknown"


def extract_evidence_excerpt(text: str, match_start: int, match_end: int, window: int = 120) -> str:
    """Extracts a readable sentence or surrounding excerpt around a detected match."""
    start = max(0, match_start - window)
    end = min(len(text), match_end + window)
    excerpt = text[start:end].strip()

    # Clean leading/trailing partial words if excerpt was truncated
    if start > 0:
        excerpt = "..." + excerpt[excerpt.find(" ") + 1 :] if " " in excerpt else excerpt
    if end < len(text):
        excerpt = excerpt[: excerpt.rfind(" ")] + "..." if " " in excerpt else excerpt

    return re.sub(r"\s+", " ", excerpt).strip()


def find_citations(text: str) -> List[Tuple[str, str, str, int, int]]:
    """Finds RCW and WAC citations in text.
    Returns: [(cite_type, cite_string, mapped_actor_id, start_idx, end_idx)]
    """
    citations = []

    # RCW pattern: e.g. "RCW 36.70A.040" or "Chapter 36.70A RCW" or "RCW 35.22"
    rcw_matches = re.finditer(
        r"\b(?:RCW\s+([0-9A-Z]+(?:\.[0-9A-Z]+)+)|([0-9A-Z]+(?:\.[0-9A-Z]+)+)\s+RCW)\b",
        text,
        re.IGNORECASE,
    )
    for m in rcw_matches:
        cite_num = m.group(1) or m.group(2)
        full_cite = f"RCW {cite_num}"
        # Map GMA specifically to wa-gma, otherwise general legislature
        actor_id = "wa-gma" if cite_num.startswith("36.70A") else "wa-leg"
        citations.append(("statute", full_cite, actor_id, m.start(), m.end()))

    # WAC pattern: e.g. "WAC 365-196" or "WAC 173-26" or "WAC 246-290"
    wac_matches = re.finditer(
        r"\b(?:WAC\s+([0-9A-Z]+(?:\-[0-9A-Z]+)+)|([0-9A-Z]+(?:\-[0-9A-Z]+)+)\s+WAC)\b",
        text,
        re.IGNORECASE,
    )
    for m in wac_matches:
        cite_num = m.group(1) or m.group(2)
        full_cite = f"WAC {cite_num}"
        actor_id = "wa-commerce"
        if cite_num.startswith("173"):
            actor_id = "wa-ecology"
        elif cite_num.startswith("246"):
            actor_id = "wa-doh"
        citations.append(("rule", full_cite, actor_id, m.start(), m.end()))

    return citations


def detect_upstream_influences_for_item(
    conn,
    item_id: str,
    actors: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Scans an item's title and body_text for upstream actors, aliases, and citations."""
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        return []

    jurisdiction = row["jurisdiction"]
    title = row["title"]
    body_text = row["body_text"] or ""
    combined_text = f"{title}\n\n{body_text}"
    item_url = row["url"]

    detected_refs = []
    seen_ref_ids: Set[str] = set()
    seen_actors: Set[str] = set()

    # 1. Search for RCW / WAC statutory citations
    citations = find_citations(combined_text)
    for cite_type, cite_str, actor_id, start_idx, end_idx in citations:
        excerpt = extract_evidence_excerpt(combined_text, start_idx, end_idx)
        mechanism = detect_mechanism(excerpt)

        # Resolve upstream_doc_id
        upstream_doc_id = get_or_create_statute_doc(
            conn,
            citation=cite_str,
            actor_id=actor_id,
            doc_kind=cite_type,
        )

        ref_id = f"ref-{item_id}-{actor_id}-{hashlib.sha256(excerpt.encode('utf-8')).hexdigest()[:8]}"
        if ref_id in seen_ref_ids:
            continue
        seen_ref_ids.add(ref_id)
        seen_actors.add(actor_id)

        detected_refs.append({
            "id": ref_id,
            "item_id": item_id,
            "actor_id": actor_id,
            "upstream_doc_id": upstream_doc_id,
            "mechanism": mechanism,
            "evidence_url": item_url,
            "evidence_ref": excerpt,
            "confidence": "documented",
            "parent_ref_id": None,
        })

    # 2. Search for registered actors and aliases
    for actor in actors:
        actor_id = actor["id"]

        # Prevent self-referencing (e.g. Olympia referencing City of Olympia)
        if jurisdiction == "olympia" and actor_id == "olympia-city-gov":
            continue
        if jurisdiction == "thurston" and actor_id == "thurston-county-gov":
            continue

        # If already cited via a specific statute, skip broad alias matching (e.g. "RCW")
        if actor_id in seen_actors:
            continue

        aliases = []
        if actor.get("aliases"):
            try:
                aliases = json.loads(actor["aliases"]) if isinstance(actor["aliases"], str) else actor["aliases"]
            except Exception:
                aliases = []
        name_variants = [actor["name"]] + aliases

        for variant in name_variants:
            if not variant or len(variant.strip()) < 3:
                continue

            # Word boundary regex
            pattern = r"\b" + re.escape(variant.strip()) + r"\b"
            match = re.search(pattern, combined_text, re.IGNORECASE)
            if match:
                excerpt = extract_evidence_excerpt(combined_text, match.start(), match.end())
                mechanism = detect_mechanism(excerpt)

                ref_id = f"ref-{item_id}-{actor_id}-{hashlib.sha256(excerpt.encode('utf-8')).hexdigest()[:8]}"
                if ref_id in seen_ref_ids:
                    break
                seen_ref_ids.add(ref_id)
                seen_actors.add(actor_id)

                detected_refs.append({
                    "id": ref_id,
                    "item_id": item_id,
                    "actor_id": actor_id,
                    "upstream_doc_id": None,
                    "mechanism": mechanism,
                    "evidence_url": item_url,
                    "evidence_ref": excerpt,
                    "confidence": "documented",
                    "parent_ref_id": None,
                })
                break  # Matched actor, avoid duplicate alias matches for same actor

    # Save to upstream_refs table
    for ref in detected_refs:
        conn.execute(
            """
            INSERT INTO upstream_refs (
                id, item_id, actor_id, upstream_doc_id, mechanism,
                evidence_url, evidence_ref, confidence, parent_ref_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                mechanism = excluded.mechanism,
                evidence_ref = excluded.evidence_ref,
                confidence = excluded.confidence
            """,
            (
                ref["id"],
                ref["item_id"],
                ref["actor_id"],
                ref["upstream_doc_id"],
                ref["mechanism"],
                ref["evidence_url"],
                ref["evidence_ref"],
                ref["confidence"],
                ref["parent_ref_id"],
            ),
        )

    return detected_refs


def run_detect(
    db_path: Optional[Path] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Runs upstream detection across items in the database."""
    conn = get_db_connection(db_path)

    stats = {
        "items_processed": 0,
        "items_with_influences": 0,
        "refs_created": 0,
        "mechanisms_detected": {},
    }

    try:
        actors = conn.execute("SELECT * FROM upstream_actors").fetchall()
        actors_list = [dict(a) for a in actors]

        query = "SELECT id FROM items ORDER BY meeting_date DESC, id DESC"
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = conn.execute(query).fetchall()

        for r in rows:
            refs = detect_upstream_influences_for_item(conn, r["id"], actors_list)
            stats["items_processed"] += 1
            if refs:
                stats["items_with_influences"] += 1
                stats["refs_created"] += len(refs)
                for ref in refs:
                    m = ref["mechanism"]
                    stats["mechanisms_detected"][m] = stats["mechanisms_detected"].get(m, 0) + 1

        conn.commit()
    finally:
        conn.close()

    return stats


def main():
    parser = argparse.ArgumentParser(description="Detect upstream influences and citations in items")
    parser.add_argument("--all", action="store_true", help="Scan all items in the database")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of items to scan")
    parser.add_argument("--item-id", type=str, default=None, help="Scan a specific item ID")

    args = parser.parse_args()

    if not args.all and not args.item_id:
        print("Please specify --all or --item-id <id>")
        return

    conn = get_db_connection()
    try:
        actors = [dict(a) for a in conn.execute("SELECT * FROM upstream_actors").fetchall()]

        if args.item_id:
            refs = detect_upstream_influences_for_item(conn, args.item_id, actors)
            conn.commit()
            print(f"\nUpstream detection results for {args.item_id} (Found: {len(refs)}):")
            for ref in refs:
                print(f"  - Actor: {ref['actor_id']} | Mechanism: {ref['mechanism']} | Confidence: {ref['confidence']}")
                print(f"    Evidence: \"{ref['evidence_ref']}\"\n")
        else:
            print("Running upstream influence detection...")
            stats = run_detect(limit=args.limit)
            print("\nUpstream Detection Summary:")
            print(f"  Items processed:        {stats['items_processed']}")
            print(f"  Items with influences:  {stats['items_with_influences']}")
            print(f"  Total refs created:     {stats['refs_created']}")
            print(f"  Mechanisms detected:    {stats['mechanisms_detected']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
