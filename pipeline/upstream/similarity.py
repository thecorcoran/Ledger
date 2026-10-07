"""Text similarity detection vs model policies and pattern alert generator."""

import argparse
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from pipeline.db import DEFAULT_DB_PATH, get_db_connection

logger = logging.getLogger(__name__)

# Sample model policies and peer templates seeded for comparison
KNOWN_MODEL_POLICIES = [
    {
        "id": "doc-model-mrsc-adu",
        "actor_id": "mrsc",
        "title": "MRSC Model Accessory Dwelling Unit (ADU) Ordinance",
        "url": "https://mrsc.org/explore-topics/planning/housing/accessory-dwelling-units",
        "doc_kind": "model_policy",
        "body_text": """
        An ordinance relating to accessory dwelling units; allowing attached and detached ADUs in single family
        residential zones; establishing maximum square footage not to exceed 1,000 square feet; eliminating owner
        occupancy requirements; prohibiting separate utility connections where feasible; and establishing streamlined
        administrative design review without requiring a public hearing.
        """,
    },
    {
        "id": "doc-model-commerce-middle-housing",
        "actor_id": "wa-commerce",
        "title": "WA Dept of Commerce Middle Housing Model Code",
        "url": "https://www.commerce.wa.gov/serving-communities/growth-management/middle-housing/",
        "doc_kind": "model_policy",
        "body_text": """
        Model code provisions implementing House Bill 1110 middle housing mandates; authorizing duplexes,
        triplexes, fourplexes, and courtyard apartments in all residential zones predominantly zoned for single
        family residences; prohibiting parking mandates within one-half mile of major transit stops; providing
        categorical exemptions under the State Environmental Policy Act (SEPA).
        """,
    },
    {
        "id": "doc-model-ecology-wetlands",
        "actor_id": "wa-ecology",
        "title": "WA Dept of Ecology Wetland Buffer & Critical Areas Guidance",
        "url": "https://ecology.wa.gov/Water-Shorelines/Wetlands/Regulations/Critical-areas-ordinance",
        "doc_kind": "model_policy",
        "body_text": """
        Critical Areas Ordinance Chapter 24.30 Wetlands buffer standards; establishing category I, category II,
        category III, and category IV wetland classification; requiring standard protective buffer widths; requiring
        wetland mitigation ratios of two to one for standard wetland creation; prohibiting development within
        protective buffers without an approved critical areas variance or reasonable use exception.
        """,
    },
]


def seed_model_docs(conn) -> int:
    """Seeds known model policies into upstream_docs table."""
    count = 0
    for doc in KNOWN_MODEL_POLICIES:
        conn.execute(
            """
            INSERT INTO upstream_docs (id, actor_id, title, url, published_at, body_text, doc_kind)
            VALUES (?, ?, ?, ?, datetime('now'), ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                url = excluded.url,
                body_text = excluded.body_text
            """,
            (
                doc["id"],
                doc["actor_id"],
                doc["title"],
                doc["url"],
                doc["body_text"].strip(),
                doc["doc_kind"],
            ),
        )
        count += 1
    conn.commit()
    return count


def tokenize_ngrams(text: str, n: int = 3) -> Set[str]:
    """Tokenizes normalized text into word n-grams for fast overlap calculation."""
    clean = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
    words = [w for w in clean.split() if len(w) > 2]
    if len(words) < n:
        return set(words)
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def calculate_passage_similarity(
    text1: str, text2: str, n: int = 3
) -> Tuple[float, List[str]]:
    """Calculates n-gram Jaccard overlap and extracts common matching phrase passages."""
    ngrams1 = tokenize_ngrams(text1, n)
    ngrams2 = tokenize_ngrams(text2, n)

    if not ngrams1 or not ngrams2:
        return 0.0, []

    intersection = ngrams1.intersection(ngrams2)
    union = ngrams1.union(ngrams2)

    score = len(intersection) / len(union) if union else 0.0

    # Extract clean sentence or phrase matches
    matched_phrases = sorted(list(intersection))[:5]
    return score, matched_phrases


def run_similarity_detection(conn, threshold: float = 0.03) -> int:
    """Compares local items against model policies and records similarity matches."""
    seed_model_docs(conn)

    model_docs = conn.execute(
        "SELECT id, actor_id, title, body_text FROM upstream_docs WHERE doc_kind IN ('model_policy', 'peer_ordinance')"
    ).fetchall()

    items = conn.execute("SELECT id, title, body_text FROM items").fetchall()
    matches_recorded = 0

    for it in items:
        item_text = f"{it['title']}\n{it['body_text'] or ''}"

        for mdoc in model_docs:
            if not mdoc["body_text"]:
                continue

            score, common_passages = calculate_passage_similarity(item_text, mdoc["body_text"], n=3)

            # Record if score exceeds threshold or substantive key phrases overlap
            if score >= threshold and len(common_passages) >= 2:
                match_id = f"sim-{it['id']}-{mdoc['id']}"
                conn.execute(
                    """
                    INSERT INTO similarity_matches (id, item_id, upstream_doc_id, score, matched_passages, reviewed)
                    VALUES (?, ?, ?, ?, ?, 0)
                    ON CONFLICT(id) DO UPDATE SET
                        score = excluded.score,
                        matched_passages = excluded.matched_passages
                    """,
                    (
                        match_id,
                        it["id"],
                        mdoc["id"],
                        round(score, 4),
                        json.dumps(common_passages),
                    ),
                )
                matches_recorded += 1

    conn.commit()
    return matches_recorded


def detect_pattern_alerts(conn, actor_threshold: int = 2) -> List[Dict[str, Any]]:
    """Finds repeated upstream actors and templates appearing across multiple local items."""
    alerts = []

    # 1. Frequently appearing upstream actors
    actor_counts = conn.execute(
        """
        SELECT a.id, a.name, a.upstream_type, COUNT(DISTINCT r.item_id) as item_count
        FROM upstream_refs r
        JOIN upstream_actors a ON r.actor_id = a.id
        GROUP BY a.id
        HAVING item_count >= ?
        ORDER BY item_count DESC
        """,
        (actor_threshold,),
    ).fetchall()

    for ac in actor_counts:
        alerts.append({
            "type": "repeated_actor",
            "actor_id": ac["id"],
            "title": f"Pattern Alert: {ac['name']} appeared across {ac['item_count']} local items",
            "description": f"The {ac['upstream_type']} '{ac['name']}' has touched {ac['item_count']} policy items in Olympia/Thurston.",
            "count": ac["item_count"],
        })

    # 2. Similarity matches against model policies
    sim_counts = conn.execute(
        """
        SELECT d.id, d.title, COUNT(DISTINCT s.item_id) as item_count
        FROM similarity_matches s
        JOIN upstream_docs d ON s.upstream_doc_id = d.id
        GROUP BY d.id
        HAVING item_count >= 1
        ORDER BY item_count DESC
        """
    ).fetchall()

    for sc in sim_counts:
        alerts.append({
            "type": "model_policy_reuse",
            "doc_id": sc["id"],
            "title": f"Language Match: '{sc['title']}' matched in {sc['item_count']} local items",
            "description": f"Local policy language shares similar phrasing with {sc['title']}.",
            "count": sc["item_count"],
        })

    return alerts


def main():
    parser = argparse.ArgumentParser(description="Text similarity detection and pattern alerts")
    parser.add_argument("--run", action="store_true", help="Run text similarity detection vs model policies")
    parser.add_argument("--alerts", action="store_true", help="Display active pattern alerts")

    args = parser.parse_args()

    conn = get_db_connection()
    try:
        if args.run or not args.alerts:
            count = run_similarity_detection(conn)
            print(f"Similarity detection complete. Matches recorded: {count}")

        if args.alerts or args.run:
            alerts = detect_pattern_alerts(conn)
            print(f"\nPattern Alerts ({len(alerts)} active):")
            for a in alerts:
                print(f"  * [{a['type'].upper()}] {a['title']}")
                print(f"    {a['description']}\n")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

