from __future__ import annotations

import copy
import json
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from liquid import Environment

from trmnl_agent_usage import collect_usage_feeds as collector
from trmnl_agent_usage import push_usage_feeds as push


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 6, 12, tzinfo=timezone.utc)


class AccountInventoryTests(unittest.TestCase):
    def test_codex_supported_count_is_authoritative_and_ids_are_not_exported(self) -> None:
        result = collector.codex_reset_inventory({"availableCount": 3, "credits": [
            {"id": "private-not-exported", "status": "available", "expiresAt": "2026-07-10T12:00:00Z"},
            {"status": "redeemed", "expiresAt": "2026-07-09T12:00:00Z"},
            {"status": "available", "expiresAt": "2026-07-01T12:00:00Z"},
        ]}, NOW)
        self.assertEqual(result, {"count": 3, "expires_at": "2026-07-10T12:00:00+00:00"})
        for value in (None, {}, {"availableCount": 0}, {"availableCount": -1}, {"availableCount": True}):
            self.assertIsNone(collector.codex_reset_inventory(value, NOW))
        self.assertEqual(collector.codex_reset_inventory({"availableCount": 2, "credits": 4}, NOW),
                         {"count": 2, "expires_at": None})

    def test_claude_inventory_excludes_expired_spent_and_ineligible_grants(self) -> None:
        valid = {"resets_total": 3, "resets_left": 2, "starts_at": "2026-07-01T00:00:00Z",
                 "ends_at": "2026-07-10T00:00:00Z"}
        grants = [valid, {**valid, "resets_left": 0}, {**valid, "ends_at": "2026-07-05T00:00:00Z"},
                  {**valid, "status": "redeemed"}, {**valid, "starts_at": "2026-07-09T00:00:00Z"}]
        self.assertEqual(collector.claude_reset_inventory({"eligible": True, "grants": grants}, NOW),
                         {"count": 2, "expires_at": "2026-07-10T00:00:00+00:00"})
        self.assertIsNone(collector.claude_reset_inventory({"eligible": False, "grants": [valid]}, NOW))
        self.assertIsNone(collector.claude_reset_inventory({"eligible": True, "grants": 4}, NOW))

    def test_positive_balances_and_currency_units(self) -> None:
        self.assertEqual(collector.account_credit("1250", "USD", 2), {"balance": "12.5", "display": "$12.50"})
        self.assertEqual(collector.account_credit("0.001", "USD")["display"], "<$0.01")
        for value in (None, "0", "-5", "NaN", "Infinity", True, "not a balance"):
            self.assertEqual(collector.account_credit(value, "USD"), {})
        self.assertEqual(collector.account_credit(5, "<private>"), {})

    def test_cloud_credit_is_remaining_balance_with_expiry_not_a_quota_reset(self) -> None:
        cloud = {"remaining_dollars": 25.5, "used_dollars": 74.5, "limit_dollars": 100,
                 "resets_at": "2026-07-10T12:00:00Z", "locked_reason": None}
        data = {"iguana_necktie": cloud, "spend": {"balance": {"amount_minor": 1250, "currency": "USD", "exponent": 2}}}
        result = collector.normalize_claude_oauth_usage(data, NOW)
        self.assertEqual(result["credits"]["display"], "$12.50")
        self.assertEqual(result["credits"]["cloud"]["display"], "$25.50")
        for change in ({"remaining_dollars": 0}, {"resets_at": "2026-07-01T00:00:00Z"},
                       {"resets_at": "unknown"}, {"locked_reason": "expired"}):
            normalized = collector.normalize_claude_oauth_usage({"iguana_necktie": {**cloud, **change}}, NOW)
            self.assertNotIn("cloud", normalized["credits"])

    def test_prepaid_reader_uses_get_and_keeps_profile_private(self) -> None:
        payloads = [
            {"organization": {"uuid": "00000000-0000-0000-0000-000000000001"}, "account": {"email": "private@example.com"}},
            {"amount": 1250, "currency": "USD"},
        ]
        responses = []
        for data in payloads:
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(data).encode()
            responses.append(response)
        with mock.patch.object(collector.urllib.request, "urlopen", side_effect=responses) as request:
            result = collector.read_claude_prepaid_credit({"Authorization": "Bearer dummy"}, [])
        self.assertEqual(result, {"balance": "12.5", "display": "$12.50"})
        self.assertTrue(all(call.args[0].get_method() == "GET" for call in request.call_args_list))
        self.assertNotIn("private", json.dumps(result))

    def test_wallet_failure_is_sanitized_and_does_not_invent_credit(self) -> None:
        notes = []
        with mock.patch.object(collector.urllib.request, "urlopen", side_effect=ValueError("private-not-exported")):
            self.assertEqual(collector.read_claude_prepaid_credit({}, notes), {})
        self.assertNotIn("private-not-exported", " ".join(notes))

    def test_claude_read_requests_reset_metadata_with_native_compatible_identity(self) -> None:
        payload = {"seven_day": {"utilization": 25, "resets_at": "2026-07-10T12:00:00Z"},
                   "cedar_ember": {"eligible": False, "ineligible_reason": "no_grant", "grants": []}}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        user_agent = "claude-cli/2.1.100 (external, cli) TRMNLAgentUsageCollector/1"
        with (
            mock.patch.object(collector, "read_claude_oauth_credentials", return_value={
                "accessToken": "synthetic", "expiresAt": 4102444800000, "subscriptionType": "max"}),
            mock.patch.object(collector.urllib.request, "urlopen", return_value=response) as read,
        ):
            limits, _ = collector.read_claude_oauth_usage(Path("/tmp/synthetic"), [], user_agent=user_agent)
        request = read.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIn("cedar_ember=1", request.full_url)
        self.assertEqual(request.get_header("User-agent"), user_agent)
        self.assertIsNone(limits["resets"])

    def test_live_codex_failure_does_not_reuse_old_jsonl_credit(self) -> None:
        with (
            mock.patch.object(collector, "ccusage", return_value={}),
            mock.patch.object(collector, "latest_codex_rate_limits", return_value=(None, {"has_credits": True, "balance": "50"}, {}, 0)),
            mock.patch.object(collector, "read_codex_app_data", return_value=(None, None)),
        ):
            feed = collector.build_codex("ccusage", "UTC", collector.ZoneInfo("UTC"), Path("/tmp/synthetic"), True, None)
        self.assertEqual(feed["credits"]["balance"], "n/a")
        self.assertIsNone(feed["resets"])

    def test_all_layouts_hide_zero_unknown_extras_and_show_each_positive_type(self) -> None:
        for kind in ("codex", "claude"):
            base = json.loads((ROOT / "examples" / f"{kind}.sample.json").read_text())
            cases = [
                ({"count": 0}, {"has_credits": "yes", "balance": "0"}, (False, False, False)),
                (None, {}, (False, False, False)),
                ({"count": 2, "expires": "07-10 12:00"}, {}, (True, False, False)),
                (None, {"balance": "12.50", "display": "$12.50"}, (False, True, False)),
                (None, {"cloud": {"balance": "25.50", "display": "$25.50"}}, (False, False, True)),
            ]
            for resets, credits, expected in cases:
                for path in (ROOT / "templates").glob("*.liquid"):
                    with self.subTest(kind=kind, template=path.name, expected=expected):
                        feed = {**base, "resets": resets, "credits": credits}
                        html = Environment().from_string(path.read_text()).render(source_1=feed)
                        patterns = (r">Resets(?:\s|<)", r">Credits(?:\s|<)", r">Cloud(?: credits)?(?:\s|<)")
                        actual = tuple(bool(re.search(pattern, html)) for pattern in patterns)
                        self.assertEqual(actual, expected)

    def test_compaction_only_exports_display_inventory(self) -> None:
        feed = {"kind": "claude", "resets": {"count": 2, "expires": "07-10 12:00", "id": "private-not-exported"},
                "credits": {"balance": "12.50", "display": "$12.50", "cloud": {"balance": "25.50", "display": "$25.50", "expires": "07-10 12:00", "token": "private-not-exported"}}}
        compact = push.compact_feed(feed)
        self.assertEqual(compact["resets"], {"count": 2, "expires": "07-10 12:00"})
        self.assertNotIn("private-not-exported", json.dumps(compact))
        for count in (None, 0, "2", True):
            self.assertNotIn("resets", push.compact_feed({"resets": {"count": count}}))
        for kind in ("codex", "claude"):
            sample = json.loads((ROOT / "examples" / f"{kind}.sample.json").read_text())
            sample.update(copy.deepcopy(feed))
            sample["kind"] = kind
            self.assertLessEqual(len(push.webhook_payload(sample)), push.DEFAULT_MAX_BYTES)

    def test_version_footer_preserves_legacy_source_fallback(self) -> None:
        self.assertIn("source", push.compact_feed({"source": "synthetic source"}))
        self.assertNotIn("source", push.compact_feed({"source": "synthetic source", "runtime": "Codex 0.100.0"}))

    def test_sub_cent_currency_display_is_escaped_in_all_layouts(self) -> None:
        feed = json.loads((ROOT / "examples/claude.sample.json").read_text())
        feed.update(resets=None, credits=collector.account_credit("0.001", "EUR"))
        for path in (ROOT / "templates").glob("*.liquid"):
            html = Environment().from_string(path.read_text()).render(source_1=feed)
            self.assertIn("&lt;EUR 0.01", html)
            self.assertNotIn("<EUR 0.01", html)


if __name__ == "__main__":
    unittest.main()
