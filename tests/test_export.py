"""Unit tests for Phase 7 static site and PDF export."""

from pathlib import Path
import tempfile
import unittest

from pipeline.db import init_db, get_db_connection, seed_upstream_actors
from pipeline.analyze import draft_item
from pipeline.review import approve_draft
from pipeline.export import export_site_content, export_pdf_html, markdown_to_html


class TestExport(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_ledger.db"
        init_db(self.db_path)
        seed_upstream_actors(self.db_path)
        self.conn = get_db_connection(self.db_path)

        self.item_id = "test-export-item"
        self.conn.execute(
            """
            INSERT INTO items (id, source_id, jurisdiction, title, url, published_at, body_text, meeting_date, comment_deadline, status, hash)
            VALUES (?, 'olympia-legistar-api', 'olympia', 'Drinking Water Rate Review', 'https://example.com/item', '2026-10-06', 'Details on rates', '2026-10-06', NULL, 'active', 'thash_exp')
            """,
            (self.item_id,),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_markdown_to_html(self):
        md = "# Heading 1\n\n**Bold Text** with [Link](https://example.com)\n\n- Item 1\n- Item 2"
        res = markdown_to_html(md)
        self.assertIn("<h1>Heading 1</h1>", res)
        self.assertIn("<strong>Bold Text</strong>", res)
        self.assertIn('<a href="https://example.com" target="_blank">Link</a>', res)
        self.assertIn("<li>Item 1</li>", res)

    def test_reviewed_only_rendering(self):
        # Generate drafts (initially reviewed = 0)
        draft_item(self.conn, self.item_id)

        draft_rows = self.conn.execute("SELECT * FROM drafts WHERE item_id = ?", (self.item_id,)).fetchall()
        self.assertEqual(len(draft_rows), 2)

        # Before approval, export should report 0 briefs published
        stats1 = export_site_content(self.conn, out_dirs=[Path(self.temp_dir.name)])
        self.assertEqual(stats1["briefs"], 0)

        # Approve the brief draft
        brief_draft_id = [d["id"] for d in draft_rows if d["kind"] == "brief"][0]
        approve_draft(self.conn, brief_draft_id)

        # After approval, export should publish exactly 1 brief
        stats2 = export_site_content(self.conn, out_dirs=[Path(self.temp_dir.name)])
        self.assertEqual(stats2["briefs"], 1)

    def test_unreviewed_drafts_and_test_cases_never_publish(self):
        # Insert a test case item and draft (reviewed = 0)
        tc_item_id = "tc-unreviewed-item"
        self.conn.execute(
            """
            INSERT INTO items (id, source_id, jurisdiction, title, url, published_at, body_text, meeting_date, comment_deadline, status, hash)
            VALUES (?, 'thurston-county', 'thurston', 'County Water Precedent Hearing', 'https://example.com/tc', '2026-10-10', 'Well regulations', '2026-10-10', NULL, 'active', 'hash_tc')
            """,
            (tc_item_id,),
        )
        self.conn.execute(
            """
            INSERT INTO test_cases (item_id, flagged_reason, status)
            VALUES (?, 'Water right permit precedent', 'watching')
            """,
            (tc_item_id,),
        )
        self.conn.execute(
            """
            INSERT INTO drafts (item_id, kind, markdown, reviewed, reviewed_at)
            VALUES (?, 'brief', '# Unreviewed Brief', 0, NULL)
            """,
            (tc_item_id,),
        )
        self.conn.commit()

        # Run export: should not publish unreviewed brief or test case
        stats = export_site_content(self.conn, out_dirs=[Path(self.temp_dir.name)])
        self.assertEqual(stats["briefs"], 0)
        self.assertEqual(stats["test_cases"], 0)

        # Now approve the draft
        self.conn.execute("UPDATE drafts SET reviewed = 1 WHERE item_id = ?", (tc_item_id,))
        self.conn.commit()

        # Run export again: both should now be published
        stats_approved = export_site_content(self.conn, out_dirs=[Path(self.temp_dir.name)])
        self.assertEqual(stats_approved["briefs"], 1)
        self.assertEqual(stats_approved["test_cases"], 1)

    def test_export_pdf_html(self):
        draft_item(self.conn, self.item_id)
        draft_rows = self.conn.execute("SELECT * FROM drafts WHERE item_id = ?", (self.item_id,)).fetchall()
        brief_draft_id = [d["id"] for d in draft_rows if d["kind"] == "brief"][0]

        out_pdf = Path(self.temp_dir.name) / "test_brief.html"
        generated_path = export_pdf_html(self.conn, brief_draft_id, out_path=out_pdf)
        self.assertTrue(generated_path.exists())

        with open(generated_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("@page", content)
        self.assertIn("Drinking Water Rate Review", content)


    def test_brief_subdirectory_nav_prefix(self):
        draft_item(self.conn, self.item_id)
        draft_rows = self.conn.execute("SELECT * FROM drafts WHERE item_id = ?", (self.item_id,)).fetchall()
        brief_draft_id = [d["id"] for d in draft_rows if d["kind"] == "brief"][0]
        approve_draft(self.conn, brief_draft_id)

        export_site_content(self.conn, out_dirs=[Path(self.temp_dir.name)])
        brief_file = Path(self.temp_dir.name) / "briefs" / f"brief-{self.item_id}.html"
        self.assertTrue(brief_file.exists())

        with open(brief_file, "r", encoding="utf-8") as f:
            html = f.read()
        self.assertIn('<a href="../index.html" class="logo">', html)
        self.assertIn('<a href="../briefs.html" class="active">', html)

    def test_dashboard_handler_routes(self):
        import io
        from pipeline.view import DashboardHandler

        class MockHandler(DashboardHandler):
            def __init__(self, path):
                self.rfile = io.BytesIO()
                self.wfile = io.BytesIO()
                self.requestline = f"GET {path} HTTP/1.1"
                self.request_version = "HTTP/1.1"
                self.command = "GET"
                self.path = path
                self.headers = {}
                self.do_GET()

        for test_path in ["/docs", "/docs/", "/public", "/public/", "/docs/index.html"]:
            h = MockHandler(test_path)
            out = h.wfile.getvalue().decode("utf-8")
            self.assertIn("Home — Loretta's Ledger", out)


if __name__ == "__main__":
    unittest.main()

