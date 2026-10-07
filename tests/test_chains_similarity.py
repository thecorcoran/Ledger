"""Unit tests for Phase 5 influence chains and text similarity detection."""

import json
from pathlib import Path
import tempfile
import unittest

from pipeline.db import init_db, get_db_connection, seed_upstream_actors
from pipeline.upstream.chains import link_hierarchical_refs, get_item_lineage, format_lineage_display
from pipeline.upstream.similarity import (
    seed_model_docs,
    tokenize_ngrams,
    calculate_passage_similarity,
    run_similarity_detection,
    detect_pattern_alerts,
)


class TestChainsAndSimilarity(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_ledger.db"
        init_db(self.db_path)
        seed_upstream_actors(self.db_path)
        self.conn = get_db_connection(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_lineage_chains(self):
        item_id = "test-item-chains"
        self.conn.execute(
            """
            INSERT INTO items (id, source_id, jurisdiction, title, url, published_at, body_text, meeting_date, comment_deadline, status, hash)
            VALUES (?, 'olympia-legistar-api', 'olympia', 'Test Item', 'https://example.com', '2026-10-06', 'Body', '2026-10-06', NULL, 'active', 'hash123')
            """,
            (item_id,),
        )

        # Insert a child agency ref (Dept of Commerce)
        child_ref_id = "ref-child-commerce"
        self.conn.execute(
            """
            INSERT INTO upstream_refs (id, item_id, actor_id, upstream_doc_id, mechanism, evidence_url, evidence_ref, confidence, parent_ref_id)
            VALUES (?, ?, 'wa-commerce', NULL, 'mandate', 'https://example.com', 'Commerce guidelines', 'documented', NULL)
            """,
            (child_ref_id, item_id),
        )
        self.conn.commit()

        # Link hierarchical refs
        links = link_hierarchical_refs(self.conn)
        self.assertGreater(links, 0)

        # Check that parent GMA ref was created and linked
        lineage = get_item_lineage(self.conn, item_id)
        self.assertEqual(len(lineage), 1)

        root = lineage[0]
        self.assertEqual(root["actor_id"], "wa-gma")
        self.assertEqual(len(root["children"]), 1)
        self.assertEqual(root["children"][0]["actor_id"], "wa-commerce")

        display = format_lineage_display(lineage)
        self.assertIn("Washington Growth Management Act", display)
        self.assertIn("Department of Commerce", display)

    def test_passage_similarity(self):
        text1 = "Establishing Chapter 24.30 Wetlands buffer standards and critical areas variance procedures."
        text2 = "Critical areas ordinance Chapter 24.30 Wetlands buffer standards and protective buffers."

        score, passages = calculate_passage_similarity(text1, text2, n=3)
        self.assertGreater(score, 0.05)
        self.assertGreater(len(passages), 0)
        self.assertTrue(any("wetlands buffer standards" in p for p in passages))

        # Unrelated text
        score_zero, _ = calculate_passage_similarity("Completely unrelated text about basketball tickets.", text1)
        self.assertEqual(score_zero, 0.0)

    def test_similarity_detection_db(self):
        item_id = "test-wetlands-item"
        self.conn.execute(
            """
            INSERT INTO items (id, source_id, jurisdiction, title, url, published_at, body_text, meeting_date, comment_deadline, status, hash)
            VALUES (?, 'thurston-planning-commission', 'thurston', 'CAO Chapter 24.30 Wetlands', 'https://example.com/cao', '2026-10-07', 
            'Critical Areas Ordinance Chapter 24.30 Wetlands buffer standards category I category II wetland classification variance', '2026-10-07', NULL, 'active', 'hashwetlands')
            """,
            (item_id,),
        )
        self.conn.commit()

        matches = run_similarity_detection(self.conn, threshold=0.03)
        self.assertGreater(matches, 0)

        # Check similarity_matches table
        rows = self.conn.execute("SELECT * FROM similarity_matches WHERE item_id = ?", (item_id,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["upstream_doc_id"], "doc-model-ecology-wetlands")

        # Check pattern alerts
        alerts = detect_pattern_alerts(self.conn)
        self.assertTrue(any(a["type"] == "model_policy_reuse" for a in alerts))


if __name__ == "__main__":
    unittest.main()

