"""Unit tests for pipeline run summary formatting and error resilience."""

import unittest
from pipeline.run import format_export_summary


class TestRunSummary(unittest.TestCase):
    def test_format_export_summary_standard(self):
        stats = {
            "matters": 49,
            "briefs": 6,
            "actors": 6,
            "test_cases": 4,
        }
        output = format_export_summary(stats)
        self.assertIn("Site generated in docs/ and site/_site/", output)
        self.assertIn("49 Matters Tracked", output)
        self.assertIn("6 Approved Policy Briefs", output)
        self.assertIn("6 Upstream Actors Indexed", output)
        self.assertIn("4 Test-Case Watch Items", output)
        self.assertNotIn("Action", output)

    def test_format_export_summary_missing_keys_never_raises_keyerror(self):
        # Empty dict should not raise KeyError
        empty_output = format_export_summary({})
        self.assertIn("Site generated in docs/ and site/_site/", empty_output)

        # Partial dicts should not raise KeyError
        partial_stats = {"briefs": 3}
        partial_output = format_export_summary(partial_stats)
        self.assertIn("3 Approved Policy Briefs", partial_output)

    def test_format_export_summary_unexpected_keys(self):
        # Unexpected / newly added exporter keys should be reported dynamically
        dynamic_stats = {"briefs": 2, "special_ordinances": 5}
        output = format_export_summary(dynamic_stats)
        self.assertIn("2 Approved Policy Briefs", output)
        self.assertIn("5 Special Ordinances", output)


if __name__ == "__main__":
    unittest.main()
