"""CLI entrypoint for running Loretta's Ledger data ingestion."""

import argparse
import logging
import sys
from pipeline.ingest import run_ingest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def main():
    parser = argparse.ArgumentParser(
        description="Run ingest pipeline for Loretta's Ledger (Olympia & Thurston County)"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run ingest across all active configured sources",
    )
    parser.add_argument(
        "--source",
        type=str,
        default=None,
        help="Specify single source to ingest (e.g. olympia)",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=30,
        help="Number of days in the past to query (default: 30)",
    )
    parser.add_argument(
        "--days-ahead",
        type=int,
        default=30,
        help="Number of days in the future to query (default: 30)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum number of events to process (default: 50)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass local cache and re-fetch from upstream APIs",
    )

    args = parser.parse_args()

    if not args.all and not args.source:
        print("Please specify either --all or --source <source_id>")
        sys.exit(1)

    print(f"Starting ingestion (days_back={args.days_back}, days_ahead={args.days_ahead}, limit={args.limit})...")
    results = run_ingest(
        source=args.source,
        all_sources=args.all,
        days_back=args.days_back,
        days_ahead=args.days_ahead,
        limit=args.limit,
        use_cache=not args.no_cache,
    )

    print("\nIngest Summary:")
    for src, stats in results.items():
        print(f"  [{src.upper()}] Events: {stats['events_processed']} | Items Created: {stats['items_created']} | Updated: {stats['items_updated']} | Skipped: {stats['items_skipped']}")
    print("\nIngestion completed successfully.")


if __name__ == "__main__":
    main()
