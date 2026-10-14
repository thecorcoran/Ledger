"""Unit tests for Calendar feeds (.ics) and 'How to take part' page in Loretta's Ledger."""

import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pipeline.calendar import (
    extract_calendar_events,
    generate_ics_content,
    build_and_export_calendars,
    load_calendar_config,
)
from pipeline.db import get_db_connection
from pipeline.export import (
    load_contacts_config,
    resolve_contact_entity,
    extract_brief_gap_questions,
    extract_brief_possible_asks,
    build_brief_contacts_html,
    build_calendar_picker_widget_html,
    build_calendar_page_html,
    build_how_to_take_part_html,
    export_site_content,
)

ROOT_DIR = Path(__file__).resolve().parent.parent


class TestCalendarFeeds(unittest.TestCase):
    def setUp(self):
        self.conn = get_db_connection()

    def tearDown(self):
        self.conn.close()

    def test_calendar_uids_stable(self):
        """Stable UIDs format: ledger-{slug}-{event_type}@{domain} and identical across runs."""
        events1 = extract_calendar_events(self.conn, today_str="2026-10-13")
        events2 = extract_calendar_events(self.conn, today_str="2026-10-13")

        self.assertGreater(len(events1), 0)
        self.assertEqual(len(events1), len(events2))

        for e1, e2 in zip(events1, events2):
            self.assertEqual(e1["uid"], e2["uid"])
            self.assertTrue(
                re.match(r"^ledger-[a-z0-9_-]+-(hearing|meeting|vote|comment-deadline)@[a-z0-9\.-]+$", e1["uid"]),
                f"UID format mismatch: {e1['uid']}",
            )

    def test_calendar_timezone_and_disclaimer(self):
        """Calendar includes RFC 5545 America/Los_Angeles VTIMEZONE, Updated date, and official agenda disclaimer."""
        events = extract_calendar_events(self.conn, today_str="2026-10-13")
        ics_text = generate_ics_content(
            "Test Feed", "Test description", events, today_str="2026-10-13", timezone="America/Los_Angeles"
        )

        self.assertIn("X-WR-TIMEZONE:America/Los_Angeles", ics_text)
        self.assertIn("BEGIN:VTIMEZONE", ics_text)
        self.assertIn("TZID:America/Los_Angeles", ics_text)
        self.assertIn("TZNAME:PST", ics_text)
        self.assertIn("TZNAME:PDT", ics_text)
        self.assertIn("Dates change; confirm on the official agenda.", ics_text)
        self.assertIn("Updated 2026-10-13", ics_text)

    def test_calendar_valarm_reminder_on_comment_deadlines(self):
        """Comment deadline events feature a 1-day advance notification (-P1D VALARM)."""
        events = extract_calendar_events(self.conn, today_str="2026-10-13")
        deadline_events = [e for e in events if e["event_type"] == "comment_deadline"]
        self.assertGreater(len(deadline_events), 0, "Expected at least one comment deadline event in test data")

        ics_text = generate_ics_content(
            "Deadlines Feed", "Deadlines description", deadline_events, today_str="2026-10-13"
        )
        self.assertIn("BEGIN:VALARM", ics_text)
        self.assertIn("TRIGGER:-P1D", ics_text)
        self.assertIn("ACTION:DISPLAY", ics_text)
        self.assertIn("END:VALARM", ics_text)

    def test_calendar_cancelled_events_handling(self):
        """Cancelled meetings must be marked with STATUS:CANCELLED and [CANCELLED] prefix."""
        fake_events = [
            {
                "uid": "ledger-cancelled-item-hearing@lorettas-ledger.local",
                "event_type": "hearing",
                "plain_title": "Cancelled Road Variance Hearing",
                "jurisdiction": "thurston",
                "is_test_case": False,
                "dtstart": "20261015T100000",
                "dtend": "20261015T110000",
                "summary": "[CANCELLED] Cancelled Road Variance Hearing",
                "description": "Cancelled meeting.\\n\\nDates change; confirm on the official agenda.",
                "url": "https://thecorcoran.github.io/Ledger/briefs/test.html",
                "cancelled": True,
                "valarm": False,
            }
        ]
        ics_text = generate_ics_content("Cancelled Test", "Test", fake_events, today_str="2026-10-13")
        self.assertIn("STATUS:CANCELLED", ics_text)
        self.assertIn("SUMMARY:[CANCELLED]", ics_text)


class TestHowToTakePartAndBriefContacts(unittest.TestCase):
    def setUp(self):
        self.contacts_data = load_contacts_config()

    def test_contacts_config_loaded(self):
        """Verified contacts must contain all 6 core local bodies with verified dates."""
        contacts = self.contacts_data.get("contacts", {})
        for key in [
            "olympia_council",
            "olympia_planning_commission",
            "olympia_committees",
            "thurston_bocc",
            "thurston_planning_commission",
            "thurston_boh",
        ]:
            self.assertIn(key, contacts, f"Missing body in contacts config: {key}")
            self.assertIsNotNone(contacts[key].get("last_verified"))
            self.assertIsNotNone(contacts[key].get("source_url"))

    def test_brief_two_group_contacts_structure(self):
        """Brief contact section must feature Group A (staff), Group B (officials), Note C (clerk record)."""
        demo_item = {
            "id": "thurston-bocc-2026-10-13-9809f687c7",
            "display_title": "Health Board weighs updates to county drinking water code",
            "title": "BOH Public Hearing: Revisions to Drinking Water Code for Thurston County Article III",
            "meeting_date": "2026-10-13",
            "jurisdiction": "thurston",
            "full_body_label": "Thurston County Board of Health",
            "comment_deadline": "2026-10-13 (2 hrs prior to start)",
        }
        sample_md = """
## For Staff
- Regulatory Baseline Discrepancy: check WAC 246-291 concurrence.

## What Would Make It Better
- Possible Ask (Separate requirements): Ask staff to isolate optional local additions.
"""
        contacts_html = build_brief_contacts_html(
            demo_item, self.contacts_data, md_source=sample_md, today_str="2026-10-13"
        )

        # Group A
        self.assertIn("A. Ask staff a question", contacts_html)
        self.assertIn("Write", contacts_html)
        self.assertIn("Copy address", contacts_html)
        self.assertIn("Copy a starting draft", contacts_html)
        self.assertIn("Questions from this brief to ask staff", contacts_html)
        self.assertIn("Regulatory Baseline Discrepancy", contacts_html)

        # Group B
        self.assertIn("B. Tell officials what you think", contacts_html)
        self.assertIn("Possible asks from this brief", contacts_html)
        self.assertIn("Possible Ask (Separate requirements)", contacts_html)
        self.assertIn("Carolina Mejia", contacts_html)

        # Note C
        self.assertIn("To put your comments in the official record:", contacts_html)
        self.assertIn("2026-10-13 (2 hrs prior to start)", contacts_html)
        self.assertIn("Contacts verified", contacts_html)

    def test_brief_contacts_undocumented_deadline(self):
        """When comment deadline is omitted, must display 'Check the agenda'."""
        item_no_deadline = {
            "id": "test-no-deadline-item",
            "display_title": "Undocumented Item",
            "title": "Discussion item without comment deadline",
            "meeting_date": "2026-10-20",
            "jurisdiction": "olympia",
            "comment_deadline": None,
        }
        contacts_html = build_brief_contacts_html(
            item_no_deadline, self.contacts_data, md_source="", today_str="2026-10-13"
        )
        self.assertIn("Check the agenda", contacts_html)

    def test_site_export_generates_pages_and_footer_links(self):
        """Export must generate how-to-take-part.html, calendar.html, calendar feeds, and keep main nav intact."""
        conn = get_db_connection()
        try:
            stats = export_site_content(conn)
            self.assertGreater(stats.get("calendar_events", 0), 0)
            self.assertGreater(stats.get("calendar_feeds", 0), 0)

            # Check files exist
            how_to_path = ROOT_DIR / "docs" / "how-to-take-part.html"
            cal_path = ROOT_DIR / "docs" / "calendar.html"
            feed_path = ROOT_DIR / "docs" / "calendars" / "thurston.ics"

            self.assertTrue(how_to_path.exists())
            self.assertTrue(cal_path.exists())
            self.assertTrue(feed_path.exists())

            # Check footer on Home page
            index_path = ROOT_DIR / "docs" / "index.html"
            index_html = index_path.read_text(encoding="utf-8")
            self.assertIn('href="how-to-take-part.html"', index_html)
            self.assertIn('href="calendar.html"', index_html)

            # Verify main nav is NOT altered
            self.assertNotIn('<nav class="ledger-nav"><a href="how-to-take-part.html"', index_html)
            self.assertIn('<nav class="ledger-nav">', index_html)

            # Check "How to take part" promo block on Home
            self.assertIn("How to Take Part in Local Decisions", index_html)

            # Check how-to-take-part.html content
            how_to_html = how_to_path.read_text(encoding="utf-8")
            self.assertIn("1. Commenting on an item that is already scheduled", how_to_html)
            self.assertIn("2. Asking for something to be added to an agenda", how_to_html)
            self.assertIn("3. Writing to staff and writing to officials", how_to_html)
            self.assertIn("4. Keeping track of the dates", how_to_html)
            self.assertIn("Olympia City Council", how_to_html)
            self.assertIn("Thurston County Board of Health", how_to_html)
            self.assertIn("webcal://", how_to_html)

        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()

