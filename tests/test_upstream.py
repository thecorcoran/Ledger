"""Unit tests for Phase 4 upstream influence detection and citations."""

from pathlib import Path
import tempfile
import unittest

from pipeline.db import init_db, get_db_connection, seed_upstream_actors
from pipeline.upstream.detect import (
    find_citations,
    detect_mechanism,
    detect_upstream_influences_for_item,
    run_detect,
)
from pipeline.upstream.fetch import get_or_create_statute_doc


class TestUpstreamDetection(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_ledger.db"
        init_db(self.db_path)
        seed_upstream_actors(self.db_path)
        self.conn = get_db_connection(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_find_citations(self):
        text = "This ordinance is adopted pursuant to RCW 36.70A.040 and complies with WAC 365-196-410."
        cites = find_citations(text)
        self.assertEqual(len(cites), 2)

        # Check GMA
        gma_cite = [c for c in cites if "36.70A" in c[1]][0]
        self.assertEqual(gma_cite[0], "statute")
        self.assertEqual(gma_cite[1], "RCW 36.70A.040")
        self.assertEqual(gma_cite[2], "wa-gma")

        # Check Commerce WAC
        wac_cite = [c for c in cites if "365-196" in c[1]][0]
        self.assertEqual(wac_cite[0], "rule")
        self.assertEqual(wac_cite[2], "wa-commerce")

    def test_detect_mechanism(self):
        # Funding strings
        self.assertEqual(
            detect_mechanism("Grant Agreement with HUD for Community Development Block Grant Award"),
            "funding_strings",
        )
        # Mandate
        self.assertEqual(
            detect_mechanism("Statutory requirement pursuant to RCW 36.70A mandated by legislature"),
            "mandate",
        )
        # Template
        self.assertEqual(
            detect_mechanism("Adopted using the MRSC model ordinance template"),
            "template",
        )
        # Approval gate
        self.assertEqual(
            detect_mechanism("Comprehensive plan amendment is subject to approval of Ecology"),
            "approval_gate",
        )
        # Unknown
        self.assertEqual(
            detect_mechanism("Regular meeting discussion about trees"),
            "unknown",
        )

    def test_statute_doc_creation(self):
        doc_id = get_or_create_statute_doc(
            self.conn,
            citation="RCW 36.70A.040",
            actor_id="wa-gma",
            doc_kind="statute",
        )
        self.assertTrue(doc_id.startswith("doc-rcw-36-70a-040"))

        row = self.conn.execute("SELECT * FROM upstream_docs WHERE id = ?", (doc_id,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["actor_id"], "wa-gma")
        self.assertIn("app.leg.wa.gov/rcw", row["url"])

    def test_detect_influences_db(self):
        # Insert test item into DB
        item_id = "test-hud-item"
        self.conn.execute(
            """
            INSERT INTO items (
                id, source_id, jurisdiction, title, url,
                published_at, body_text, meeting_date, comment_deadline,
                status, hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_id,
                "olympia-legistar-api",
                "olympia",
                "Approval of a Resolution Authorizing a Grant Agreement with HUD for CDBG Award",
                "https://example.com/hud-item",
                "2026-10-06",
                "Grant conditions require adherence to federal low-income housing criteria pursuant to RCW 35.22.280.",
                "2026-10-06",
                None,
                "active",
                "test_hash_hud",
            ),
        )
        self.conn.commit()

        actors = [dict(a) for a in self.conn.execute("SELECT * FROM upstream_actors").fetchall()]
        refs = detect_upstream_influences_for_item(self.conn, item_id, actors)
        self.conn.commit()

        self.assertGreater(len(refs), 0)

        # Check HUD detected
        hud_refs = [r for r in refs if r["actor_id"] == "us-hud"]
        self.assertEqual(len(hud_refs), 1)
        self.assertEqual(hud_refs[0]["mechanism"], "funding_strings")
        self.assertEqual(hud_refs[0]["confidence"], "documented")
        self.assertIn("Grant Agreement with HUD", hud_refs[0]["evidence_ref"])

        # Check in DB
        db_rows = self.conn.execute("SELECT * FROM upstream_refs WHERE item_id = ?", (item_id,)).fetchall()
        self.assertEqual(len(db_rows), len(refs))


if __name__ == "__main__":
    unittest.main()

