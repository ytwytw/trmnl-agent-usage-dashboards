from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path

from liquid import Environment

from trmnl_agent_usage.collect_usage_feeds import normalize_claude_oauth_usage


ROOT = Path(__file__).resolve().parents[1]


class AdaptiveQuotaTests(unittest.TestCase):
    def assert_quota_labels(self, feed: dict, expected: list[str]) -> None:
        for path in (ROOT / "templates").glob("*.liquid"):
            with self.subTest(template=path.name, expected=expected):
                rendered = Environment().from_string(path.read_text()).render(source_1=feed)
                section = re.search(r'<section class="limits[^"]*">(.*?)</section>', rendered, re.S)
                self.assertIsNotNone(section)
                labels = re.findall(r'class="(?:limit-label|label)">(.*?)</div>', section[1])
                self.assertEqual(labels, expected or ["Limits unavailable"])
                self.assertNotIn("n/a", section[1])
                self.assertIn(f'limits--{len(expected)}"', section[0])

    def test_only_weekly_uses_the_complete_quota_row(self) -> None:
        feed = json.loads((ROOT / "examples/codex.sample.json").read_text())
        self.assert_quota_labels(feed, ["Weekly all"])

    def test_zero_percent_five_hour_is_present(self) -> None:
        feed = json.loads((ROOT / "examples/codex.sample.json").read_text())
        feed["limits"]["weekly"] = copy.deepcopy(feed["limits"]["primary"])
        feed["limits"]["primary"].update(label="5h", window="5h", used="0%", used_percent=0)
        self.assert_quota_labels(feed, ["5h", "Weekly all"])

    def test_claude_five_hour_weekly_and_fable(self) -> None:
        feed = json.loads((ROOT / "examples/claude.sample.json").read_text())
        self.assert_quota_labels(feed, ["Session", "Weekly all", "Fable"])
        feed["limits"]["primary"]["used"] = "n/a"
        self.assert_quota_labels(feed, ["Weekly all", "Fable"])
        feed["limits"].pop("scoped")
        self.assert_quota_labels(feed, ["Weekly all"])

    def test_missing_windows_do_not_invent_zero_usage(self) -> None:
        feed = json.loads((ROOT / "examples/claude.sample.json").read_text())
        feed["limits"] = {}
        self.assert_quota_labels(feed, [])

    def test_reported_fable_is_not_displaced_by_another_scope(self) -> None:
        limits = normalize_claude_oauth_usage({"limits": [
            {"kind": "weekly_scoped", "percent": 82, "is_active": True,
             "scope": {"model": {"display_name": "Other model"}}},
            {"kind": "weekly_scoped", "percent": 0, "is_active": True,
             "scope": {"model": {"display_name": "Fable"}}},
        ]})
        self.assertEqual(limits["scoped"]["label"], "Fable")
        self.assertEqual(limits["scoped"]["used_percentage"], 0)


if __name__ == "__main__":
    unittest.main()
