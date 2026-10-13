"""Unit tests for the legislative matter tracker, grouping, minutes parsing, tense/status wording, and Active/Closed/Dormant rules."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from pipeline.db import get_db_connection, init_db
from pipeline.matters import (
    approve_matter_update,
    close_matter,
    compute_archive_state,
    compute_lifecycle_status,
    confirm_proposed_link,
    determine_event_outcome_and_stage,
    determine_matter_Grouping_Key,
    extract_identifiers,
    is_minutes_or_results_item,
    link_matters,
    mark_all_approved_as_published,
    parse_minutes_and_results,
    reject_proposed_link,
    restore_matter_to_active,
)
from pipeline.view import generate_html_dashboard


class TestMatterTracker(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_tracker.db"
        init_db(self.db_path)
        self.conn = get_db_connection(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_extract_identifiers_and_code_chapters(self):
        item1 = {
            "jurisdiction": "olympia",
            "title": "Public Hearing - Case: 25-1692, West Bay Marina Replacement Project",
            "body_text": "Matter File: 26-0731 (information)\nOrdinance No. 2026-88",
        }
        idents1 = extract_identifiers(item1)
        self.assertIn("25-1692", idents1["case_numbers"])
        self.assertIn("26-0731", idents1["matter_files"])
        self.assertIn("2026-88", idents1["ordinance_numbers"])

        item2 = {
            "jurisdiction": "thurston",
            "title": "DRAFT - CAO Chapter 24.30: Wetlands (Changes Since Sept 2, 2026)",
            "body_text": "Planning Commission review of Chapter 24.30.",
        }
        idents2 = extract_identifiers(item2)
        self.assertIn("TCC Chapter 24.30", idents2["code_chapters"])

    def test_matter_grouping_never_matches_generic_words(self):
        """Items with generic words ('Public Hearing', 'Ordinance', 'Agreement') must NOT merge unless they share an identifier or named code chapter."""
        # Two wetlands chapter items (memo + draft code) -> MUST merge into TCC Chapter 24.30
        self.conn.execute(
            """
            INSERT INTO items (id, source_id, jurisdiction, title, url, meeting_date, body_text, hash)
            VALUES
              ('wet-1', 'src', 'thurston', 'Staff Memo - CAO Wetland Gap Analysis', 'https://example.com/w1', '2026-08-05', 'Review of TCC Chapter 24.30 wetlands', 'hw1'),
              ('wet-2', 'src', 'thurston', 'DRAFT - CAO Chapter 24.30: Wetlands', 'https://example.com/w2', '2026-09-02', 'First draft of Chapter 24.30', 'hw2'),
              ('wet-3', 'src', 'thurston', 'DRAFT - CAO Chapter 24.30: Wetlands (Changes Since Sept 2, 2026)', 'https://example.com/w3', '2026-10-07', 'Revised draft of Chapter 24.30', 'hw3'),
              ('gen-1', 'src', 'olympia', 'Public Hearing on Downtown Parking Ordinance', 'https://example.com/g1', '2026-10-06', 'General ordinance text', 'hg1'),
              ('gen-2', 'src', 'olympia', 'Public Hearing on Stormwater Management Ordinance', 'https://example.com/g2', '2026-10-06', 'General ordinance text', 'hg2')
            """
        )
        self.conn.commit()

        link_matters(self.conn, today_str="2026-10-12")

        # All 3 wetland items grouped into a single matter with 3 events and 3 versions
        wet_matter = self.conn.execute("SELECT * FROM matters WHERE stable_id = 'TCC Chapter 24.30'").fetchone()
        self.assertIsNotNone(wet_matter)
        self.assertEqual(wet_matter["matter_type"], "code_amendment")

        wet_events = self.conn.execute(
            "SELECT * FROM matter_events WHERE matter_id = ? AND confidence = 'confirmed' ORDER BY action_date ASC",
            (wet_matter["id"],),
        ).fetchall()
        self.assertEqual(len(wet_events), 3)

        wet_versions = self.conn.execute(
            "SELECT * FROM matter_versions WHERE matter_id = ? ORDER BY version_number ASC",
            (wet_matter["id"],),
        ).fetchall()
        self.assertEqual(len(wet_versions), 3)
        self.assertTrue(all(v["what_changed"] for v in wet_versions))

        # The two generic "Public Hearing ... Ordinance" items in Olympia must remain distinct matters
        oly_matters = self.conn.execute("SELECT * FROM matters WHERE jurisdiction = 'olympia'").fetchall()
        self.assertEqual(len(oly_matters), 2)

        # And no uncertain link is proposed purely on generic words like "Public Hearing" or "Ordinance"
        proposed = self.conn.execute("SELECT * FROM matter_events WHERE confidence = 'proposed'").fetchall()
        self.assertEqual(len(proposed), 0)

    def test_minutes_as_evidence_not_standalone_matters(self):
        """Minutes items must never become standalone matters; instead they populate motions, votes, and source links in matter history."""
        self.conn.execute(
            """
            INSERT INTO items (id, source_id, jurisdiction, title, url, meeting_date, body_text, hash)
            VALUES
              ('case-item', 'src', 'olympia', 'Hearing Examiner Case: 25-1692 Dock Replacement', 'https://example.com/packet', '2026-09-28', 'Matter File: 26-0731', 'hc1'),
              ('min-item', 'src', 'olympia', 'Approval of September 28, 2026 City Council Meeting Minutes', 'https://example.com/minutes-0928', '2026-10-06',
               'Case: 25-1692 Dock Replacement. Moved by Councilmember Smith to approve Case 25-1692. Council approved Case 25-1692; passed unanimously by a vote of 7-0.', 'hm1')
            """
        )
        self.conn.commit()

        # Verify minutes item is recognized as minutes/procedural
        min_row = self.conn.execute("SELECT * FROM items WHERE id = 'min-item'").fetchone()
        self.assertTrue(is_minutes_or_results_item(min_row))
        self.assertIsNone(determine_matter_Grouping_Key(min_row))

        # Parse minutes directly
        parsed = parse_minutes_and_results(min_row["body_text"])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["subject_ref"], "Case 25-1692")
        self.assertIn("Moved by Councilmember Smith", parsed[0]["motion_text"] or "")
        self.assertIn("7-0", parsed[0]["vote_tally"] or "")

        # Run full matter linking
        link_matters(self.conn, today_str="2026-10-12")

        # Verify only 1 matter exists (minutes did NOT create a matter)
        matters = self.conn.execute("SELECT * FROM matters").fetchall()
        self.assertEqual(len(matters), 1)
        self.assertEqual(matters[0]["primary_identifier"], "25-1692")

        # Verify the matter event now has the motion, vote tally, and minutes_url from the minutes item!
        ev = self.conn.execute("SELECT * FROM matter_events WHERE matter_id = ?", (matters[0]["id"],)).fetchone()
        self.assertEqual(ev["minutes_url"], "https://example.com/minutes-0928")
        self.assertIn("Moved by Councilmember Smith", ev["motion_text"] or "")
        self.assertIn("7-0", ev["vote_tally"] or "")
        self.assertNotIn("outcome not yet posted", ev["outcome_text"])

    def test_tense_and_status_wording_never_fabricates_outcomes(self):
        """Scheduled future items must use 'will consider'; past items without documented results must use 'considered ...; outcome not yet posted'."""
        upcoming_item = {
            "id": "up-1",
            "jurisdiction": "thurston",
            "title": "Public Hearing: Proposed Ordinance to Adjust Rates",
            "meeting_date": "2026-10-20",
            "body_text": "Body: Board of County Commissioners",
            "url": "https://example.com/up",
        }
        past_item_no_result = {
            "id": "past-1",
            "jurisdiction": "olympia",
            "title": "Home Based Businesses Code Amendment Overview",
            "meeting_date": "2026-10-08",
            "body_text": "Body: Olympia Land Use and Environment Committee",
            "url": "https://example.com/past",
        }

        # 1. Upcoming item (today = 2026-10-12)
        act1, out1, mot1, vote1 = determine_event_outcome_and_stage(upcoming_item, "ordinance", today_str="2026-10-12")
        self.assertIn("will consider", out1)
        self.assertNotIn("outcome not yet posted", out1)
        self.assertIsNone(vote1)

        status_up = compute_lifecycle_status(
            "ordinance",
            [{"action_date": "2026-10-20", "body_name": "Thurston County Board of County Commissioners", "action_type": act1, "outcome_text": out1}],
            today_str="2026-10-12",
        )
        self.assertIn("will consider on Oct 20, 2026", status_up)
        self.assertNotIn("deliberation", status_up.lower())

        # 2. Past item without documented result (today = 2026-10-12)
        act2, out2, mot2, vote2 = determine_event_outcome_and_stage(past_item_no_result, "code_amendment", today_str="2026-10-12")
        self.assertIn("considered", out2)
        self.assertNotIn("will consider", out2)
        self.assertIn("outcome not yet posted", out2)

        status_past = compute_lifecycle_status(
            "code_amendment",
            [{"action_date": "2026-10-08", "body_name": "Olympia Land Use and Environment Committee", "action_type": act2, "outcome_text": out2}],
            today_str="2026-10-12",
        )
        self.assertIn("considered on Oct 8, 2026; outcome not yet posted", status_past)
        self.assertNotIn("deliberation", status_past.lower())

    def test_active_closed_dormant_and_automatic_wake_rules(self):
        """Tests Active, Closed, Dormant (6-month rule), Restore override, and automatic Dormant -> Active wake with 'Update to review'."""
        # 1. Active matter: recent action, undecided
        state_active, woke = compute_archive_state(
            matter_type="code_amendment",
            lifecycle_status="Advisory commission review — Planning Commission considered on Oct 7, 2026; outcome not yet posted",
            last_action_date="2026-10-07",
            next_action_date=None,
            today_str="2026-10-12",
            dormant_months=6,
        )
        self.assertEqual(state_active, "active")
        self.assertFalse(woke)

        # 2. Closed matter: decided and in effect, or withdrawn
        state_closed, woke = compute_archive_state(
            matter_type="ordinance",
            lifecycle_status="Adopted / In effect (2026-10-06)",
            last_action_date="2026-10-06",
            next_action_date=None,
            today_str="2026-10-12",
            dormant_months=6,
        )
        self.assertEqual(state_closed, "closed")
        self.assertFalse(woke)

        # 3. Dormant matter: no action and no posted next step for >= 6 months
        state_dormant, woke = compute_archive_state(
            matter_type="code_amendment",
            lifecycle_status="Advisory commission review — considered on Mar 1, 2026; outcome not yet posted",
            last_action_date="2026-03-01",
            next_action_date=None,
            today_str="2026-10-12",
            dormant_months=6,
        )
        self.assertEqual(state_dormant, "dormant")
        self.assertFalse(woke)

        # 4. End-to-end DB test: Dormant matter automatically wakes to Active and is marked "Update to review" (needs_review = 1)
        # when a new action, version, or scheduled meeting arrives!
        self.conn.execute(
            """
            INSERT INTO items (id, source_id, jurisdiction, title, url, meeting_date, body_text, hash)
            VALUES ('old-1', 'src', 'olympia', 'Public Hearing - Case: 25-0101 Old Annexation', 'https://example.com/old1', '2026-02-10', 'Case: 25-0101', 'ho1')
            """
        )
        self.conn.commit()

        # First link run on 2026-10-12 -> last action was 2026-02-10 (> 6 months ago, no next step), so it becomes 'dormant'
        link_matters(self.conn, today_str="2026-10-12")
        m_dormant = self.conn.execute("SELECT * FROM matters WHERE primary_identifier = '25-0101'").fetchone()
        self.assertEqual(m_dormant["archive_state"], "dormant")

        # Mark it reviewed so needs_review = 0 while dormant
        approve_matter_update(self.conn, m_dormant["id"])
        m_reviewed = self.conn.execute("SELECT * FROM matters WHERE id = ?", (m_dormant["id"],)).fetchone()
        self.assertEqual(m_reviewed["needs_review"], 0)

        # Now a new scheduled meeting / version is posted for Case 25-0101!
        self.conn.execute(
            """
            INSERT INTO items (id, source_id, jurisdiction, title, url, meeting_date, body_text, hash)
            VALUES ('new-2', 'src', 'olympia', 'Council Vote - Case: 25-0101 Old Annexation Revised Ordinance', 'https://example.com/new2', '2026-10-20', 'Case: 25-0101', 'hn2')
            """
        )
        self.conn.commit()

        stats_wake = link_matters(self.conn, today_str="2026-10-12")
        self.assertEqual(stats_wake["woke_from_dormant"], 1)

        m_woke = self.conn.execute("SELECT * FROM matters WHERE id = ?", (m_dormant["id"],)).fetchone()
        self.assertEqual(m_woke["archive_state"], "active")
        self.assertEqual(m_woke["needs_review"], 1)
        self.assertEqual(m_woke["woke_from_dormant"], 1)
        self.assertIn("Returned from Dormant to Active automatically", m_woke["whats_new_summary"])

        # And its living brief is marked unreviewed ("Update to review")
        mb_woke = self.conn.execute("SELECT * FROM matter_briefs WHERE matter_id = ?", (m_dormant["id"],)).fetchone()
        self.assertEqual(mb_woke["reviewed"], 0)

        # 5. Test manual Close and Restore buttons
        close_matter(self.conn, m_woke["id"])
        m_closed = self.conn.execute("SELECT * FROM matters WHERE id = ?", (m_woke["id"],)).fetchone()
        self.assertEqual(m_closed["archive_state"], "closed")

        restore_matter_to_active(self.conn, m_woke["id"])
        m_restored = self.conn.execute("SELECT * FROM matters WHERE id = ?", (m_woke["id"],)).fetchone()
        self.assertEqual(m_restored["archive_state"], "active")

    def test_dashboard_tabs_and_live_vs_not_pushed_status(self):
        """Tests the 5 dashboard tabs (Review | Links to confirm | Alerts | Published | Archive) and Live vs Approved – not yet pushed status."""
        self.conn.execute(
            """
            INSERT INTO items (id, source_id, jurisdiction, title, url, meeting_date, body_text, hash)
            VALUES ('item-pub', 'src', 'olympia', 'Ordinance No. 9901 Sidewalk Repair Program', 'https://example.com/9901', '2026-10-20', 'Ordinance 9901', 'hp1')
            """
        )
        self.conn.commit()
        link_matters(self.conn, today_str="2026-10-12")

        matter = self.conn.execute("SELECT * FROM matters WHERE primary_identifier = '9901'").fetchone()
        self.assertIsNotNone(matter)

        # Before approval, matter is in Review queue with "Update to review"
        html_before = generate_html_dashboard(db_path=self.db_path)
        for tab_label in ("Review (", "Links to confirm (", "Alerts (", "Published (", "Archive ("):
            self.assertIn(tab_label, html_before)
        self.assertIn("Update to review", html_before)

        # Approve matter update -> moves to Published as "Approved – not yet pushed"
        approve_matter_update(self.conn, matter["id"])
        html_approved = generate_html_dashboard(db_path=self.db_path)
        self.assertIn("Approved – not yet pushed", html_approved)

        # Mark all approved as published -> now shows "Live"
        mark_all_approved_as_published(self.conn)
        html_live = generate_html_dashboard(db_path=self.db_path)
        self.assertIn(">Live<", html_live)


if __name__ == "__main__":
    unittest.main()
