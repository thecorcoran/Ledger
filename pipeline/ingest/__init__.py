"""Ingest package for Loretta's Ledger."""

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Set
import yaml

from pipeline.db import get_db_connection, init_db, seed_upstream_actors
from pipeline.ingest.olympia import OlympiaIngester
from pipeline.ingest.thurston import ThurstonIngester

logger = logging.getLogger(__name__)


def run_ingest(
    source: Optional[str] = None,
    all_sources: bool = False,
    days_back: int = 30,
    days_ahead: int = 30,
    limit: int = 50,
    db_path: Optional[Path] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Runs data ingestion for specified sources or all active sources."""
    init_db(db_path)
    seed_upstream_actors(db_path)
    conn = get_db_connection(db_path)

    results = {}
    try:
        # Olympia
        if all_sources or source in ("olympia", "olympia-legistar-api"):
            ingester = OlympiaIngester()
            res = ingester.ingest(
                conn=conn,
                days_back=days_back,
                days_ahead=days_ahead,
                limit=limit,
                use_cache=use_cache,
            )
            results["olympia"] = res

        # Thurston County
        if all_sources or source in ("thurston", "thurston-commissioners", "thurston-planning-commission"):
            t_ingester = ThurstonIngester()
            t_res = t_ingester.ingest(
                conn=conn,
                use_cache=use_cache,
            )
            results["thurston"] = t_res
    finally:
        conn.close()

    return results
