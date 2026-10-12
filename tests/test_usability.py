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
            self.assertTrue(
                any(dt.startswith(b) for b in ("County", "City Council", "Health Board", "Planning Commission", "Planners", "Council")),
                f"Body missing in '{dt}'",
            )

    def test_plain_title_shares_key_nouns_and_fixes_known_errors(self):
        from pipeline.titles import shares_key_nouns, determine_exact_body_name

        # 1. Known error fixes
        quince = {
            "title": "4.G Approval of a Resolution Authorizing Amendments to the Affordable Housing and Homeless Services Contracts with Thurston County for Funding for Operation of the Quince Street Tiny Home Village",
            "jurisdiction": "olympia",
            "body_text": "Body: City Council",
        }
        quince_dt = generate_display_title(quince, quince["body_text"])
        self.assertIn("Quince Street Tiny Home Village", quince_dt)
        self.assertNotIn("road", quince_dt.lower())
        self.assertTrue(shares_key_nouns(quince_dt, quince["title"]))

        timberland = {
            "title": "1.E Consideration of a Resolution Expressing Collective City Council Support for the Timberland Regional Library District Levy Lid Lift Proposition No. 1",
            "jurisdiction": "olympia",
            "body_text": "Body: City Council",
        }
        timberland_dt = generate_display_title(timberland, timberland["body_text"])
        self.assertIn("Timberland", timberland_dt)
        self.assertIn("levy", timberland_dt.lower())
        self.assertNotIn("land use", timberland_dt.lower())
        self.assertTrue(shares_key_nouns(timberland_dt, timberland["title"]))

        farm_tour = {
            "title": "County Commissioner Working Lands Farm Tour",
            "jurisdiction": "thurston",
            "body_text": "",
        }
        farm_dt = generate_display_title(farm_tour)
        self.assertIn("Working Lands Farm Tour", farm_dt)
        self.assertNotIn("land use plan", farm_dt.lower())
        self.assertTrue(shares_key_nouns(farm_dt, farm_tour["title"]))

        # 2. Body accuracy: never say "City Council" for a committee or commission
        committee_item = {
            "title": "6.A Approval of the Recommended 2027 Utility Rates and Impact Fees",
            "jurisdiction": "olympia",
            "body_text": "Body: Finance Committee",
        }
        self.assertEqual(determine_exact_body_name(committee_item, committee_item["body_text"]), "Finance Committee")
        comm_dt = generate_display_title(committee_item, committee_item["body_text"])
        self.assertTrue(comm_dt.startswith("Finance Committee"))
        self.assertNotIn("City Council", comm_dt)

        # 3. Noun-overlap check across all substantive database items
        from pipeline.db import get_db_connection
        from pipeline.titles import classify_item_tier
        conn = get_db_connection()
        try:
            rows = conn.execute("SELECT id, title, jurisdiction, source_id, body_text FROM items").fetchall()
            for r in rows:
                d = dict(r)
                if classify_item_tier(d) < 3:
                    pt = generate_display_title(d, d.get("body_text") or "")
                    self.assertTrue(
                        shares_key_nouns(pt, d["title"]),
                        f"Plain title '{pt}' shares no key nouns with official title '{d['title']}'",
                    )
        finally:
            conn.close()

    def test_tier_classification_and_no_deliberation_or_generic_stake(self):
        from pipeline.titles import classify_item_tier
        from pipeline.export import get_whats_at_stake, determine_plain_status

        # Tier 3 procedural items
        for proc_title in [
            "1.A CALL TO ORDER (5:30-5:35 PM)",
            "3.A Approval of August 17, 2026 Finance Committee Meeting Minutes",
            "4. PUBLIC COMMENT (5:45-6:00 PM)",
            "1.C STAFF PRESENT",
            "PLEASE NOTE: Actual beginning and start times for a specific agenda item may vary.",
        ]:
            self.assertEqual(classify_item_tier({"title": proc_title}), 3, f"Expected Tier 3 for '{proc_title}'")

        # Tier 1 items
        self.assertEqual(classify_item_tier({"title": "Public Hearing: Proposed Ordinance to Adjust Rates"}), 1)
        self.assertEqual(classify_item_tier({"title": "Some Item"}, has_brief=True), 1)
        self.assertEqual(classify_item_tier({"title": "Some Item"}, is_test_case=True), 1)

        # Tier 2 items
        self.assertEqual(classify_item_tier({"title": "4.F Approval of a Lease Agreement for Fire Vehicles Storage"}), 2)

        # Generic stake must never be returned
        unknown_item = {"title": "6.A Routine Departmental Update"}
        self.assertIsNone(get_whats_at_stake(unknown_item))

        # Status must never be "Deliberation"
        st = determine_plain_status(unknown_item, "City Council", has_brief=False, today_str="2026-10-12")
        self.assertNotIn("deliberation", st.lower())
        self.assertIn(st, {"Hearing", "Vote", "Recommendation stage", "Brief ready", "Outcome not yet posted"})

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

