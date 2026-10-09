"""Unit tests for Usability Pass:
1. Plain display_title (under ~90 chars, [Body] [does what] [to what], Flesch-Kincaid <= 8.0).
2. For Staff section omission when findings are absent vs inclusion when findings exist.
3. Questions and Possible Actions with verified official contacts and near-duplicate action rejection.
4. Contacts directory 90-day verification flag.
"""

from datetime import datetime, timedelta
from pathlib import Path
import unittest
import yaml

from pipeline.titles import generate_display_title, flesch_kincaid_grade_level
from pipeline.analyze import (
    extract_staff_findings,
    check_duplicate_action,
    generate_brief_markdown,
    load_contacts,
)


class TestUsabilityPass(unittest.TestCase):
    def test_display_title_readability_and_format(self):
        sample_items = [
            {"title": "Public Hearing: Proposed Ordinance to Adjust Thurston Conservation District's Rates", "jurisdiction": "thurston"},
            {"title": "BOH Public Hearing: Revisions to Drinking Water Code for Thurston County Article III", "jurisdiction": "thurston"},
            {"title": "DRAFT - CAO Chapter 24.30: Wetlands", "jurisdiction": "thurston"},
            {"title": "4.J Approval of a Resolution Authorizing a Grant Agreement with HUD for CDBG Award", "jurisdiction": "olympia"},
            {"title": "6.B Review of Proposed 2027 Utility Rates and Facility Charges", "jurisdiction": "olympia"},
        ]
        for it in sample_items:
            dt = generate_display_title(it)
            grade = flesch_kincaid_grade_level(dt)
            self.assertLessEqual(grade, 8.0, f"Title '{dt}' exceeded grade 8.0 with {grade}")
            self.assertLessEqual(len(dt), 90, f"Title '{dt}' exceeded 90 chars with {len(dt)}")
            # Must follow [Body] [verb] [object]
            self.assertTrue(any(dt.startswith(b) for b in ("County", "City Council", "Health Board", "Planners", "Council")), f"Body missing in '{dt}'")

    def test_for_staff_omission_when_no_findings(self):
        routine_item = {
            "title": "Approval of Vendor Payment Vouchers",
            "jurisdiction": "olympia",
            "meeting_date": "2026-10-06",
            "url": "https://example.com/vouchers",
            "body_text": "Routine payment vouchers for city operations",
            "comment_deadline": "2026-10-06",
        }
        # No upstream refs and no document gaps
        brief = generate_brief_markdown(routine_item, [], "")
        self.assertNotIn("## For Staff", brief, "For Staff section must be omitted when no findings exist")

    def test_for_staff_present_when_findings_exist(self):
        tcd_item = {
            "title": "Public Hearing: Proposed Ordinance to Adjust Thurston Conservation District's Rates",
            "jurisdiction": "thurston",
            "meeting_date": "2026-10-20",
            "url": "https://example.com/rates",
            "body_text": "Rates hearing",
            "comment_deadline": "2026-10-20",
        }
        brief = generate_brief_markdown(tcd_item, [], "")
        self.assertIn("## For Staff", brief)
        self.assertIn("Notice Gap on Dollar Rates", brief)

    def test_duplicate_action_rejection(self):
        existing = {
            "What is the exact proposed dollar increase per parcel?",
        }
        near_duplicate = "What is the exact proposed dollar increase per parcel and property rate?"
        distinct_action = "Inquire whether the ordinance includes a five year expiration sunset date."
        
        self.assertTrue(check_duplicate_action(near_duplicate, existing))
        self.assertFalse(check_duplicate_action(distinct_action, existing))

    def test_contacts_yaml_structure_and_dates(self):
        contacts_dict = load_contacts()
        contacts = contacts_dict.get("contacts", {})
        self.assertIn("thurston_bocc", contacts)
        self.assertIn("olympia_council", contacts)
        
        for cid, info in contacts.items():
            self.assertTrue(info.get("source_url"), f"Contact {cid} missing source_url")
            verified_date_str = info.get("last_verified")
            self.assertTrue(verified_date_str, f"Contact {cid} missing last_verified")
            verified_date = datetime.strptime(verified_date_str, "%Y-%m-%d")
            # Verify date is not in the distant past (> 90 days from Oct 2026)
            ref_date = datetime(2026, 10, 9)
            age_days = (ref_date - verified_date).days
            self.assertLessEqual(age_days, 90, f"Contact {cid} is older than 90 days ({age_days} days)")


if __name__ == "__main__":
    unittest.main()

