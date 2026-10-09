"""Unit tests for Phase 6 draft generation and editorial review CLI."""

from pathlib import Path
import tempfile
import unittest

from pipeline.db import init_db, get_db_connection
from pipeline.analyze import generate_brief_markdown, draft_item
from pipeline.review import approve_draft, reject_draft


class TestAnalyzeAndReview(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_ledger.db"
        init_db(self.db_path)
        self.conn = get_db_connection(self.db_path)

        self.item_id = "test-item-brief"
        self.conn.execute(
            """
            INSERT INTO items (id, source_id, jurisdiction, title, url, published_at, body_text, meeting_date, comment_deadline, status, hash)
            VALUES (?, 'olympia-legistar-api', 'olympia', 'Proposed Development Fee Increases', 'https://example.com/fees', '2026-10-06', 'Details on impact fees', '2026-10-06', '2026-10-06 5pm', 'active', 'hash_fee')
            """,
            (self.item_id,),
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_generate_brief_format(self):
        item = {
            "title": "Review of Development Fees",
            "jurisdiction": "olympia",
            "meeting_date": "2026-10-06",
            "url": "https://example.com",
            "body_text": "Fee proposal text",
            "comment_deadline": "2026-10-06 5pm",
        }
        refs = [
            {
                "actor_name": "U.S. Department of Housing and Urban Development",
                "upstream_type": "federal",
                "mechanism": "funding_strings",
                "evidence_ref": "CDBG Grant Agreement",
                "evidence_url": "https://example.com",
            }
        ]
        brief = generate_brief_markdown(item, refs, "• HUD via funding_strings")

        # Verify the 3 labeled sections are present
        self.assertIn("## For Staff", brief)
        self.assertIn("## For Residents", brief)
        self.assertIn("## Trajectory", brief)

        # Verify only engaged principles are present (e.g. Cost and who pays)
        self.assertIn("Cost and who pays", brief)
        # Verify non-engaged principles are omitted
        self.assertNotIn("Family and household", brief)

        # Verify upstream section
        self.assertIn("U.S. Department of Housing and Urban Development", brief)

        # Verify citizen action details
        self.assertIn("Meeting Date", brief)
        self.assertIn("How to Comment", brief)

    def test_design_rubric_evaluations(self):
        # Test critical areas evaluation
        cao_item = {
            "title": "Presentation - CAO Fish and Wildlife Habitat Conservation Areas (FWHCAs) Draft Code",
            "jurisdiction": "thurston",
            "meeting_date": "2026-09-16",
            "url": "https://example.com/cao",
            "body_text": "CAO update covering buffers and wetland habitat",
            "comment_deadline": "2026-09-16 5pm",
        }
        refs = [{
            "actor_name": "Washington Growth Management Act",
            "upstream_type": "state_law",
            "mechanism": "mandate",
            "evidence_ref": "CAO update",
            "evidence_url": "https://example.com/cao",
        }]
        cao_brief = generate_brief_markdown(cao_item, refs, "")

        # Verify the 3 labeled sections and specific factual narrative
        self.assertIn("## For Staff", cao_brief)
        self.assertIn("## For Residents", cao_brief)
        self.assertIn("## Trajectory", cao_brief)
        self.assertIn("Site Potential Tree Height", cao_brief)

        # Verify only the 3 engaged principles are highlighted
        self.assertIn("**Ownership**:", cao_brief)
        self.assertIn("**Subsidiarity**:", cao_brief)
        self.assertIn("**Small and local vs. large and distant**:", cao_brief)
        # Verify unengaged principles like Family and household or Place are not listed as headers
        self.assertNotIn("**Family and household**:", cao_brief)

        # Verify upstream and questions
        self.assertIn("WDFW & Growth Management Act", cao_brief)
        self.assertIn("Will the county remove prescriptive lawn size limitations", cao_brief)

    def test_draft_item_and_review_workflow(self):
        # 1. Generate brief draft
        res = draft_item(self.conn, self.item_id)
        self.assertEqual(res["created"], 1)

        # 2. Check draft stored with reviewed = 0
        rows = self.conn.execute("SELECT * FROM drafts WHERE item_id = ?", (self.item_id,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "brief")
        self.assertEqual(rows[0]["reviewed"], 0)
        self.assertIsNone(rows[0]["reviewed_at"])

        # 3. Approve draft
        draft_id = rows[0]["id"]
        approve_draft(self.conn, draft_id)

        approved_row = self.conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        self.assertEqual(approved_row["reviewed"], 1)
        self.assertIsNotNone(approved_row["reviewed_at"])

        # 4. Reject draft
        reject_draft(self.conn, draft_id)
        rejected_row = self.conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        self.assertIsNone(rejected_row)

    def test_save_draft_edits(self):
        draft_item(self.conn, self.item_id)
        row = self.conn.execute("SELECT id FROM drafts WHERE item_id = ? LIMIT 1", (self.item_id,)).fetchone()
        draft_id = row["id"]

        # Save an edit
        new_text = "# Edited Brief Title\n\nCustom edited content with resident notes."
        self.conn.execute("UPDATE drafts SET markdown = ? WHERE id = ?", (new_text, draft_id))
        self.conn.commit()

        updated_row = self.conn.execute("SELECT markdown FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        self.assertEqual(updated_row["markdown"], new_text)

    def test_principles_yaml_config(self):
        import yaml
        config_path = Path(__file__).resolve().parent.parent / "config" / "principles.yaml"
        self.assertTrue(config_path.exists())
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        self.assertIn("principles", cfg)
        principles = cfg["principles"]
        self.assertEqual(len(principles), 8)
        for key in ["subsidiarity", "ownership", "small_and_local", "family_and_household", "cost_and_who_pays", "consent_and_process", "reversibility_and_accountability", "place"]:
            self.assertIn(key, principles)
            p = principles[key]
            self.assertIn("name", p)
            self.assertIn("plain_question", p)
            self.assertIn("what_to_look_for", p)
            self.assertIn("verdicts", p)
            self.assertIn("advances", p["verdicts"])
            self.assertIn("mixed", p["verdicts"])
            self.assertIn("cuts_against", p["verdicts"])

    def test_sharpened_verdict_and_strongest_case(self):
        tcd_item = {
            "title": "Public Hearing: Proposed Ordinance to Adjust Thurston Conservation District's Rates",
            "jurisdiction": "thurston",
            "meeting_date": "2026-10-20",
            "url": "https://example.com/tcd",
            "body_text": "Meeting Time: 3:30 PM, Atrium",
            "comment_deadline": "2026-10-20 1:30 PM",
        }
        refs = [{
            "actor_name": "Washington State Conservation Commission",
            "upstream_type": "state_agency",
            "mechanism": "mandate",
            "evidence_ref": "TCD Rate Adjustment",
            "evidence_url": "https://example.com/tcd",
        }]
        brief = generate_brief_markdown(tcd_item, refs, "")
        self.assertIn("Verdict: Cuts against", brief)
        self.assertIn("Verdict: Mixed", brief)
        self.assertIn("The Strongest Case for This", brief)
        self.assertIn("What We'd Want to Know", brief)
        self.assertIn("RCW 89.08", brief)


if __name__ == "__main__":
    unittest.main()

