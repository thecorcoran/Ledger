"""Unit tests for Thurston County ingester using offline fixtures."""

from pathlib import Path
import tempfile
import unittest

from pipeline.db import init_db, get_db_connection
from pipeline.ingest.thurston import ThurstonIngester

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class TestThurstonIngester(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_ledger.db"
        init_db(self.db_path)
        self.conn = get_db_connection(self.db_path)

        with open(FIXTURES_DIR / "thurston_meetings.html", "r", encoding="utf-8") as f:
            self.meetings_html = f.read()

        with open(FIXTURES_DIR / "thurston_planning_commission.html", "r", encoding="utf-8") as f:
            self.pc_html = f.read()

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_date_parsing(self):
        self.assertEqual(ThurstonIngester.parse_date("Tuesday, October 20, 2026"), "2026-10-20")
        self.assertEqual(ThurstonIngester.parse_date("October 7, 2026"), "2026-10-07")
        self.assertEqual(ThurstonIngester.parse_date("2026-09-15"), "2026-09-15")
        self.assertIsNone(ThurstonIngester.parse_date("Invalid Date"))

    def test_meetings_table_parsing(self):
        ingester = ThurstonIngester(cache_dir=Path(self.temp_dir.name))
        items = ingester.parse_meetings_table(self.meetings_html)

        self.assertGreater(len(items), 10, "Expected multiple meetings parsed")

        # Verify no canceled items were included
        for item in items:
            self.assertEqual(item["jurisdiction"], "thurston")
            self.assertEqual(item["source_id"], "thurston-commissioners")
            self.assertFalse(item["title"].lower().startswith("canceled:"))
            self.assertTrue(item["meeting_date"])
            self.assertTrue(item["hash"])

        # Check for specific known items
        titles = [it["title"] for it in items]
        self.assertTrue(
            any("Drinking Water Code" in t for t in titles),
            "Expected Drinking Water Code public hearing to be parsed",
        )
        self.assertTrue(
            any("Conservation District" in t for t in titles),
            "Expected Conservation District rates public hearing to be parsed",
        )

    def test_planning_commission_parsing(self):
        ingester = ThurstonIngester(cache_dir=Path(self.temp_dir.name))
        pc_items = ingester.parse_planning_commission_page(self.pc_html, limit_dates=4)

        self.assertGreater(len(pc_items), 5, "Expected Planning Commission documents")
        for item in pc_items:
            self.assertEqual(item["jurisdiction"], "thurston")
            self.assertEqual(item["source_id"], "thurston-planning-commission")
            self.assertTrue(item["url"].startswith("https://www.thurstoncountywa.gov/media/"))

    def test_database_insert_and_deduplication(self):
        ingester = ThurstonIngester(cache_dir=Path(self.temp_dir.name))

        # First ingestion
        bocc_items = ingester.parse_meetings_table(self.meetings_html)
        created = 0
        for record in bocc_items:
            cursor = self.conn.execute("SELECT id FROM items WHERE id = ?", (record["id"],))
            if not cursor.fetchone():
                self.conn.execute(
                    """
                    INSERT INTO items (
                        id, source_id, jurisdiction, title, url,
                        published_at, body_text, meeting_date, comment_deadline,
                        status, hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["id"],
                        record["source_id"],
                        record["jurisdiction"],
                        record["title"],
                        record["url"],
                        record["published_at"],
                        record["body_text"],
                        record["meeting_date"],
                        record["comment_deadline"],
                        record["status"],
                        record["hash"],
                    ),
                )
                created += 1
        self.conn.commit()

        self.assertGreater(created, 10)

        # Re-running same items should yield 0 new insertions
        second_run_created = 0
        for record in bocc_items:
            cursor = self.conn.execute("SELECT id FROM items WHERE id = ?", (record["id"],))
            if not cursor.fetchone():
                second_run_created += 1

        self.assertEqual(second_run_created, 0, "All items must be deduplicated")


if __name__ == "__main__":
    unittest.main()

