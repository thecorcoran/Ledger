"""Tests for simple reader interactions on briefs, Q&A, and private document queries."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from pipeline.db import init_db, get_db_connection
from pipeline.analyze import generate_who_this_affects, generate_brief_markdown
from pipeline.export import build_brief_share_html, load_matter_qa_html, export_site_content
from pipeline.view import query_matter_documents


class TestReaderInteractions(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        init_db(self.db_path)
        self.conn = get_db_connection(self.db_path)

        # Ingest Thurston Board of Health item
        self.conn.execute(
            """
            INSERT INTO items (
                id, source_id, jurisdiction, title, url, published_at,
                body_text, meeting_date, comment_deadline, status, hash, display_title
            ) VALUES (
                'thurston-boh-test-1', 'thurston-boh', 'thurston',
                'BOH Public Hearing: Revisions to Drinking Water Code for Thurston County Article III',
                'https://www.thurstoncountywa.gov/media/35264',
                '2026-10-13',
                'Location: Thurston County Atrium Room 110\nTime: 4:15 PM\n[Hearing Notice](https://www.thurstoncountywa.gov/media/35264)',
                '2026-10-13',
                '2026-10-13',
                'active',
                'hash123',
                'Health Board weighs updates to county drinking water code'
            )
            """
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_who_this_affects_water_and_health(self):
        text = generate_who_this_affects("water_and_health", {"id": "test"})
        self.assertIn("Single-home private well", text)
        self.assertIn("WAC 246-291-005(2)", text)
        self.assertIn("Two-party shared well", text)
        self.assertIn("WAC 246-291-025", text)
        self.assertIn("Group B system", text)
        self.assertIn("Farm or irrigation well", text)
        self.assertIn("The documents do not say", text)
        self.assertIn("Hearing Notice p. 1", text)

    def test_brief_markdown_includes_who_this_affects_after_what_changes(self):
        item = dict(self.conn.execute("SELECT * FROM items WHERE id = 'thurston-boh-test-1'").fetchone())
        md = generate_brief_markdown(item, [], "", today_str="2026-10-13")

        self.assertIn("## What Changes", md)
        self.assertIn("## Who This Affects", md)
        idx_changes = md.index("## What Changes")
        idx_affects = md.index("## Who This Affects")
        idx_good = md.index("## What's Good Here")

        self.assertTrue(idx_changes < idx_affects < idx_good)
        # Verify Today display
        self.assertIn("Docket**: Thurston County • Today (2026-10-13)", md)
        # Verify Board of Health contact
        self.assertIn("Thurston County Board of Health (Deciding Body)", md)
        self.assertIn("Melanie Bruno", md)
        self.assertIn("phss_boards@co.thurston.wa.us", md)

    def test_share_feature_one_line(self):
        it = {
            "display_title": "Health Board weighs updates to county drinking water code",
            "stake": "Revisions to Sanitary Code Article III governing Group B private and shared residential drinking water wells.",
        }
        canonical_url = "https://thecorcoran.github.io/Ledger/briefs/brief-test.html"
        top_html, bottom_html = build_brief_share_html(it, canonical_url)

        self.assertIn("Share &middot; Ask a question", top_html)
        self.assertIn("Copy link", top_html)
        self.assertIn("Email", top_html)
        self.assertIn("Ask a question", top_html)
        self.assertIn("mailto:thecorcoran@gmail.com?subject=Question%3A%20Health%20Board%20weighs%20updates%20to%20county%20drinking%20water%20code", top_html)
        self.assertIn("mailto:?subject=Loretta%27s%20Ledger%3A%20Health%20Board%20weighs%20updates%20to%20county%20drinking%20water%20code", top_html)
        self.assertIn(canonical_url.replace("/", "%2F").replace(":", "%3A"), top_html)

    def test_load_matter_qa_html_approved_vs_unapproved(self):
        qa_dir = Path("data/qa")
        qa_dir.mkdir(parents=True, exist_ok=True)
        test_qa_file = qa_dir / "test-matter-unapproved.md"

        try:
            # Unapproved file
            test_qa_file.write_text("---\napproved: false\n---\n### A question?\nAn answer.", encoding="utf-8")
            html_unapproved = load_matter_qa_html("test-matter-unapproved", "test-item", "Test Title")
            self.assertEqual(html_unapproved, "")

            # Approved file
            test_qa_file.write_text("---\napproved: true\n---\n### Are private wells affected?\nNo changes posted.", encoding="utf-8")
            html_approved = load_matter_qa_html("test-matter-unapproved", "test-item", "Test Title")
            self.assertIn("Questions We&#x27;ve Been Asked", html_approved)
            self.assertIn("Are private wells affected?", html_approved)
            self.assertIn("Have a question about this item?", html_approved)
            self.assertIn("mailto:thecorcoran@gmail.com?subject=Question%3A%20Test%20Title", html_approved)
        finally:
            if test_qa_file.exists():
                test_qa_file.unlink()

    def test_query_matter_documents_answers_and_omissions(self):
        # Test private well question on drinking water matter
        res_wells = query_matter_documents("matter-thurston-sanitary-code-article-iii", "Are single family wells required to test?")
        self.assertIn("The documents do not say", res_wells["answer"])
        self.assertIn("Hearing Notice p. 1", res_wells["answer"])

        # Test fee question
        res_fees = query_matter_documents("matter-thurston-sanitary-code-article-iii", "What fee amounts are proposed under 19.5.2?")
        self.assertIn("The documents do not say", res_fees["answer"])
        self.assertIn("Article I, sections 19.5.2", res_fees["answer"])

        # Test hearing schedule
        res_sched = query_matter_documents("matter-thurston-sanitary-code-article-iii", "When is the hearing?")
        self.assertIn("Tuesday, October 13, 2026", res_sched["answer"])
        self.assertIn("Room 110 of the Atrium", res_sched["answer"])
        self.assertIn("Melanie Bruno", res_sched["answer"])


if __name__ == "__main__":
    unittest.main()

