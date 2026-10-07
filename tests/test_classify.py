"""Unit tests for Phase 3 classification (topics, deadlines, test cases)."""

from pathlib import Path
import tempfile
import unittest

from pipeline.db import init_db, get_db_connection
from pipeline.classify import (
    load_topics_config,
    tag_item_topics,
    extract_comment_deadline,
    detect_test_case,
    classify_item,
    run_classify,
)


class TestClassify(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_ledger.db"
        init_db(self.db_path)
        self.conn = get_db_connection(self.db_path)
        self.topics_config = load_topics_config()

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_topic_tagging(self):
        # Housing
        t1 = tag_item_topics(
            title="Approval of amendments to affordable housing contract for tiny home village",
            body_text="",
            topics_config=self.topics_config,
        )
        self.assertIn("housing", t1)

        # Water & Drinking Water
        t2 = tag_item_topics(
            title="BOH Public Hearing: Revisions to Drinking Water Code Article III",
            body_text="",
            topics_config=self.topics_config,
        )
        self.assertIn("water", t2)

        # Taxes and Fees
        t3 = tag_item_topics(
            title="Review of Proposed 2027 Development Fees, Impact Fees, and Utility Rates",
            body_text="",
            topics_config=self.topics_config,
        )
        self.assertIn("taxes-fees", t3)

        # Land use & Rezone
        t4 = tag_item_topics(
            title="Memo - Grand Mound 193rd Land Use Amendment & Rezone",
            body_text="",
            topics_config=self.topics_config,
        )
        self.assertIn("land-use", t4)

    def test_comment_deadline_extraction(self):
        # Relative Thurston rule
        dl1 = extract_comment_deadline(
            title="BoCC Meeting",
            body_text="Written comments may be submitted up to 2 hours prior to meeting start via email",
            meeting_date="2026-10-20",
        )
        self.assertEqual(dl1, "2026-10-20 (2 hrs prior to start)")

        # Explicit date
        dl2 = extract_comment_deadline(
            title="Public Hearing",
            body_text="Submit comments by: October 18, 2026 at 5:00 PM",
            meeting_date="2026-10-20",
        )
        self.assertEqual(dl2, "2026-10-18")

    def test_detect_test_case(self):
        # Regulatory overhaul / first application
        tc1 = detect_test_case(
            title="BOH Public Hearing: Revisions to Drinking Water Code for Thurston County",
            body_text="",
        )
        self.assertIsNotNone(tc1)
        self.assertIn("regulatory code", tc1[0].lower())

        # Pilot program
        tc2 = detect_test_case(
            title="Resolution Authorizing Pilot Program for Franz Anderson Tiny Home Village",
            body_text="",
        )
        self.assertIsNotNone(tc2)
        self.assertIn("pilot program", tc2[0].lower())

        # Non-test case routine item
        tc3 = detect_test_case(
            title="Approval of September 15 Meeting Minutes",
            body_text="Routine minutes approval",
        )
        self.assertIsNone(tc3)

    def test_classify_item_db(self):
        # Insert test item into DB
        item_id = "test-item-1"
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
                "Review of Proposed 2027 Development Fees and Water Utility Rates",
                "https://example.com/item1",
                "2026-10-06",
                "Discussion of water utility fees and impact fees",
                "2026-10-06",
                None,
                "active",
                "dummyhash123",
            ),
        )
        self.conn.commit()

        res = classify_item(self.conn, item_id, self.topics_config)
        self.conn.commit()

        self.assertIn("taxes-fees", res["topics"])
        self.assertIn("water", res["topics"])

        # Check item_topics table
        rows = self.conn.execute("SELECT topic FROM item_topics WHERE item_id = ?", (item_id,)).fetchall()
        topics_in_db = [r["topic"] for r in rows]
        self.assertIn("taxes-fees", topics_in_db)
        self.assertIn("water", topics_in_db)


if __name__ == "__main__":
    unittest.main()
