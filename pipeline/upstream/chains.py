"""Influence lineage chains for Loretta's Ledger."""

import argparse
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pipeline.db import DEFAULT_DB_PATH, get_db_connection

logger = logging.getLogger(__name__)

# Known statutory parent relationships in Washington state law
AUTHORIZING_STATUTE_MAP = {
    # WA Dept of Commerce rules (WAC 365) derive from GMA (RCW 36.70A)
    "wa-commerce": ("wa-gma", "RCW 36.70A", "Growth Management Act"),
    # Ecology shoreline/water rules derive from RCW 90.58 (SMA) or RCW 90.48
    "wa-ecology": ("wa-gma", "RCW 36.70A", "Growth Management Act / SMA"),
    # Dept of Health drinking water rules derive from RCW 70A.125 / RCW 43.20
    "wa-doh": ("wa-leg", "RCW 70A.125", "Public Water Systems Penalties and Compliance"),
    # TRPC operates under interlocal agreement and GMA regional planning mandates
    "trpc": ("wa-gma", "RCW 36.70A.210", "County-wide Planning Policies"),
}


def link_hierarchical_refs(conn) -> int:
    """Links existing upstream_refs to parent upstream actors where legal lineage is known.
    Sets parent_ref_id on child refs, creating an unbroken chain (e.g. Local -> Agency -> Statute).
    """
    refs = conn.execute(
        """
        SELECT r.id, r.item_id, r.actor_id, r.parent_ref_id, a.upstream_type
        FROM upstream_refs r
        JOIN upstream_actors a ON r.actor_id = a.id
        """
    ).fetchall()

    links_created = 0
    item_refs: Dict[str, List[Any]] = {}
    for r in refs:
        item_refs.setdefault(r["item_id"], []).append(r)

    for item_id, item_ref_list in item_refs.items():
        actor_map = {r["actor_id"]: r for r in item_ref_list}

        for r in item_ref_list:
            actor_id = r["actor_id"]
            if actor_id in AUTHORIZING_STATUTE_MAP and not r["parent_ref_id"]:
                parent_actor_id, parent_cite, parent_name = AUTHORIZING_STATUTE_MAP[actor_id]

                # Check if the parent ref already exists for this item
                parent_ref = actor_map.get(parent_actor_id)
                parent_ref_id = None

                if parent_ref:
                    parent_ref_id = parent_ref["id"]
                else:
                    # Create the upstream statutory parent ref so the complete lineage is visible
                    new_ref_id = f"ref-{item_id}-{parent_actor_id}-chain"
                    conn.execute(
                        """
                        INSERT INTO upstream_refs (
                            id, item_id, actor_id, upstream_doc_id, mechanism,
                            evidence_url, evidence_ref, confidence, parent_ref_id, created_at
                        ) VALUES (?, ?, ?, NULL, 'mandate', NULL, ?, 'inferred', NULL, datetime('now'))
                        ON CONFLICT(id) DO NOTHING
                        """,
                        (
                            new_ref_id,
                            item_id,
                            parent_actor_id,
                            f"Authorizing state statute ({parent_cite}: {parent_name}) underlying agency administrative action.",
                        ),
                    )
                    parent_ref_id = new_ref_id

                if parent_ref_id and parent_ref_id != r["id"]:
                    conn.execute(
                        "UPDATE upstream_refs SET parent_ref_id = ? WHERE id = ?",
                        (parent_ref_id, r["id"]),
                    )
                    links_created += 1

    conn.commit()
    return links_created


def get_item_lineage(conn, item_id: str) -> List[Dict[str, Any]]:
    """Returns the ordered hierarchical chain of upstream influences for a local policy item."""
    rows = conn.execute(
        """
        SELECT r.id, r.actor_id, r.upstream_doc_id, r.mechanism, r.evidence_ref,
               r.confidence, r.parent_ref_id, a.name as actor_name, a.upstream_type,
               d.title as doc_title, d.url as doc_url
        FROM upstream_refs r
        JOIN upstream_actors a ON r.actor_id = a.id
        LEFT JOIN upstream_docs d ON r.upstream_doc_id = d.id
        WHERE r.item_id = ?
        """,
        (item_id,),
    ).fetchall()

    if not rows:
        return []

    refs_by_id = {r["id"]: dict(r) for r in rows}
    chains = []

    # Find root nodes (refs with no parent_ref_id)
    roots = [r for r in refs_by_id.values() if not r["parent_ref_id"] or r["parent_ref_id"] not in refs_by_id]

    def build_tree(node: Dict[str, Any]) -> Dict[str, Any]:
        children = [r for r in refs_by_id.values() if r.get("parent_ref_id") == node["id"]]
        node_copy = dict(node)
        node_copy["children"] = [build_tree(c) for c in children]
        return node_copy

    for root in roots:
        chains.append(build_tree(root))

    return chains


def format_lineage_display(chains: List[Dict[str, Any]], indent: int = 0) -> str:
    """Formats an upstream lineage tree into a clean text representation."""
    lines = []
    prefix = "  " * indent
    bullet = "└── " if indent > 0 else "• "

    for node in chains:
        doc_str = f" [{node['doc_title']}]" if node.get("doc_title") else ""
        conf_str = f" ({node['confidence']})" if node.get("confidence") == "inferred" else ""
        lines.append(
            f"{prefix}{bullet}{node['actor_name']} ({node['upstream_type']}){doc_str} "
            f"via {node['mechanism']}{conf_str}"
        )
        if node.get("children"):
            lines.append(format_lineage_display(node["children"], indent=indent + 1))

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Build and inspect upstream influence chains")
    parser.add_argument("--link", action="store_true", help="Link hierarchical parent refs across all items")
    parser.add_argument("--item-id", type=str, default=None, help="Inspect lineage chain for a specific item")

    args = parser.parse_args()

    conn = get_db_connection()
    try:
        if args.link or not args.item_id:
            count = link_hierarchical_refs(conn)
            print(f"Hierarchical lineage links established: {count}")

        if args.item_id:
            chains = get_item_lineage(conn, args.item_id)
            print(f"\nLineage Chain for {args.item_id}:")
            if chains:
                print(format_lineage_display(chains))
            else:
                print("  No upstream source identified in the documents reviewed.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
