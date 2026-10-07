"""Unit tests for Olympia ingester using offline fixtures."""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from pipeline.db import init_db, get_db_connection
from pipeline.ingest.olympia import OlympiaIngester
from pipeline.normalize import compute_item_hash, clean_text

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class TestOlympiaIngester(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_ledger.db"
        init_db(self.db_path)
        self.conn = get_db_connection(self.db_path)

        with open(FIXTURES_DIR / "olympia_events.json", "r", encoding="utf-8") as f:
            self.events_fixture = json.load(f)

        with open(FIXTURES_DIR / "olympia_event_items.json", "r", encoding="utf-8") as f:
            self.event_items_fixture = json.load(f)

        with open(FIXTURES_DIR / "olympia_matter_attachments.json", "r", encoding="utf-8") as f:
            self.attachments_fixture = json.load(f)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_procedural_item_filtering(self):
        self.assertTrue(OlympiaIngester.is_procedural_item("ROLL CALL"))
        self.assertTrue(OlympiaIngester.is_procedural_item("Approval of Agenda"))
        self.assertTrue(OlympiaIngester.is_procedural_item("Adjournment"))
        self.assertTrue(OlympiaIngester.is_procedural_item("PUBLIC COMMENT"))
        self.assertTrue(OlympiaIngester.is_procedural_item("Pledge of Allegiance"))

        self.assertFalse(OlympiaIngester.is_procedural_item("Approval of a Resolution Authorizing Amendments to Affordable Housing Contract"))
        self.assertFalse(OlympiaIngester.is_procedural_item("Review of the Proposed 2027 Development Fees and Utility Rates"))

    def test_build_item_record(self):
        ingester = OlympiaIngester(cache_dir=Path(self.temp_dir.name))
        event = self.events_fixture[0]
        # Find a substantive event item
        substantive_item = None
        for it in self.event_items_fixture:
            if it.get("EventItemMatterId"):
                substantive_item = it
                break
        self.assertIsNotNone(substantive_item)

        record = ingester.build_item_record(event, substantive_item, attachments=self.attachments_fixture)
        self.assertIsNotNone(record)
        self.assertEqual(record["jurisdiction"], "olympia")
        self.assertEqual(record["source_id"], "olympia-legistar-api")
        self.assertTrue(record["title"])
        self.assertTrue(record["url"])
        self.assertTrue(record["hash"])
        self.assertIn("Supporting Attachments", record["body_text"])

    def test_database_insert_and_deduplication(self):
        ingester = OlympiaIngester(cache_dir=Path(self.temp_dir.name))
        event = self.events_fixture[0]

        # Ingest items manually using the fixtures
        created = 0
        skipped = 0
        for it in self.event_items_fixture:
            record = ingester.build_item_record(event, it)
            if not record:
                continue

            cursor = self.conn.execute("SELECT id, hash FROM items WHERE id = ?", (record["id"],))
            existing = cursor.fetchone()
            if existing:
                skipped += 1
            else:
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

        self.assertGreater(created, 5, "Expected substantive items to be inserted")

        # Verify items stored in db
        row = self.conn.execute("SELECT count(*) as cnt FROM items").fetchone()
        self.assertEqual(row["cnt"], created)

        # Re-running the exact same records should result in 0 new insertions (deduplication)
        second_run_created = 0
        for it in self.event_items_fixture:
            record = ingester.build_item_record(event, it)
            if not record:
                continue
            cursor = self.conn.execute("SELECT id, hash FROM items WHERE id = ?", (record["id"],))
            existing = cursor.fetchone()
            if not existing:
                second_run_created += 1

        self.assertEqual(second_run_created, 0, "Deduplication must prevent duplicate insertions")


if __name__ == "__main__":
    unittest.main()

