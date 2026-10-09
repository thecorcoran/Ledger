"""Unified one-click pipeline runner for Loretta's Ledger."""

import argparse
import sys
import time
from pathlib import Path
from typing import Dict, Any

from pipeline.db import init_db, seed_upstream_actors, get_db_connection
from pipeline.ingest import run_ingest
from pipeline.classify import run_classify
from pipeline.upstream.detect import run_detect
from pipeline.upstream.chains import link_hierarchical_refs
from pipeline.upstream.similarity import run_similarity_detection
from pipeline.analyze import run_drafts
from pipeline.export import export_site_content


def format_export_summary(export_stats: Dict[str, Any]) -> str:
    """Formats export stats reporting only what the exporter actually returns without KeyError."""
    lines = ["  OK: Site generated in docs/ and site/_site/"]
    label_map = {
        "matters": "Matters Tracked",
        "briefs": "Approved Policy Briefs",
        "actors": "Upstream Actors Indexed",
        "test_cases": "Test-Case Watch Items",
    }
    for key, value in export_stats.items():
        label = label_map.get(key, key.replace("_", " ").title())
        lines.append(f"      - {value} {label}")
    return "\n".join(lines)


def run_full_pipeline(
    days_back: int = 30,
    days_ahead: int = 30,
    ingest_limit: int = 50,
    draft_limit: int = 10,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Executes the entire Loretta's Ledger pipeline from ingest to publication in sequence."""
    start_time = time.time()
    results = {}

    print("\n" + "=" * 80)
    print(" LORETTA'S LEDGER — FULL AUTOMATED PIPELINE RUN")
    print("=" * 80)

    # 1. Database Init & Seeding
    print("\n[Step 1/6] Initializing Database & Upstream Registry...")
    init_db()
    seeded = seed_upstream_actors()
    print(f"  OK: Database verified. Upstream actors seeded: {seeded}")

    # 2. Ingest
    print("\n[Step 2/6] Ingesting Feeds (Olympia & Thurston County)...")
    try:
        ingest_stats = run_ingest(
            all_sources=True,
            days_back=days_back,
            days_ahead=days_ahead,
            limit=ingest_limit,
            use_cache=use_cache,
        )
        results["ingest"] = ingest_stats
        for src, st in ingest_stats.items():
            print(f"  OK [{src.upper()}]: Created {st['items_created']} new items | Skipped {st['items_skipped']} existing")
    except Exception as e:
        print(f"  WARNING: Ingest encountered an issue: {e}")
        results["ingest"] = {"error": str(e)}

    # 3. Classify
    print("\n[Step 3/6] Classifying Topics, Comment Deadlines & Test Cases...")
    classify_stats = run_classify()
    results["classify"] = classify_stats
    print(f"  OK: Processed {classify_stats['items_processed']} items")
    print(f"      - Assigned {classify_stats['topics_assigned']} topic tags")
    print(f"      - Detected {classify_stats['deadlines_found']} comment deadlines")
    print(f"      - Flagged {classify_stats['test_cases_flagged']} test-case watch items")

    # 4. Upstream Influences & Chains
    print("\n[Step 4/6] Tracking Upstream Influences & Building Lineage Chains...")
    conn = get_db_connection()
    try:
        detect_stats = run_detect()
        results["detect"] = detect_stats
        print(f"  OK: Found {detect_stats['refs_created']} upstream references across {detect_stats['items_with_influences']} items")

        links = link_hierarchical_refs(conn)
        print(f"  OK: Established {links} hierarchical parent-child legal lineage links")

        sim_matches = run_similarity_detection(conn)
        print(f"  OK: Checked model policy similarity ({sim_matches} template matches)")
    finally:
        conn.close()

    # 5. Generate Drafts
    print("\n[Step 5/6] Generating Litmus Test Briefs...")
    conn = get_db_connection()
    try:
        drafts_created = run_drafts(conn, limit=draft_limit)
        results["drafts"] = drafts_created
        print(f"  OK: Generated {drafts_created} new drafts for priority policy items")
    finally:
        conn.close()

    # 6. Export Static Site
    print("\n[Step 6/6] Exporting Reviewed Content to Public Site...")
    conn = get_db_connection()
    try:
        export_stats = export_site_content(conn)
        results["export"] = export_stats
        print(format_export_summary(export_stats))
    finally:
        conn.close()

    elapsed = round(time.time() - start_time, 2)
    print("\n" + "=" * 80)
    print(f" PIPELINE COMPLETE in {elapsed}s")
    print("=" * 80 + "\n")

    return results


def main():
    parser = argparse.ArgumentParser(description="Run complete Loretta's Ledger pipeline in one command")
    parser.add_argument("--days-back", type=int, default=30, help="Days in past to ingest (default: 30)")
    parser.add_argument("--days-ahead", type=int, default=30, help="Days in future to ingest (default: 30)")
    parser.add_argument("--limit", type=int, default=50, help="Max meetings to process (default: 50)")
    parser.add_argument("--drafts", type=int, default=10, help="Max drafts to generate (default: 10)")
    parser.add_argument("--no-cache", action="store_true", help="Bypass local HTTP cache")

    args = parser.parse_args()

    run_full_pipeline(
        days_back=args.days_back,
        days_ahead=args.days_ahead,
        ingest_limit=args.limit,
        draft_limit=args.drafts,
        use_cache=not args.no_cache,
    )


if __name__ == "__main__":
    main()

