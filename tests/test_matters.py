"""Tests for matters and matter_events linking logic."""

import sqlite3
import unittest
from pathlib import Path

from pipeline.db import init_db
from pipeline.matters import extract_identifiers, link_matters, detect_stage_and_next_step


class TestMatterLinking(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON;")
        from pipeline.db import SCHEMA_SQL
        self.conn.executescript(SCHEMA_SQL)

    def tearDown(self):
        self.conn.close()

    def test_extract_identifiers(self):
        item = {
            "title": "Public Hearing - Case: 25-1692, West Bay Marina Replacement Project",
            "body_text": "Matter File: 26-0731 (information)\nOrdinance No. 2026-88",
        }
        idents = extract_identifiers(item)
        self.assertIn("25-1692", idents["case_numbers"])
        self.assertIn("26-0731", idents["matter_files"])
        self.assertIn("2026-88", idents["ordinance_numbers"])

    def test_link_matters_exact_and_timeline(self):
        # Insert two items sharing the same case number
        self.conn.execute(
            """
            INSERT INTO items (id, source_id, jurisdiction, title, url, meeting_date, body_text, hash)
            VALUES ('item-1', 'src-1', 'olympia', 'Hearing Examiner Case: 25-1692 Dock', 'http://example.com/1', '2026-09-28', 'Matter File: 26-0731', 'h1')
            """
        )
        self.conn.execute(
            """
            INSERT INTO items (id, source_id, jurisdiction, title, url, meeting_date, body_text, hash)
            VALUES ('item-2', 'src-1', 'olympia', 'Council Review Case: 25-1692 Dock', 'http://example.com/2', '2026-10-12', 'Matter File: 26-0731', 'h2')
            """
        )
        self.conn.commit()

        stats = link_matters(self.conn)
        self.assertGreater(stats["matters_created"], 0)
        self.assertGreater(stats["events_linked"], 0)

        # Check matters record
        matter = self.conn.execute("SELECT * FROM matters WHERE primary_identifier = '25-1692'").fetchone()
        self.assertIsNotNone(matter)
        self.assertEqual(matter["jurisdiction"], "olympia")

        # Check events timeline
        events = self.conn.execute(
            "SELECT * FROM matter_events WHERE matter_id = ? ORDER BY action_date ASC",
            (matter["id"],),
        ).fetchall()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["action_date"], "2026-09-28")
        self.assertEqual(events[1]["action_date"], "2026-10-12")


if __name__ == "__main__":
    unittest.main()

