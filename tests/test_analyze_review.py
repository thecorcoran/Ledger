"""Unit tests for Phase 6 draft generation and editorial review CLI."""

from pathlib import Path
import tempfile
import unittest

from pipeline.db import init_db, get_db_connection
from pipeline.analyze import generate_brief_markdown, generate_action_page_markdown, draft_item
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

        # Verify all 8 litmus questions are present
        self.assertIn("1. **Subsidiarity**:", brief)
        self.assertIn("2. **Ownership**:", brief)
        self.assertIn("3. **Small and local vs. large and distant**:", brief)
        self.assertIn("4. **Family and household**:", brief)
        self.assertIn("5. **Cost and who pays**:", brief)
        self.assertIn("6. **Consent and process**:", brief)
        self.assertIn("7. **Reversibility and accountability**:", brief)
        self.assertIn("8. **Place**:", brief)

        # Verify upstream section
        self.assertIn("Who's Behind This?", brief)
        self.assertIn("U.S. Department of Housing and Urban Development", brief)

        # Verify citizen next step
        self.assertIn("Next Step for Citizens", brief)

        # Verify actionable way forward
        self.assertIn("Recommended Response & A Way Forward", brief)
        self.assertIn("For Citizens & Property Owners:", brief)
        self.assertIn("For Local Elected Officials:", brief)

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

        self.assertIn("1. **Subsidiarity**: [Rating: Cuts against]", cao_brief)
        self.assertIn("2. **Ownership**: [Rating: Cuts against]", cao_brief)
        self.assertIn("3. **Small and local vs. large and distant**: [Rating: Cuts against]", cao_brief)
        self.assertIn("4. **Family and household**: [Rating: Cuts against]", cao_brief)
        self.assertIn("5. **Cost and who pays**: [Rating: Cuts against]", cao_brief)
        self.assertIn("Demand Small-Parcel Exemptions", cao_brief)
        self.assertIn("Adopt Statutory Minimums Only", cao_brief)

    def test_draft_item_and_review_workflow(self):
        # 1. Generate drafts
        res = draft_item(self.conn, self.item_id)
        self.assertEqual(res["created"], 2)

        # 2. Check draft stored with reviewed = 0
        rows = self.conn.execute("SELECT * FROM drafts WHERE item_id = ?", (self.item_id,)).fetchall()
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(r["reviewed"], 0)
            self.assertIsNone(r["reviewed_at"])

        # 3. Approve draft
        draft_id = rows[0]["id"]
        approve_draft(self.conn, draft_id)

        approved_row = self.conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        self.assertEqual(approved_row["reviewed"], 1)
        self.assertIsNotNone(approved_row["reviewed_at"])

        # 4. Reject draft
        other_draft_id = rows[1]["id"]
        reject_draft(self.conn, other_draft_id)

        rejected_row = self.conn.execute("SELECT * FROM drafts WHERE id = ?", (other_draft_id,)).fetchone()
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


if __name__ == "__main__":
    unittest.main()

