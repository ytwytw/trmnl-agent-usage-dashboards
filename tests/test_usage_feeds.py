#!/usr/bin/env python3
from __future__ import annotations

import http.client
import io
import json
import os
import re
import stat
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pathlib import Path

from trmnl_agent_usage import capture_claude_statusline as capture
from trmnl_agent_usage import collect_usage_feeds as collector
from trmnl_agent_usage import push_usage_feeds as push
from trmnl_agent_usage import serve_usage_feeds as server_mod


DASHBOARD_TEMPLATE_NAMES = (
    "agent-usage-dashboard.liquid",
    "agent-usage-dashboard-bwr.liquid",
    "agent-usage-dashboard-bwr-half-horizontal.liquid",
    "agent-usage-dashboard-bwr-half-vertical.liquid",
    "agent-usage-dashboard-bwr-quadrant.liquid",
)


class CollectorHelperTests(unittest.TestCase):
    def test_short_number(self) -> None:
        self.assertEqual(collector.short_number(999), "999")
        self.assertEqual(collector.short_number(12_345), "12.3K")
        self.assertEqual(collector.short_number(1_234_567), "1.23M")

    def test_daily_bars_are_newest_first(self) -> None:
        today = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
        rows = [
            {"date": "2026-07-13", "totalTokens": 300},
            {"date": "2026-07-12", "totalTokens": 200},
            {"date": "2026-07-11", "totalTokens": 100},
        ]

        bars = collector.daily_bars(rows, today, days=3)

        self.assertEqual([bar["day"] for bar in bars], ["Mon", "Sun", "Sat"])
        self.assertEqual([bar["tokens"] for bar in bars], ["300", "200", "100"])

    def test_env_flag(self) -> None:
        self.assertTrue(collector.env_flag("MISSING_FLAG", default=True))

    def test_pricing_defaults_online_and_supports_explicit_offline_mode(self) -> None:
        with mock.patch.object(collector.sys, "argv", ["collect"]):
            with mock.patch.dict(collector.os.environ, {}, clear=False):
                collector.os.environ.pop("TRMNL_AGENT_USAGE_OFFLINE", None)
                self.assertFalse(collector.parse_args().offline)
        with mock.patch.object(collector.sys, "argv", ["collect", "--offline"]):
            self.assertTrue(collector.parse_args().offline)

    def test_ccusage_calculates_claude_costs_with_current_prices(self) -> None:
        with mock.patch.object(collector, "run_json", return_value={"ok": True}) as run_json:
            self.assertEqual(
                collector.ccusage("ccusage", ["claude", "daily"], "UTC", False, []),
                {"ok": True},
            )

        command = run_json.call_args.args[0]
        self.assertIn("--mode", command)
        self.assertEqual(command[command.index("--mode") + 1], "calculate")
        self.assertIn("--no-offline", command)
        self.assertNotIn("--offline", command)

    def test_ccusage_codex_uses_current_prices_without_claude_mode(self) -> None:
        with mock.patch.object(collector, "run_json", return_value={}) as run_json:
            collector.ccusage("ccusage", ["codex", "daily"], "UTC", False, [])

        command = run_json.call_args.args[0]
        self.assertNotIn("--mode", command)
        self.assertIn("--no-offline", command)

    def test_codex_cached_input_tokens_normalize_as_cache_reads(self) -> None:
        metrics = collector.normalize_metrics(
            {
                "inputTokens": 100,
                "cachedInputTokens": 900,
                "outputTokens": 25,
                "reasoningOutputTokens": 5,
                "totalTokens": 1025,
                "costUSD": 1.25,
            }
        )
        card = collector.usage_card("Today", metrics)
        self.assertEqual(metrics["cacheReadTokens"], 900)
        self.assertEqual(card["cache_read"], "900")
        self.assertEqual(card["cache_hit"], "90%")

    def test_parse_dt_supported_shapes(self) -> None:
        self.assertEqual(collector.parse_dt(1_700_000_000), datetime.fromtimestamp(1_700_000_000, timezone.utc))
        self.assertEqual(collector.parse_dt("2026-07-06T12:00:00Z").tzinfo, timezone.utc)
        self.assertEqual(collector.parse_dt("not-a-date"), None)

    def test_weekly_reset_labels_include_date_context(self) -> None:
        tz = ZoneInfo("UTC")
        now = datetime(2026, 7, 13, 12, 0, tzinfo=tz)

        self.assertEqual(
            collector.compact_reset_label(datetime(2026, 7, 13, 17, 0, tzinfo=tz), tz, "7d", now),
            "Today 17:00",
        )
        self.assertEqual(
            collector.compact_reset_label(datetime(2026, 7, 14, 9, 0, tzinfo=tz), tz, "7d", now),
            "Tomorrow 09:00",
        )
        self.assertEqual(
            collector.compact_reset_label(datetime(2026, 7, 20, 14, 7, tzinfo=tz), tz, "7d", now),
            "Jul 20 14:07",
        )
        self.assertEqual(
            collector.compact_reset_label(datetime(2026, 7, 13, 17, 0, tzinfo=tz), tz, "5h", now),
            "17:00",
        )

    def test_claude_oauth_usage_normalizes_app_limits(self) -> None:
        limits = collector.normalize_claude_oauth_usage(
            {
                "five_hour": {"utilization": 3.0, "resets_at": "2026-07-14T00:20:00Z"},
                "seven_day": {"utilization": 78.0, "resets_at": "2026-07-13T21:00:00Z"},
                "limits": [
                    {
                        "kind": "session",
                        "percent": 3,
                        "resets_at": "2026-07-14T00:20:00Z",
                        "is_active": False,
                    },
                    {
                        "kind": "weekly_all",
                        "percent": 78,
                        "resets_at": "2026-07-13T21:00:00Z",
                        "is_active": False,
                    },
                    {
                        "kind": "weekly_scoped",
                        "percent": 80,
                        "resets_at": "2026-07-13T21:00:00Z",
                        "is_active": True,
                        "scope": {"model": {"display_name": "Fable"}},
                    },
                ],
            }
        )

        self.assertEqual(limits["five_hour"]["used_percentage"], 3)
        self.assertEqual(limits["weekly"]["used_percentage"], 78)
        self.assertEqual(limits["scoped"]["used_percentage"], 80)
        self.assertEqual(limits["scoped"]["label"], "Fable")

    def test_expired_claude_oauth_token_is_not_sent(self) -> None:
        notes: list[str] = []
        with mock.patch.object(
            collector,
            "read_claude_oauth_credentials",
            return_value={"accessToken": "secret", "expiresAt": 1, "subscriptionType": "max"},
        ):
            with mock.patch.object(collector.urllib.request, "urlopen") as urlopen:
                limits, subscription = collector.read_claude_oauth_usage(Path("/tmp/claude"), notes)
        self.assertIsNone(limits)
        self.assertIsNone(subscription)
        urlopen.assert_not_called()
        self.assertIn("run /login", notes[0])

    def test_expired_claude_oauth_token_with_refresh_token_requests_cli_refresh(self) -> None:
        notes: list[str] = []
        with mock.patch.object(
            collector,
            "read_claude_oauth_credentials",
            return_value={
                "accessToken": "secret",
                "refreshToken": "refresh-secret",
                "expiresAt": 1,
                "subscriptionType": "max",
            },
        ):
            with mock.patch.object(collector.urllib.request, "urlopen") as urlopen:
                limits, subscription = collector.read_claude_oauth_usage(Path("/tmp/claude"), notes)
        self.assertIsNone(limits)
        self.assertIsNone(subscription)
        urlopen.assert_not_called()
        self.assertIn("run Claude Code once to refresh", notes[0])
        self.assertEqual(collector.claude_usage_auth(notes, "pending"), "run Claude")

    def _refresh_stamp(self) -> Path:
        """A real cache location; the refresh refuses to run without one."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name) / collector.CLAUDE_OAUTH_REFRESH_STAMP_NAME

    def _refresh_state(self, stamp: Path) -> Path:
        return stamp.parent / collector.CLAUDE_OAUTH_REFRESH_STATE_NAME

    def _oauth_usage_response(self) -> mock.MagicMock:
        payload = json.dumps(
            {
                "five_hour": {"utilization": 2.0, "resets_at": "2026-07-20T05:00:00Z"},
                "seven_day": {"utilization": 67.0, "resets_at": "2026-07-20T21:00:00Z"},
            }
        ).encode("utf-8")
        response = mock.MagicMock()
        response.read.return_value = payload
        response.__enter__.return_value = response
        return response

    def test_expired_claude_oauth_token_is_not_refreshed_unless_enabled(self) -> None:
        notes: list[str] = []
        expired = {
            "accessToken": "secret",
            "refreshToken": "refresh-secret",
            "expiresAt": 1,
            "subscriptionType": "max",
        }
        with mock.patch.object(collector, "read_claude_oauth_credentials", return_value=expired):
            with mock.patch.object(collector.subprocess, "run") as run:
                limits, subscription = collector.read_claude_oauth_usage(
                    Path("/tmp/claude"),
                    notes,
                    claude_bin="/usr/local/bin/claude",
                )
        run.assert_not_called()
        self.assertIsNone(limits)
        self.assertIsNone(subscription)

    def test_expired_claude_oauth_token_refreshes_through_claude_cli(self) -> None:
        notes: list[str] = []
        expired = {
            "accessToken": "stale",
            "refreshToken": "refresh-secret",
            "expiresAt": 1000,
            "subscriptionType": "max",
        }
        fresh = {
            "accessToken": "rotated",
            "refreshToken": "refresh-secret",
            "expiresAt": (time.time() + 8 * 3600) * 1000,
            "subscriptionType": "max",
        }
        with mock.patch.object(
            collector,
            "read_claude_oauth_credentials",
            side_effect=[expired, fresh],
        ):
            with mock.patch.object(
                collector.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as run:
                with mock.patch.object(
                    collector.urllib.request,
                    "urlopen",
                    return_value=self._oauth_usage_response(),
                ) as urlopen:
                    limits, subscription = collector.read_claude_oauth_usage(
                        Path("/tmp/claude"),
                        notes,
                        claude_bin="/usr/local/bin/claude",
                        enable_refresh=True,
                        refresh_stamp=self._refresh_stamp(),
                    )
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], "/usr/local/bin/claude")
        self.assertIn("--print", run.call_args.args[0])
        urlopen.assert_called_once()
        self.assertEqual(urlopen.call_args.args[0].headers["Authorization"], "Bearer rotated")
        self.assertEqual(limits["five_hour"]["used_percentage"], 2)
        self.assertEqual(limits["weekly"]["used_percentage"], 67)
        self.assertEqual(subscription, "max")
        self.assertEqual(notes, [])

    def test_nearly_expired_claude_oauth_token_refreshes_before_it_lapses(self) -> None:
        notes: list[str] = []
        nearly = {
            "accessToken": "stale",
            "refreshToken": "refresh-secret",
            "expiresAt": (time.time() + 60) * 1000,
            "subscriptionType": "max",
        }
        with mock.patch.object(collector, "read_claude_oauth_credentials", return_value=nearly):
            with mock.patch.object(
                collector.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as run:
                with mock.patch.object(
                    collector.urllib.request,
                    "urlopen",
                    return_value=self._oauth_usage_response(),
                ):
                    collector.read_claude_oauth_usage(
                        Path("/tmp/claude"),
                        notes,
                        claude_bin="/usr/local/bin/claude",
                        enable_refresh=True,
                        refresh_skew_minutes=15,
                        refresh_stamp=self._refresh_stamp(),
                    )
        run.assert_called_once()

    def test_healthy_claude_oauth_token_does_not_trigger_a_refresh_ping(self) -> None:
        notes: list[str] = []
        healthy = {
            "accessToken": "current",
            "refreshToken": "refresh-secret",
            "expiresAt": (time.time() + 8 * 3600) * 1000,
            "subscriptionType": "max",
        }
        with mock.patch.object(collector, "read_claude_oauth_credentials", return_value=healthy):
            with mock.patch.object(collector.subprocess, "run") as run:
                with mock.patch.object(
                    collector.urllib.request,
                    "urlopen",
                    return_value=self._oauth_usage_response(),
                ):
                    limits, _ = collector.read_claude_oauth_usage(
                        Path("/tmp/claude"),
                        notes,
                        claude_bin="/usr/local/bin/claude",
                        enable_refresh=True,
                        refresh_stamp=self._refresh_stamp(),
                    )
        run.assert_not_called()
        self.assertEqual(limits["weekly"]["used_percentage"], 67)

    def test_failed_refresh_ping_falls_back_to_the_recovery_note(self) -> None:
        notes: list[str] = []
        expired = {
            "accessToken": "stale",
            "refreshToken": "refresh-secret",
            "expiresAt": 1000,
            "subscriptionType": "max",
        }
        with mock.patch.object(collector, "read_claude_oauth_credentials", return_value=expired):
            with mock.patch.object(
                collector.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 1),
            ):
                with mock.patch.object(collector.urllib.request, "urlopen") as urlopen:
                    limits, subscription = collector.read_claude_oauth_usage(
                        Path("/tmp/claude"),
                        notes,
                        claude_bin="/usr/local/bin/claude",
                        enable_refresh=True,
                        refresh_stamp=self._refresh_stamp(),
                    )
        urlopen.assert_not_called()
        self.assertIsNone(limits)
        self.assertIsNone(subscription)
        self.assertIn("Claude OAuth refresh ping failed", notes)
        self.assertEqual(collector.claude_usage_auth(notes, "pending"), "run Claude")

    def test_refresh_ping_is_rate_limited_by_the_cooldown_stamp(self) -> None:
        expired = {
            "accessToken": "stale",
            "refreshToken": "refresh-secret",
            "expiresAt": 1000,
            "subscriptionType": "max",
        }
        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / collector.CLAUDE_OAUTH_REFRESH_STAMP_NAME
            with mock.patch.object(collector, "read_claude_oauth_credentials", return_value=expired):
                with mock.patch.object(
                    collector.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ) as run:
                    with mock.patch.object(collector.urllib.request, "urlopen") as urlopen:
                        urlopen.side_effect = OSError("offline")
                        for _ in range(3):
                            collector.read_claude_oauth_usage(
                                Path("/tmp/claude"),
                                [],
                                claude_bin="/usr/local/bin/claude",
                                enable_refresh=True,
                                refresh_stamp=stamp,
                                refresh_cooldown_minutes=30,
                            )
            self.assertEqual(run.call_count, 1)
            self.assertTrue(stamp.exists())
            self.assertEqual(
                run.call_args.kwargs["cwd"],
                str(Path(tmp) / collector.CLAUDE_OAUTH_REFRESH_WORKDIR_NAME),
            )

    def test_missing_claude_binary_skips_the_refresh_ping(self) -> None:
        notes: list[str] = []
        with mock.patch.object(collector.shutil, "which", return_value=None):
            self.assertFalse(
                collector.trigger_claude_oauth_refresh(None, None, 30, "claude-haiku-4-5-20251001", notes)
            )
        self.assertIn("Claude OAuth refresh skipped: claude executable not found", notes)

    def test_absent_oauth_login_never_spends_a_refresh_ping(self) -> None:
        # An API-key-only install has no stored OAuth credential to rotate, so a
        # prompt would be a billable request that accomplishes nothing.
        notes: list[str] = []
        with mock.patch.object(collector, "read_claude_oauth_credentials", return_value=None):
            with mock.patch.object(collector.subprocess, "run") as run:
                limits, subscription = collector.read_claude_oauth_usage(
                    Path("/tmp/claude"),
                    notes,
                    claude_bin="/usr/local/bin/claude",
                    enable_refresh=True,
                    refresh_stamp=self._refresh_stamp(),
                )
        run.assert_not_called()
        self.assertIsNone(limits)
        self.assertIsNone(subscription)

    def test_expired_token_without_a_refresh_token_never_spends_a_ping(self) -> None:
        notes: list[str] = []
        expired = {"accessToken": "stale", "expiresAt": 1000, "subscriptionType": "max"}
        with mock.patch.object(collector, "read_claude_oauth_credentials", return_value=expired):
            with mock.patch.object(collector.subprocess, "run") as run:
                collector.read_claude_oauth_usage(
                    Path("/tmp/claude"),
                    notes,
                    claude_bin="/usr/local/bin/claude",
                    enable_refresh=True,
                    refresh_stamp=self._refresh_stamp(),
                )
        run.assert_not_called()
        self.assertIn("run /login", notes[0])

    def test_ping_that_does_not_rotate_the_token_is_reported(self) -> None:
        # A competing API key would let the prompt succeed while the stored
        # credential stays untouched. The access token deliberately DOES change
        # here while the expiry does not, so an implementation that compares
        # access tokens instead of expiry fails this test.
        notes: list[str] = []
        expired = {
            "accessToken": "stale",
            "refreshToken": "refresh-secret",
            "expiresAt": 1000,
            "subscriptionType": "max",
        }
        reissued_but_not_rotated = {**expired, "accessToken": "different-but-same-expiry"}
        with mock.patch.object(
            collector,
            "read_claude_oauth_credentials",
            side_effect=[dict(expired), dict(reissued_but_not_rotated)],
        ):
            with mock.patch.object(
                collector.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ):
                with mock.patch.object(collector.urllib.request, "urlopen") as urlopen:
                    limits, _ = collector.read_claude_oauth_usage(
                        Path("/tmp/claude"),
                        notes,
                        claude_bin="/usr/local/bin/claude",
                        enable_refresh=True,
                        refresh_stamp=self._refresh_stamp(),
                    )
        urlopen.assert_not_called()
        self.assertIsNone(limits)
        self.assertIn("Claude OAuth refresh ping did not rotate the stored token", notes)

    def test_refresh_ping_drops_competing_authentication_from_the_environment(self) -> None:
        # Asserts on what the subprocess actually receives, so an implementation
        # that builds a sanitized environment and then passes os.environ fails.
        notes: list[str] = []
        competing = {
            "ANTHROPIC_API_KEY": "competing",
            "ANTHROPIC_AUTH_TOKEN": "competing",
            "CLAUDE_CODE_OAUTH_TOKEN": "competing",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "CLAUDE_CODE_USE_FOUNDRY": "1",
            "PATH": "/usr/bin",
        }
        with mock.patch.dict(collector.os.environ, competing, clear=False):
            with mock.patch.object(
                collector.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as run:
                stamp = self._refresh_stamp()
                collector.trigger_claude_oauth_refresh(
                    "/usr/local/bin/claude", stamp, 0, "claude-haiku-4-5-20251001", notes,
                    state=self._refresh_state(stamp),
                )
        passed_env = run.call_args.kwargs["env"]
        for name in competing:
            if name == "PATH":
                continue
            self.assertNotIn(name, passed_env, f"{name} reached the refresh prompt")
        self.assertEqual(passed_env["PATH"], "/usr/bin")

    def test_refresh_ping_keeps_variables_supported_deployments_need(self) -> None:
        # An allowlist was tried and rejected: it silently broke corporate
        # proxy, mTLS, custom CA, and relocated-config deployments.
        notes: list[str] = []
        required = {
            "HTTP_PROXY": "http://proxy.invalid:3128",
            "http_proxy": "http://proxy.invalid:3128",
            "CLAUDE_CODE_CLIENT_CERT": "/etc/ssl/client.pem",
            "CLAUDE_CODE_CLIENT_KEY": "/etc/ssl/client.key",
            "CLAUDE_CODE_CERT_STORE": "/etc/ssl/store",
            "CLAUDE_CONFIG_DIR": "/srv/claude-config",
            "NODE_EXTRA_CA_CERTS": "/etc/ssl/corp.pem",
        }
        with mock.patch.dict(collector.os.environ, required, clear=False):
            with mock.patch.object(
                collector.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as run:
                stamp = self._refresh_stamp()
                collector.trigger_claude_oauth_refresh(
                    "/usr/local/bin/claude", stamp, 0, "claude-haiku-4-5-20251001", notes,
                    state=self._refresh_state(stamp),
                )
        passed_env = run.call_args.kwargs["env"]
        for name, value in required.items():
            self.assertEqual(passed_env.get(name), value, f"{name} was stripped from the prompt")

    def test_persistent_401_after_a_successful_rotation_is_bounded(self) -> None:
        # Rotation can succeed while the usage endpoint keeps rejecting the
        # account. Without a record, the cooldown alone would let that paid
        # prompt repeat forever.
        expired = {
            "accessToken": "stale",
            "refreshToken": "refresh-secret",
            "expiresAt": 1000,
            "subscriptionType": "max",
        }
        rotated = {**expired, "accessToken": "fresh", "expiresAt": (time.time() + 8 * 3600) * 1000}
        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / collector.CLAUDE_OAUTH_REFRESH_STAMP_NAME
            state = self._refresh_state(stamp)
            with mock.patch.object(
                collector,
                "read_claude_oauth_credentials",
                side_effect=[dict(expired), dict(rotated)],
            ):
                with mock.patch.object(
                    collector.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ):
                    unauthorized = collector.urllib.error.HTTPError(
                        "https://example.invalid", 401, "Unauthorized", {}, io.BytesIO(b"")
                    )
                    self.addCleanup(unauthorized.close)
                    with mock.patch.object(
                        collector.urllib.request, "urlopen", side_effect=unauthorized
                    ):
                        collector.read_claude_oauth_usage(
                            Path("/tmp/claude"),
                            [],
                            claude_bin="/usr/local/bin/claude",
                            enable_refresh=True,
                            refresh_stamp=stamp,
                        )
            self.assertTrue(state.exists(), "an unproductive paid prompt was left unbounded")

    def test_writability_preflight_does_not_disturb_a_recorded_value(self) -> None:
        # The preflight runs before the lock, so reading and rewriting the real
        # marker could restore a stale value over another collector's write.
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / collector.CLAUDE_OAUTH_REFRESH_STATE_NAME
            collector.write_claude_refresh_non_rotating_expiry(state, 2000.0)
            self.assertTrue(collector.claude_refresh_state_is_writable(state))
            self.assertEqual(collector.read_claude_refresh_non_rotating_expiry(state), 2000.0)
            self.assertFalse(list(Path(tmp).glob("*.probe*")), "left probe files behind")

    def test_non_finite_expiry_is_rejected(self) -> None:
        # NaN would make every later comparison false, including the one
        # deciding whether a rotation happened.
        for value in (float("nan"), float("inf"), float("-inf")):
            self.assertEqual(collector.claude_oauth_expiry_epoch({"expiresAt": value}), 0.0)

    def test_refresh_without_a_cache_directory_is_refused(self) -> None:
        notes: list[str] = []
        with mock.patch.object(collector.subprocess, "run") as run:
            triggered = collector.trigger_claude_oauth_refresh(
                "/usr/local/bin/claude", None, 0, "claude-haiku-4-5-20251001", notes
            )
        self.assertFalse(triggered)
        run.assert_not_called()
        self.assertIn("Claude OAuth refresh skipped: no cache directory for its retry bounds", notes)

    def test_state_target_blocked_by_a_directory_is_detected_before_spending(self) -> None:
        notes: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / collector.CLAUDE_OAUTH_REFRESH_STAMP_NAME
            state = self._refresh_state(stamp)
            state.mkdir()  # a probe beside the target would not notice this
            with mock.patch.object(collector.subprocess, "run") as run:
                triggered = collector.trigger_claude_oauth_refresh(
                    "/usr/local/bin/claude", stamp, 0, "claude-haiku-4-5-20251001", notes,
                    state=state,
                )
        self.assertFalse(triggered)
        run.assert_not_called()
        self.assertIn("Claude OAuth refresh skipped: refresh state unwritable", notes)

    def test_refresh_ping_is_isolated_but_can_still_read_the_stored_credential(self) -> None:
        notes: list[str] = []
        with mock.patch.object(
            collector.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as run:
            stamp = self._refresh_stamp()
            collector.trigger_claude_oauth_refresh(
                "/usr/local/bin/claude", stamp, 0, "claude-haiku-4-5-20251001", notes,
                state=self._refresh_state(stamp),
            )
        argv = run.call_args.args[0]
        self.assertIn("--safe-mode", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--tools", argv)
        # `--bare` never reads OAuth or the keychain, so it would make the
        # prompt unable to rotate the credential it exists to rotate.
        self.assertNotIn("--bare", argv)

    def test_confirmed_non_rotation_stops_further_paid_pings(self) -> None:
        expired = {
            "accessToken": "stale",
            "refreshToken": "refresh-secret",
            "expiresAt": 1000,
            "subscriptionType": "max",
        }
        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / collector.CLAUDE_OAUTH_REFRESH_STAMP_NAME
            with mock.patch.object(
                collector,
                "read_claude_oauth_credentials",
                side_effect=lambda _r, _n: dict(expired),
            ):
                with mock.patch.object(
                    collector.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ) as run:
                    for _ in range(3):
                        notes: list[str] = []
                        # Age the cooldown out each round; only the recorded
                        # non-rotation may prevent a second prompt.
                        if stamp.exists():
                            os.utime(stamp, (0, 0))
                        collector.read_claude_oauth_usage(
                            Path("/tmp/claude"),
                            notes,
                            claude_bin="/usr/local/bin/claude",
                            enable_refresh=True,
                            refresh_stamp=stamp,
                        )
            self.assertEqual(run.call_count, 1)
            self.assertIn(collector.CLAUDE_OAUTH_REFRESH_NO_ROTATION_NOTE, notes)

    def test_pre_expiry_ping_that_does_not_rotate_is_not_suppressed(self) -> None:
        # Claude Code declines to rotate a still-valid token, which is normal.
        # Recording that as a stuck credential would suppress the real refresh
        # once the token actually expires.
        still_valid = {
            "accessToken": "current",
            "refreshToken": "refresh-secret",
            "expiresAt": (time.time() + 600) * 1000,
            "subscriptionType": "max",
        }
        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / collector.CLAUDE_OAUTH_REFRESH_STAMP_NAME
            state = Path(tmp) / collector.CLAUDE_OAUTH_REFRESH_STATE_NAME
            notes: list[str] = []
            with mock.patch.object(
                collector,
                "read_claude_oauth_credentials",
                side_effect=lambda _r, _n: dict(still_valid),
            ):
                with mock.patch.object(
                    collector.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ):
                    with mock.patch.object(
                        collector.urllib.request,
                        "urlopen",
                        return_value=self._oauth_usage_response(),
                    ):
                        collector.read_claude_oauth_usage(
                            Path("/tmp/claude"),
                            notes,
                            claude_bin="/usr/local/bin/claude",
                            enable_refresh=True,
                            refresh_stamp=stamp,
                            refresh_skew_minutes=60,
                        )
            self.assertFalse(state.exists(), "a still-valid token was recorded as stuck")
            self.assertNotIn(collector.CLAUDE_OAUTH_REFRESH_NO_ROTATION_NOTE, notes)

    def test_refresh_waits_for_real_expiry_by_default(self) -> None:
        # A pre-expiry prompt cannot rotate anything, so the default must not
        # spend one.
        self.assertEqual(collector.DEFAULT_CLAUDE_OAUTH_REFRESH_SKEW_MINUTES, 0)
        nearly = {
            "accessToken": "current",
            "refreshToken": "refresh-secret",
            "expiresAt": (time.time() + 300) * 1000,
            "subscriptionType": "max",
        }
        self.assertFalse(
            collector.claude_oauth_refresh_is_useful(
                nearly,
                collector.claude_oauth_expiry_epoch(nearly),
                collector.DEFAULT_CLAUDE_OAUTH_REFRESH_SKEW_MINUTES,
            )
        )

    def test_authoritative_401_triggers_a_refresh_despite_a_valid_local_expiry(self) -> None:
        # A skewed clock or a server-side revocation makes the local expiry
        # field wrong; the service's 401 is the authoritative signal.
        valid_locally = {
            "accessToken": "rejected",
            "refreshToken": "refresh-secret",
            "expiresAt": (time.time() + 8 * 3600) * 1000,
            "subscriptionType": "max",
        }
        rotated = {**valid_locally, "accessToken": "fresh", "expiresAt": (time.time() + 9 * 3600) * 1000}
        # A real file object keeps HTTPError cleanup quiet across Python versions.
        unauthorized = collector.urllib.error.HTTPError(
            "https://example.invalid", 401, "Unauthorized", {}, io.BytesIO(b"")
        )
        self.addCleanup(unauthorized.close)
        with mock.patch.object(
            collector,
            "read_claude_oauth_credentials",
            side_effect=[dict(valid_locally), dict(rotated)],
        ):
            with mock.patch.object(
                collector.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as run:
                with mock.patch.object(
                    collector.urllib.request,
                    "urlopen",
                    side_effect=[unauthorized, self._oauth_usage_response()],
                ):
                    limits, _ = collector.read_claude_oauth_usage(
                        Path("/tmp/claude"),
                        [],
                        claude_bin="/usr/local/bin/claude",
                        enable_refresh=True,
                        refresh_stamp=self._refresh_stamp(),
                    )
        run.assert_called_once()
        self.assertIsNotNone(limits, "the retry after rotation should have succeeded")
        self.assertEqual(limits["weekly"]["used_percentage"], 67)

    def test_unwritable_refresh_state_blocks_the_ping(self) -> None:
        notes: list[str] = []
        with mock.patch.object(collector, "claude_refresh_state_is_writable", return_value=False):
            with mock.patch.object(collector.subprocess, "run") as run:
                stamp = self._refresh_stamp()
                triggered = collector.trigger_claude_oauth_refresh(
                    "/usr/local/bin/claude", stamp, 0, "claude-haiku-4-5-20251001", notes,
                    state=self._refresh_state(stamp),
                )
        self.assertFalse(triggered)
        run.assert_not_called()
        self.assertIn("Claude OAuth refresh skipped: refresh state unwritable", notes)

    def test_a_second_collector_cannot_spend_a_concurrent_ping(self) -> None:
        notes: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / collector.CLAUDE_OAUTH_REFRESH_STAMP_NAME
            with collector.claude_refresh_claim(stamp) as held:
                self.assertTrue(held)
                with mock.patch.object(collector.subprocess, "run") as run:
                    triggered = collector.trigger_claude_oauth_refresh(
                        "/usr/local/bin/claude", stamp, 0, "claude-haiku-4-5-20251001", notes,
                        state=Path(tmp) / collector.CLAUDE_OAUTH_REFRESH_STATE_NAME,
                    )
                self.assertFalse(triggered)
                run.assert_not_called()

    def test_future_dated_cooldown_marker_does_not_block_forever(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / collector.CLAUDE_OAUTH_REFRESH_STAMP_NAME
            stamp.touch()
            os.utime(stamp, (time.time() + 86400, time.time() + 86400))
            self.assertFalse(collector.claude_oauth_refresh_cooldown_active(stamp, 30))

    def test_corrupt_refresh_state_is_ignored_rather_than_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / collector.CLAUDE_OAUTH_REFRESH_STATE_NAME
            state.write_bytes(b"\xff\xfe not utf-8 at all")
            self.assertIsNone(collector.read_claude_refresh_non_rotating_expiry(state))
            state.write_text("{not json", encoding="utf-8")
            self.assertIsNone(collector.read_claude_refresh_non_rotating_expiry(state))

    def test_non_rotation_state_is_written_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / collector.CLAUDE_OAUTH_REFRESH_STATE_NAME
            self.assertTrue(collector.write_claude_refresh_non_rotating_expiry(state, 1234.5))
            self.assertEqual(collector.read_claude_refresh_non_rotating_expiry(state), 1234.5)
            self.assertFalse(list(Path(tmp).glob("*.tmp")), "left a partial marker behind")

    def test_rotation_clears_a_recorded_non_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / collector.CLAUDE_OAUTH_REFRESH_STAMP_NAME
            state = Path(tmp) / collector.CLAUDE_OAUTH_REFRESH_STATE_NAME
            collector.write_claude_refresh_non_rotating_expiry(state, 1.0)
            expired = {
                "accessToken": "stale",
                "refreshToken": "refresh-secret",
                "expiresAt": 1000,
                "subscriptionType": "max",
            }
            rotated = {**expired, "accessToken": "new", "expiresAt": (time.time() + 8 * 3600) * 1000}
            with mock.patch.object(
                collector,
                "read_claude_oauth_credentials",
                side_effect=[dict(expired), dict(rotated)],
            ):
                with mock.patch.object(
                    collector.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ):
                    with mock.patch.object(
                        collector.urllib.request,
                        "urlopen",
                        return_value=self._oauth_usage_response(),
                    ):
                        collector.read_claude_oauth_usage(
                            Path("/tmp/claude"),
                            [],
                            claude_bin="/usr/local/bin/claude",
                            enable_refresh=True,
                            refresh_stamp=stamp,
                        )
            self.assertFalse(state.exists())

    def test_unwritable_cooldown_marker_blocks_the_ping(self) -> None:
        notes: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / collector.CLAUDE_OAUTH_REFRESH_STAMP_NAME
            with mock.patch.object(collector.Path, "touch", side_effect=OSError("read-only")):
                with mock.patch.object(collector.subprocess, "run") as run:
                    triggered = collector.trigger_claude_oauth_refresh(
                        "/usr/local/bin/claude", stamp, 30, "claude-haiku-4-5-20251001", notes,
                        state=self._refresh_state(stamp),
                    )
        self.assertFalse(triggered)
        run.assert_not_called()
        self.assertIn("Claude OAuth refresh skipped: cooldown marker unwritable", notes)

    def test_failed_credential_reread_reports_the_reread_failure(self) -> None:
        notes: list[str] = []
        expired = {
            "accessToken": "stale",
            "refreshToken": "refresh-secret",
            "expiresAt": 1000,
            "subscriptionType": "max",
        }

        def reads(_root, note_sink):
            if reads.calls:
                note_sink.append("Claude OAuth usage unavailable: credentials unreadable")
                return None
            reads.calls += 1
            return expired

        reads.calls = 0
        with mock.patch.object(collector, "read_claude_oauth_credentials", side_effect=reads):
            with mock.patch.object(
                collector.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ):
                limits, _ = collector.read_claude_oauth_usage(
                    Path("/tmp/claude"),
                    notes,
                    claude_bin="/usr/local/bin/claude",
                    enable_refresh=True,
                    refresh_stamp=self._refresh_stamp(),
                )
        self.assertIsNone(limits)
        self.assertIn("Claude OAuth usage unavailable: credentials unreadable", notes)

    def test_relative_claude_executable_is_resolved_before_the_cwd_change(self) -> None:
        notes: list[str] = []
        original_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "claude"
            target.write_text("#!/bin/sh\nexit 0\n")
            target.chmod(0o755)
            os.chdir(tmp)
            try:
                # A genuinely relative path: it would resolve against the
                # prompt's working directory instead of the collector's.
                relative = "./claude"
                self.assertFalse(Path(relative).is_absolute())
                with mock.patch.object(
                    collector.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0),
                ) as run:
                    stamp = Path(tmp) / collector.CLAUDE_OAUTH_REFRESH_STAMP_NAME
                    collector.trigger_claude_oauth_refresh(
                        relative, stamp, 0, "claude-haiku-4-5-20251001", notes,
                        workdir=Path(tmp) / "cwd",
                        state=self._refresh_state(stamp),
                    )
            finally:
                os.chdir(original_cwd)
            invoked = run.call_args.args[0][0]
            self.assertTrue(Path(invoked).is_absolute())
            self.assertEqual(Path(invoked).resolve(), target.resolve())

    def test_seconds_epoch_expiry_is_not_rescaled(self) -> None:
        future_seconds = time.time() + 3600
        self.assertAlmostEqual(
            collector.claude_oauth_expiry_epoch({"expiresAt": future_seconds}),
            future_seconds,
            places=3,
        )

    def _build_claude_feed(self, **kwargs) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            kwargs.setdefault(
                "oauth_refresh_stamp", Path(tmp) / collector.CLAUDE_OAUTH_REFRESH_STAMP_NAME
            )
            with mock.patch.object(collector, "ccusage", return_value={}):
                with mock.patch.object(collector, "read_claude_subscription_type", return_value="max"):
                    return collector.build_claude(
                        "/usr/local/bin/ccusage",
                        "UTC",
                        ZoneInfo("UTC"),
                        Path(tmp) / "claude",
                        True,
                        Path(tmp) / "claude-statusline.json",
                        "/usr/local/bin/claude",
                        30,
                        None,
                        **kwargs,
                    )

    def test_refresh_without_oauth_usage_is_reported_as_ignored(self) -> None:
        # The refresh only runs inside the OAuth usage path, so enabling it
        # alone must not look effective.
        with mock.patch.object(collector.subprocess, "run") as run:
            feed = self._build_claude_feed(enable_oauth_usage=False, enable_oauth_refresh=True)
        run.assert_not_called()
        self.assertIn(
            "Claude OAuth refresh ignored without Claude OAuth usage enabled",
            feed.get("notes", []),
        )

    def test_both_oauth_flags_reach_the_refresh_path(self) -> None:
        with mock.patch.object(
            collector,
            "read_claude_oauth_credentials",
            return_value={
                "accessToken": "stale",
                "refreshToken": "refresh-secret",
                "expiresAt": 1000,
                "subscriptionType": "max",
            },
        ):
            with mock.patch.object(
                collector.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as run:
                feed = self._build_claude_feed(enable_oauth_usage=True, enable_oauth_refresh=True)
        run.assert_called_once()
        self.assertNotIn(
            "Claude OAuth refresh ignored without Claude OAuth usage enabled",
            feed.get("notes", []),
        )

    def test_refresh_disabled_never_reaches_any_refresh_code(self) -> None:
        # Existing users who never set the new flag must be untouched. Every
        # refresh entry point is booby-trapped, so any disabled-path execution
        # raises instead of silently changing behaviour.
        expired = {
            "accessToken": "stale",
            "refreshToken": "refresh-secret",
            "expiresAt": 1000,
            "subscriptionType": "max",
        }

        def forbidden(*_args, **_kwargs):
            raise AssertionError("refresh code ran while refresh was disabled")

        for kwargs in ({"enable_oauth_usage": True}, {"enable_oauth_usage": True, "enable_oauth_refresh": False}):
            with mock.patch.object(collector, "read_claude_oauth_credentials", return_value=dict(expired)):
                with mock.patch.object(collector, "trigger_claude_oauth_refresh", side_effect=forbidden):
                    with mock.patch.object(collector, "claude_oauth_refresh_is_useful", side_effect=forbidden):
                        with mock.patch.object(collector.subprocess, "run", side_effect=forbidden):
                            feed = self._build_claude_feed(**kwargs)
            self.assertTrue(feed["ok"])
            self.assertNotIn(
                collector.CLAUDE_OAUTH_REFRESH_NO_ROTATION_NOTE,
                feed.get("notes", []),
            )

    def test_misconfiguration_note_survives_single_note_compaction(self) -> None:
        # Webhook payloads keep only the first note, so a silently inert
        # configuration must lead the list.
        with mock.patch.object(collector.subprocess, "run") as run:
            feed = self._build_claude_feed(enable_oauth_usage=False, enable_oauth_refresh=True)
        run.assert_not_called()
        self.assertEqual(
            feed["notes"][0],
            "Claude OAuth refresh ignored without Claude OAuth usage enabled",
        )

    def test_rejected_claude_oauth_token_does_not_publish_cached_plan(self) -> None:
        notes: list[str] = []
        # A real file object keeps HTTPError cleanup working on Python 3.9,
        # where closing one built with fp=None raises.
        error = collector.urllib.error.HTTPError(
            "https://example.invalid", 401, "Unauthorized", {}, io.BytesIO(b"")
        )
        with mock.patch.object(
            collector,
            "read_claude_oauth_credentials",
            return_value={"accessToken": "secret", "expiresAt": 4_102_444_800_000, "subscriptionType": "max"},
        ):
            with mock.patch.object(collector.urllib.request, "urlopen", side_effect=error):
                limits, subscription = collector.read_claude_oauth_usage(Path("/tmp/claude"), notes)
        self.assertIsNone(limits)
        self.assertIsNone(subscription)
        self.assertIn("run /login", notes[0])
        error.close()

    def test_rejected_claude_oauth_token_with_refresh_token_requests_cli_refresh(self) -> None:
        notes: list[str] = []
        # A real file object keeps HTTPError cleanup working on Python 3.9,
        # where closing one built with fp=None raises.
        error = collector.urllib.error.HTTPError(
            "https://example.invalid", 401, "Unauthorized", {}, io.BytesIO(b"")
        )
        with mock.patch.object(
            collector,
            "read_claude_oauth_credentials",
            return_value={
                "accessToken": "secret",
                "refreshToken": "refresh-secret",
                "expiresAt": 4_102_444_800_000,
                "subscriptionType": "max",
            },
        ):
            with mock.patch.object(collector.urllib.request, "urlopen", side_effect=error):
                limits, subscription = collector.read_claude_oauth_usage(Path("/tmp/claude"), notes)
        self.assertIsNone(limits)
        self.assertIsNone(subscription)
        self.assertIn("run Claude Code once to refresh", notes[0])
        self.assertEqual(collector.claude_usage_auth(notes, "unavailable"), "run Claude")
        error.close()

    def test_run_json_error_does_not_leak_absolute_command_path(self) -> None:
        errors: list[str] = []
        result = collector.run_json(["/private/home/example/bin/missing-ccusage", "codex", "daily"], errors)
        self.assertEqual(result, {})
        self.assertEqual(len(errors), 1)
        self.assertNotIn("/private/home/example", errors[0])
        self.assertNotIn("codex", errors[0])
        self.assertNotIn("daily", errors[0])
        self.assertTrue(errors[0].startswith("missing-ccusage:"))
        self.assertIn("FileNotFoundError", errors[0])

    def test_sessions_summary_does_not_publish_project_path(self) -> None:
        tz = ZoneInfo("UTC")
        today = datetime(2026, 7, 6, 12, 0, tzinfo=tz)
        summary = collector.sessions_summary(
            [
                {
                    "lastActivity": "2026-07-06T11:00:00Z",
                    "projectPath": "/private/work/customer-sensitive-project",
                    "totalTokens": 123,
                }
            ],
            today,
            today,
        )
        self.assertEqual(summary["top_project"], "n/a")
        self.assertNotIn("customer-sensitive-project", json.dumps(summary))

    def test_limit_card_window_labels(self) -> None:
        tz = ZoneInfo("America/Toronto")
        raw = {
            "primary": {
                "used_percent": 12.4,
                "window_minutes": 300,
                "resets_at": 1_783_234_285,
            },
            "weekly": {
                "used_percent": 51,
                "window_minutes": 10080,
                "resets_at": 1_783_388_670,
            },
        }
        self.assertEqual(collector.limit_card(raw, "primary", "5h", tz)["window"], "5h")
        self.assertEqual(collector.limit_card(raw, "weekly", "Weekly", tz)["window"], "7d")

    def test_unknown_cost(self) -> None:
        card = collector.usage_card("Last 5h", {"totalTokens": 1000, "costUSD": 0}, 3)
        self.assertEqual(collector.unknown_cost(card)["cost"], "n/a")

    def test_usage_title_preserves_native_plan_value(self) -> None:
        self.assertEqual(collector.usage_title("Codex", "prolite"), "Codex prolite Usage")
        self.assertEqual(collector.usage_title("Claude Code", "max"), "Claude Code max Usage")
        self.assertEqual(collector.usage_title("Codex", None), "Codex Usage")

    def test_claude_subscription_uses_auth_status_native_value(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["claude", "auth", "status", "--json"],
            returncode=0,
            stdout='{"loggedIn":true,"subscriptionType":"max"}',
            stderr="",
        )
        notes: list[str] = []
        with mock.patch.object(collector.subprocess, "run", return_value=completed) as run:
            self.assertEqual(collector.read_claude_subscription_type("claude", notes), "max")
        run.assert_called_once_with(
            ["claude", "auth", "status", "--json"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
        self.assertIn("Claude subscription from claude auth status", notes)

    def test_current_claude_auth_status_without_subscription_type_is_not_guessed(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["claude", "auth", "status", "--json"],
            returncode=0,
            stdout='{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty"}',
            stderr="",
        )
        notes: list[str] = []
        with mock.patch.object(collector.subprocess, "run", return_value=completed):
            subscription = collector.read_claude_subscription_type("claude", notes)

        self.assertIsNone(subscription)
        self.assertIn("Claude auth status did not expose subscriptionType", notes)

    def test_codex_app_rate_limit_snapshot_normalizes_to_dashboard_shape(self) -> None:
        tz = ZoneInfo("UTC")
        snapshot = {
            "limitId": "codex_bengalfox",
            "limitName": "Codex",
            "primary": {"usedPercent": 3, "windowDurationMins": 300, "resetsAt": "2026-07-05T18:00:00Z"},
            "secondary": {"usedPercent": 57, "windowDurationMins": 10080, "resetsAt": "2026-07-07T00:00:00Z"},
            "credits": {"hasCredits": True, "unlimited": False, "balance": "42"},
            "planType": "prolite",
        }
        normalized = collector.normalize_app_rate_limit_snapshot(snapshot)
        self.assertEqual(normalized["limit_id"], "codex_bengalfox")
        self.assertEqual(normalized["credits"]["balance"], "42")
        self.assertEqual(collector.limit_card(normalized, "primary", "5h", tz)["window"], "5h")
        self.assertEqual(collector.limit_card(normalized, "secondary", "Weekly", tz)["used"], "57%")

    def test_codex_current_multi_bucket_response_selects_general_bucket(self) -> None:
        selected = collector.select_app_rate_limit_snapshot(
            {
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {"usedPercent": 45, "windowDurationMins": 10080},
                },
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitId": "codex",
                        "planType": "pro",
                        "primary": {"usedPercent": 44, "windowDurationMins": 10080},
                    },
                    "codex_bengalfox": {
                        "limitId": "codex_bengalfox",
                        "limitName": "GPT-5.3-Codex-Spark",
                        "planType": "pro",
                        "primary": {"usedPercent": 0, "windowDurationMins": 10080},
                    },
                },
            }
        )
        self.assertEqual(selected["limit_id"], "codex")
        self.assertEqual(selected["plan_type"], "pro")
        self.assertEqual(selected["primary"]["used_percent"], 45)

    def test_codex_multi_bucket_response_works_without_legacy_view(self) -> None:
        selected = collector.select_app_rate_limit_snapshot(
            {
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitId": "codex",
                        "planType": "prolite",
                        "primary": {"usedPercent": 12, "windowDurationMins": 300},
                    }
                }
            }
        )
        self.assertEqual(selected["limit_id"], "codex")
        self.assertEqual(selected["plan_type"], "prolite")

    def test_codex_rolling_usage_skips_replay_dedupes_and_splits_cached_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            sessions = Path(temp) / "sessions"
            sessions.mkdir()
            replay_at = datetime.now(timezone.utc).replace(microsecond=0)
            actual_at = replay_at + timedelta(minutes=1, milliseconds=123)

            def token_row(timestamp, *, last=None, total=None, model="gpt-5.6-sol"):
                info = {"model": model}
                if last is not None:
                    info["last_token_usage"] = last
                if total is not None:
                    info["total_token_usage"] = total
                return {
                    "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
                    "type": "event_msg",
                    "payload": {"type": "token_count", "info": info},
                }

            replay_one = {
                "input_tokens": 1_000,
                "cached_input_tokens": 100,
                "output_tokens": 200,
                "reasoning_output_tokens": 0,
                "total_tokens": 1_200,
            }
            replay_two = {
                "input_tokens": 2_000,
                "cached_input_tokens": 200,
                "output_tokens": 400,
                "reasoning_output_tokens": 0,
                "total_tokens": 2_400,
            }
            cumulative_actual = {
                "input_tokens": 2_200,
                "cached_input_tokens": 250,
                "output_tokens": 430,
                "reasoning_output_tokens": 5,
                "total_tokens": 2_630,
            }
            actual_delta = {
                "input_tokens": 200,
                "cached_input_tokens": 50,
                "output_tokens": 30,
                "reasoning_output_tokens": 5,
                "total_tokens": 230,
            }
            replay_file = sessions / "a-subagent.jsonl"
            replay_file.write_text(
                "\n".join(
                    json.dumps(row)
                    for row in [
                        {
                            "timestamp": replay_at.isoformat().replace("+00:00", "Z"),
                            "type": "session_meta",
                            "payload": {"source": {"subagent": {"thread_spawn": {"parent_thread_id": "p"}}}},
                        },
                        token_row(replay_at, last=replay_one, total=replay_one),
                        token_row(replay_at, last=replay_two, total=replay_two),
                        token_row(actual_at, total=cumulative_actual),
                    ]
                ),
                encoding="utf-8",
            )
            (sessions / "b-duplicate.jsonl").write_text(
                json.dumps(token_row(actual_at, last=actual_delta, total=actual_delta)),
                encoding="utf-8",
            )

            _, _, metrics, session_count = collector.latest_codex_rate_limits(Path(temp))

            self.assertEqual(collector.codex_replay_second(replay_file), replay_at.isoformat()[:19])

        self.assertEqual(metrics["inputTokens"], 150)
        self.assertEqual(metrics["cacheReadTokens"], 50)
        self.assertEqual(metrics["outputTokens"], 30)
        self.assertEqual(metrics["reasoningOutputTokens"], 5)
        self.assertEqual(metrics["totalTokens"], 230)
        self.assertEqual(session_count, 1)

    def test_codex_session_candidates_skip_stale_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            sessions = Path(temp) / "sessions"
            sessions.mkdir()
            stale = sessions / "stale.jsonl"
            recent = sessions / "recent.jsonl"
            stale.write_text("{}\n", encoding="utf-8")
            recent.write_text("{}\n", encoding="utf-8")
            since = datetime.now(timezone.utc) - timedelta(hours=5)
            old_mtime = since.timestamp() - 60
            os.utime(stale, (old_mtime, old_mtime))

            candidates = collector.codex_session_candidates(sessions, since)

        self.assertEqual(candidates, [recent])

    def test_codex_session_candidates_keep_newest_stale_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            sessions = Path(temp) / "sessions"
            sessions.mkdir()
            older = sessions / "older.jsonl"
            newest = sessions / "newest.jsonl"
            older.write_text("{}\n", encoding="utf-8")
            newest.write_text("{}\n", encoding="utf-8")
            since = datetime.now(timezone.utc) - timedelta(hours=5)
            older_mtime = since.timestamp() - 120
            newest_mtime = since.timestamp() - 60
            os.utime(older, (older_mtime, older_mtime))
            os.utime(newest, (newest_mtime, newest_mtime))

            candidates = collector.codex_session_candidates(sessions, since)

        self.assertEqual(candidates, [newest])

    def test_codex_rate_limit_windows_are_classified_by_duration(self) -> None:
        snapshot = {
            "limit_id": "codex",
            "plan_type": "pro",
            "primary": {"used_percent": 24, "window_minutes": 10080, "resets_at": 1784487507},
            "secondary": None,
        }
        classified = collector.classify_codex_rate_limit_windows(snapshot)
        self.assertIsNone(classified["primary"])
        self.assertEqual(classified["secondary"]["used_percent"], 24)
        self.assertEqual(collector.limit_card(classified, "primary", "5h", ZoneInfo("UTC"), "5h")["used"], "n/a")
        self.assertEqual(collector.limit_card(classified, "secondary", "Weekly", ZoneInfo("UTC"), "7d")["used"], "24%")

    def test_codex_rate_limit_windows_preserve_legacy_primary_secondary_order(self) -> None:
        snapshot = {
            "primary": {"used_percent": 3, "window_minutes": 300},
            "secondary": {"used_percent": 57, "window_minutes": 10080},
        }
        classified = collector.classify_codex_rate_limit_windows(snapshot)
        self.assertEqual(classified["primary"]["used_percent"], 3)
        self.assertEqual(classified["secondary"]["used_percent"], 57)

    def test_codex_multi_limit_response_renders_weekly_all_and_named_bucket(self) -> None:
        normalized = collector.normalize_codex_rate_limit_response(
            {
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {"usedPercent": 2, "windowDurationMins": 10080, "resetsAt": 1784783576},
                    "secondary": None,
                    "planType": "pro",
                },
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitId": "codex",
                        "primary": {"usedPercent": 2, "windowDurationMins": 10080, "resetsAt": 1784783576},
                        "secondary": None,
                        "planType": "pro",
                    },
                    "codex_bengalfox": {
                        "limitId": "codex_bengalfox",
                        "limitName": "GPT-5.3-Codex-Spark",
                        "primary": {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": 1784823755},
                        "secondary": None,
                        "planType": "pro",
                    },
                },
            }
        )
        classified = collector.classify_codex_rate_limit_windows(normalized)
        cards = collector.codex_dashboard_limit_cards(
            classified,
            ZoneInfo("UTC"),
            datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(cards["primary"]["label"], "Weekly all")
        self.assertEqual(cards["primary"]["used"], "2%")
        self.assertEqual(cards["weekly"]["label"], "Spark")
        self.assertEqual(cards["weekly"]["used"], "0%")

    def test_codex_general_five_hour_with_scoped_weekly_keeps_scoped_label(self) -> None:
        normalized = collector.normalize_codex_rate_limit_response(
            {
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitId": "codex",
                        "primary": {"usedPercent": 11, "windowDurationMins": 300, "resetsAt": 1784823755},
                        "secondary": None,
                        "planType": "pro",
                    },
                    "codex_bengalfox": {
                        "limitId": "codex_bengalfox",
                        "limitName": "GPT-5.3-Codex-Spark",
                        "primary": {"usedPercent": 7, "windowDurationMins": 10080, "resetsAt": 1784823755},
                        "secondary": None,
                        "planType": "pro",
                    },
                }
            }
        )
        cards = collector.codex_dashboard_limit_cards(
            collector.classify_codex_rate_limit_windows(normalized),
            ZoneInfo("UTC"),
            datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(cards["primary"]["label"], "5h")
        self.assertEqual(cards["weekly"]["label"], "Spark")

    def test_codex_account_usage_normalizes_lifetime_and_daily_rows(self) -> None:
        usage = collector.normalize_codex_account_usage(
            {
                "summary": {
                    "lifetimeTokens": 480_000_000,
                    "peakDailyTokens": 4_000_000,
                    "currentStreakDays": 7,
                    "longestStreakDays": 14,
                },
                "dailyUsageBuckets": [{"startDate": "2026-07-15", "tokens": 2_400_000}],
            }
        )
        self.assertEqual(usage["lifetime"], "480M")
        self.assertEqual(usage["peak_day"], "4.00M")
        self.assertEqual(usage["daily_rows"][0]["date"], "2026-07-15")

    def test_codex_account_usage_preserves_nullable_summary_fields(self) -> None:
        usage = collector.normalize_codex_account_usage(
            {
                "summary": {
                    "lifetimeTokens": None,
                    "peakDailyTokens": None,
                    "currentStreakDays": None,
                    "longestStreakDays": None,
                },
                "dailyUsageBuckets": [{"startDate": "2026-07-15", "tokens": None}],
            }
        )
        self.assertIsNone(usage["lifetime"])
        self.assertIsNone(usage["peak_day"])
        self.assertIsNone(usage["current_streak"])
        self.assertIsNone(usage["longest_streak"])
        self.assertEqual(usage["daily_rows"], [])

    def test_codex_private_reset_credit_endpoint_is_opt_in(self) -> None:
        originals = {
            "ccusage": collector.ccusage,
            "latest_codex_rate_limits": collector.latest_codex_rate_limits,
            "read_codex_app_data": collector.read_codex_app_data,
            "read_codex_private_reset_credits": collector.read_codex_private_reset_credits,
        }

        def fake_ccusage(_bin, args, _tz_name, _offline, _errors):
            if args[1] == "daily":
                return {"daily": [], "totals": {}}
            if args[1] == "monthly":
                return {"monthly": []}
            if args[1] == "session":
                return {"sessions": []}
            self.fail(f"unexpected ccusage args: {args}")

        def fail_private_endpoint(*_args, **_kwargs):
            raise AssertionError("private reset-credit endpoint should not be called by default")

        try:
            collector.ccusage = fake_ccusage
            collector.latest_codex_rate_limits = lambda _root: (None, None, {}, 0)
            collector.read_codex_app_data = lambda _bin, _notes: (None, None, None)
            collector.read_codex_private_reset_credits = fail_private_endpoint
            feed = collector.build_codex(
                "ccusage",
                "UTC",
                ZoneInfo("UTC"),
                Path("/tmp/nonexistent-codex-root"),
                True,
                "codex",
                False,
            )
        finally:
            for name, value in originals.items():
                setattr(collector, name, value)

        self.assertTrue(feed["ok"])
        self.assertIn("Private reset-credit endpoint disabled", feed["notes"])

    def test_codex_private_reset_credit_endpoint_is_skipped_when_supported_details_exist(self) -> None:
        def fake_ccusage(_bin, args, _tz_name, _offline, _errors):
            if args[1] == "daily":
                return {"daily": [], "totals": {}}
            if args[1] == "monthly":
                return {"monthly": []}
            if args[1] == "session":
                return {"sessions": []}
            self.fail(f"unexpected ccusage args: {args}")

        app_limits = {
            "limit_id": "codex",
            "plan_type": "pro",
            "primary": {"used_percent": 10, "window_minutes": 300},
            "secondary": {"used_percent": 20, "window_minutes": 10080},
        }
        supported = {
            "availableCount": 1,
            "credits": [{"status": "available", "expiresAt": "2026-07-17T00:00:00Z"}],
        }
        with (
            mock.patch.object(collector, "ccusage", side_effect=fake_ccusage),
            mock.patch.object(collector, "latest_codex_rate_limits", return_value=(None, None, {}, 0)),
            mock.patch.object(collector, "read_codex_app_data", return_value=(app_limits, supported, None)),
            mock.patch.object(collector, "read_codex_private_reset_credits") as private_read,
        ):
            feed = collector.build_codex(
                "ccusage",
                "UTC",
                ZoneInfo("UTC"),
                Path("/tmp/nonexistent-codex-root"),
                True,
                "codex",
                True,
            )

        private_read.assert_not_called()
        self.assertEqual(feed["credits"]["banked_reset_source"], "app-server")
        self.assertIn("private fallback not needed", " ".join(feed["notes"]))

    def test_reset_credit_display_keeps_supported_count_with_private_expiry_fallback(self) -> None:
        tz = ZoneInfo("UTC")
        display = collector.reset_credit_display(
            {"availableCount": 1},
            {
                "available_count": 2,
                "credits": [
                    {"status": "available", "expires_at": "2026-07-17T00:00:00Z"},
                    {"status": "redeemed", "expires_at": "2026-07-01T00:00:00Z"},
                ],
            },
            tz,
        )
        self.assertEqual(display["banked_reset"], "1")
        self.assertEqual(display["banked_reset_expires"], "2026-07-17 00:00")
        self.assertEqual(display["banked_reset_expires_short"], "07-17 00:00")
        self.assertEqual(display["banked_reset_summary"], "1 @ 07-17 00:00")
        self.assertEqual(display["banked_reset_source"], "app-server + private wham")

    def test_reset_credit_display_prefers_supported_expiry_details(self) -> None:
        display = collector.reset_credit_display(
            {
                "availableCount": 2,
                "credits": [
                    {"status": "available", "expiresAt": "2026-07-16T12:00:00Z"},
                    {"status": "redeemed", "expiresAt": "2026-07-01T00:00:00Z"},
                ],
            },
            {
                "available_count": 9,
                "credits": [{"status": "available", "expires_at": "2026-07-15T00:00:00Z"}],
            },
            ZoneInfo("UTC"),
        )
        self.assertEqual(display["banked_reset"], "2")
        self.assertEqual(display["banked_reset_expires"], "2026-07-16 12:00")
        self.assertEqual(display["banked_reset_source"], "app-server")

    def test_claude_statusline_cache_drives_limit_cards(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "claude-statusline.json"
            cache.write_text(
                json.dumps(
                    {
                        "captured_at": "2026-07-05T12:00:00-04:00",
                        "source": "Claude Code statusLine stdin",
                        "rate_limits": {
                            "five_hour": {"used_percentage": 12.4, "resets_at": 1783274400},
                            "weekly": {"used_percentage": 34.1, "resets_at": 1783879200},
                        },
                    }
                ),
                encoding="utf-8",
            )
            tz = ZoneInfo("America/Toronto")
            now = datetime(2026, 7, 5, 13, 0, tzinfo=tz)
            statusline = collector.load_claude_statusline_cache(cache, [], now, tz, max_age_minutes=120)
            primary = collector.statusline_limit_card(statusline["rate_limits"], "five_hour", "5h block", "5h", tz)
            weekly = collector.statusline_limit_card(statusline["rate_limits"], "weekly", "Weekly", "7d", tz)
            self.assertEqual(primary["used"], "12%")
            self.assertEqual(weekly["used"], "34%")
            self.assertEqual(statusline["captured_age"], "60m ago")

    def test_stale_claude_statusline_cache_keeps_reset_but_drops_percentage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp) / "claude-statusline.json"
            cache.write_text(
                json.dumps(
                    {
                        "captured_at": "2026-07-05T10:00:00-04:00",
                        "source": "Claude Code statusLine stdin",
                        "rate_limits": {
                            "five_hour": {"used_percentage": 12.4, "resets_at": 1783274400},
                        },
                    }
                ),
                encoding="utf-8",
            )
            tz = ZoneInfo("America/Toronto")
            now = datetime(2026, 7, 5, 13, 0, tzinfo=tz)
            warnings: list[str] = []
            statusline = collector.load_claude_statusline_cache(cache, warnings, now, tz, max_age_minutes=30)
            self.assertTrue(statusline["stale"])
            limits = collector.valid_claude_statusline_limits(statusline["rate_limits"], now, stale=True)
            self.assertNotIn("used_percentage", limits["five_hour"])
            self.assertEqual(limits["five_hour"]["resets_at"], 1783274400)
            self.assertEqual(statusline["captured_age"], "3h ago")
            self.assertIn("Claude statusline cache stale: 3h ago", warnings)

    def test_expired_claude_statusline_window_is_discarded(self) -> None:
        tz = ZoneInfo("America/Toronto")
        now = datetime(2026, 7, 5, 13, 0, tzinfo=tz)
        limits = collector.valid_claude_statusline_limits(
            {
                "five_hour": {"used_percentage": 12.4, "resets_at": "2026-07-05T16:00:00Z"},
                "weekly": {"used_percentage": 34.1, "resets_at": "2026-07-06T16:00:00Z"},
                "without_reset": {"used_percentage": 99},
            },
            now,
            stale=True,
        )
        self.assertNotIn("five_hour", limits)
        self.assertNotIn("without_reset", limits)
        self.assertEqual(limits["weekly"], {"resets_at": "2026-07-06T16:00:00Z"})

    def test_claude_statusline_capture_sanitizes_windows(self) -> None:
        window = capture.safe_window({"used_percentage": 23.5, "resets_at": 123, "extra": "private-path-not-copied"})
        self.assertEqual(window, {"used_percentage": 23.5, "resets_at": 123})

    def test_claude_statusline_capture_preserves_iso_reset_strings(self) -> None:
        window = capture.safe_window({"used_percentage": 23.5, "resets_at": "2026-07-06T12:00:00Z"})
        self.assertEqual(window, {"used_percentage": 23.5, "resets_at": "2026-07-06T12:00:00Z"})

    def test_atomic_write_uses_private_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "feed.json"
            collector.atomic_write(path, {"ok": True})
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_unexpected_feed_failure_becomes_sanitized_error_feed(self) -> None:
        def fail():
            raise ValueError("do not expose /Users/example/private")

        feed = collector.build_feed_safely("codex", "Codex Usage", ZoneInfo("UTC"), fail)

        self.assertFalse(feed["ok"])
        self.assertEqual(feed["kind"], "codex")
        self.assertEqual(feed["errors"], ["ValueError"])
        self.assertNotIn("/Users/", json.dumps(feed))


class FeedServerTests(unittest.TestCase):
    def request(self, server, path: str, method: str = "GET") -> tuple[int, str]:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request(method, path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        return resp.status, body

    def start_server(self, directory: Path):
        handler = lambda *args, **kwargs: server_mod.FeedHandler(  # noqa: E731
            *args,
            directory=str(directory),
            **kwargs,
        )
        httpd = server_mod.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd

    def test_health_requires_both_ok_feeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            httpd = self.start_server(root)
            try:
                status, body = self.request(httpd, "/health")
                self.assertEqual(status, 503)
                self.assertFalse(json.loads(body)["ok"])

                (root / "codex.json").write_text('{"ok": true}', encoding="utf-8")
                (root / "claude.json").write_text('{"ok": true}', encoding="utf-8")
                status, body = self.request(httpd, "/health")
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(body)["ok"])
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_directory_listing_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            httpd = self.start_server(Path(temp))
            try:
                status, _ = self.request(httpd, "/")
                self.assertEqual(status, 404)
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_only_known_feed_files_are_served(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "codex.json").write_text('{"ok": true}', encoding="utf-8")
            (root / "hidden.txt").write_text("do not serve", encoding="utf-8")
            httpd = self.start_server(root)
            try:
                status, body = self.request(httpd, "/codex.json?cache_bust=1")
                self.assertEqual(status, 200)
                self.assertIn('"ok": true', body)

                status, _ = self.request(httpd, "/hidden.txt")
                self.assertEqual(status, 404)

                status, _ = self.request(httpd, "/claude-statusline.json")
                self.assertEqual(status, 404)

                status, _ = self.request(httpd, "/hidden.txt", method="HEAD")
                self.assertEqual(status, 404)

                status, _ = self.request(httpd, "/claude-statusline.json", method="HEAD")
                self.assertEqual(status, 404)
            finally:
                httpd.shutdown()
                httpd.server_close()


class FeedSanitizationTests(unittest.TestCase):
    def test_statusline_capture_sanitization_drops_unknown_fields(self) -> None:
        window = capture.safe_window(
            {
                "used_percentage": 12.5,
                "resets_at": 1234567890,
                "prompt": "should not be copied",
                "path": "/tmp/example",
            }
        )
        self.assertEqual(window, {"used_percentage": 12.5, "resets_at": 1234567890})


class WebhookPayloadTests(unittest.TestCase):
    def test_sample_payloads_fit_trmnl_webhook_free_tier_limit(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        for name in ("codex", "claude"):
            feed = json.loads((repo / "examples" / f"{name}.sample.json").read_text(encoding="utf-8"))
            body = push.webhook_payload(feed)
            self.assertLessEqual(len(body), push.DEFAULT_MAX_BYTES, name)
            payload = json.loads(body)
            self.assertIn("source_1", payload["merge_variables"])
            self.assertEqual(payload["merge_variables"]["source_1"]["cost_basis"], "Token Cost*")
        codex = json.loads((repo / "examples" / "codex.sample.json").read_text(encoding="utf-8"))
        claude = json.loads((repo / "examples" / "claude.sample.json").read_text(encoding="utf-8"))
        self.assertEqual(push.compact_feed(codex)["account_usage"]["lifetime"], "480M")
        self.assertEqual(push.compact_feed(claude)["auth"]["usage"], "live")

    def test_sample_titles_include_native_plan_value(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        expectations = {"codex": "prolite", "claude": "max"}
        for name, label in expectations.items():
            feed = json.loads((repo / "examples" / f"{name}.sample.json").read_text(encoding="utf-8"))
            self.assertIn(label, feed["title"])
            self.assertEqual(feed["plan"], label)

    def test_samples_declare_deterministic_synthetic_provenance(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        for name in ("codex", "claude"):
            feed = json.loads((repo / "examples" / f"{name}.sample.json").read_text(encoding="utf-8"))
            self.assertIn("deterministic synthetic schema fixture", feed["notes"])

    def test_sample_derived_values_match_collector_formatting(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        codex = json.loads((repo / "examples" / "codex.sample.json").read_text(encoding="utf-8"))
        claude = json.loads((repo / "examples" / "claude.sample.json").read_text(encoding="utf-8"))

        def parse_short(value: str) -> int:
            multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
            suffix = value[-1]
            return round(float(value[:-1]) * multipliers[suffix]) if suffix in multipliers else int(value)

        self.assertEqual(codex["usage"]["five_hour"]["cache_write"], "0")
        self.assertEqual(codex["usage"]["five_hour"]["cache"], "620K")
        self.assertEqual(codex["usage"]["month"]["cost"], "$126")
        self.assertEqual(codex["usage"]["all"]["cost"], "$884")
        self.assertEqual([item["pct"] for item in codex["daily_bars"]], [100, 50, 25, 12, 6, 4, 4])
        self.assertEqual(
            sum(item["raw_tokens"] for item in codex["models"]),
            codex["usage"]["week"]["total_raw"],
        )
        self.assertEqual([item["pct"] for item in codex["models"]], [57, 29, 14])

        self.assertEqual(claude["usage"]["all"]["cost"], "$430")
        self.assertEqual(
            [item["pct"] for item in claude["daily_bars"]],
            [100, 62, 34, 24, 17, 14, 10],
        )
        self.assertEqual(
            sum(parse_short(item["tokens"]) for item in claude["daily_bars"]),
            claude["usage"]["week"]["total_raw"],
        )
        self.assertEqual(
            sum(item["raw_tokens"] for item in claude["models"]),
            claude["usage"]["week"]["total_raw"],
        )
        self.assertEqual([item["pct"] for item in claude["models"]], [80, 20])

        for feed in (codex, claude):
            for card in feed["usage"].values():
                for key in ("input", "output", "reasoning", "cache_read", "cache_write", "cache"):
                    self.assertEqual(card[key], collector.short_number(parse_short(card[key])))
                component_total = sum(
                    parse_short(card[key])
                    for key in ("input", "output", "cache_read", "cache_write")
                )
                self.assertEqual(card["total_raw"], component_total)
                self.assertEqual(card["total"], collector.short_number(component_total))
                cache = parse_short(card["cache_read"]) + parse_short(card["cache_write"])
                self.assertEqual(card["cache"], collector.short_number(cache))
                expected_cache_hit = round(cache / max(cache + parse_short(card["input"]), 1) * 100)
                self.assertEqual(card["cache_hit"], f"{expected_cache_hit}%")

            grand = sum(item["raw_tokens"] for item in feed["models"])
            for item in feed["models"]:
                self.assertEqual(item["tokens"], collector.short_number(item["raw_tokens"]))
                self.assertEqual(item["pct"], collector.pct(item["raw_tokens"] * 100 / grand))

    def test_codex_sample_does_not_include_claude_models(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        feed = json.loads((repo / "examples" / "codex.sample.json").read_text(encoding="utf-8"))
        model_names = [item["name"].lower() for item in feed["models"]]
        self.assertTrue(model_names)
        self.assertFalse(any("claude" in name or "opus" in name for name in model_names))

    def test_codex_plan_is_not_rendered_as_duplicate_status_row(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        for template_name in DASHBOARD_TEMPLATE_NAMES:
            template = (repo / "templates" / template_name).read_text(encoding="utf-8")
            self.assertNotIn("{{ s.plan }}", template)

    def test_templates_do_not_override_trmnl_screen_wrapper(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        for template_name in DASHBOARD_TEMPLATE_NAMES:
            template = (repo / "templates" / template_name).read_text(encoding="utf-8")
            self.assertNotRegex(template, r"(?<![-\w])\.screen\b", template_name)
            self.assertNotRegex(template, r"""class=(["'])(?:(?!\1).)*(?:^|\s)screen(?:\s|["'])""", template_name)

    def test_templates_with_limit_bars_render_an_explicit_no_data_state(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        names = (
            "agent-usage-dashboard.liquid",
            "agent-usage-dashboard-bwr.liquid",
            "agent-usage-dashboard-bwr-half-vertical.liquid",
            "agent-usage-dashboard-bwr-quadrant.liquid",
        )
        for template_name in names:
            template = (repo / "templates" / template_name).read_text(encoding="utf-8")
            self.assertIn("bar-empty", template, template_name)
            self.assertIn("s.limits.primary.used != 'n/a'", template, template_name)
            self.assertIn("s.limits.weekly.used != 'n/a'", template, template_name)

    def test_templates_render_claude_scoped_limit(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        for template_name in DASHBOARD_TEMPLATE_NAMES:
            template = (repo / "templates" / template_name).read_text(encoding="utf-8")
            self.assertIn("s.limits.scoped", template, template_name)

    def test_zero_activity_bar_has_no_minimum_colored_width(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        for template_name in ("agent-usage-dashboard.liquid", "agent-usage-dashboard-bwr.liquid"):
            template = (repo / "templates" / template_name).read_text(encoding="utf-8")
            self.assertRegex(template, r"\.smallfill \{[^}]*min-width: 0;", template_name)

    def test_full_templates_label_account_and_local_scopes(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        for template_name in ("agent-usage-dashboard.liquid", "agent-usage-dashboard-bwr.liquid"):
            template = (repo / "templates" / template_name).read_text(encoding="utf-8")
            self.assertIn("s.account_usage.lifetime", template, template_name)
            self.assertIn("s.activity_scope", template, template_name)
            self.assertIn("s.auth.usage", template, template_name)
            self.assertIn("Weekly all", template, template_name)

    def test_full_templates_use_clear_session_footer_and_scoped_no_data_state(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        for template_name in ("agent-usage-dashboard.liquid", "agent-usage-dashboard-bwr.liquid"):
            template = (repo / "templates" / template_name).read_text(encoding="utf-8")
            self.assertIn("sess {{ s.sessions.today }}", template, template_name)
            self.assertIn("s.limits.scoped and s.limits.scoped.used", template, template_name)
            self.assertIn("s.limits.scoped and s.limits.scoped.reset_full", template, template_name)
            self.assertIn("s.statusline and s.statusline.captured_age", template, template_name)

    def test_full_template_header_reserves_space_for_single_line_metadata(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        for template_name in ("agent-usage-dashboard.liquid", "agent-usage-dashboard-bwr.liquid"):
            template = (repo / "templates" / template_name).read_text(encoding="utf-8")
            self.assertIn("grid-template-columns: minmax(0, 1fr) auto;", template, template_name)
            self.assertRegex(template, r"\.title \{[^}]*min-width: 0;", template_name)

    def test_templates_label_token_cost_conversions(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        for template_name in DASHBOARD_TEMPLATE_NAMES:
            template = (repo / "templates" / template_name).read_text(encoding="utf-8")
            self.assertIn("s.cost_basis", template, template_name)
        for template_name in ("agent-usage-dashboard.liquid", "agent-usage-dashboard-bwr.liquid"):
            template = (repo / "templates" / template_name).read_text(encoding="utf-8")
            self.assertIn("Today Token Cost*", template, template_name)
            self.assertIn("7d Token Cost*", template, template_name)
        quadrant = (repo / "templates" / "agent-usage-dashboard-bwr-quadrant.liquid").read_text(encoding="utf-8")
        self.assertIn("s.usage.week.cost", quadrant)

    def test_template_css_is_scoped_to_dashboard_root(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        for template_name in DASHBOARD_TEMPLATE_NAMES:
            template = (repo / "templates" / template_name).read_text(encoding="utf-8")
            styles = re.findall(r"<style>(.*?)</style>", template, flags=re.DOTALL)
            self.assertTrue(styles, template_name)
            for style in styles:
                self.assertNotIn("html, body", style, template_name)
                self.assertNotRegex(style, r"(?m)^\s*\*\s*\{", template_name)
                for selector_group in re.findall(r"(?m)^\s*([^@{}\n][^{}]*)\s*\{", style):
                    selectors = [item.strip() for item in selector_group.split(",")]
                    for selector in selectors:
                        self.assertTrue(
                            selector == ".agent-usage-screen" or selector.startswith(".agent-usage-screen "),
                            f"{template_name}: unscoped selector {selector!r}",
                        )

    def test_webhook_errors_do_not_print_private_url(self) -> None:
        status, message = push.post_payload(
            "codex",
            "http://127.0.0.1:9/api/custom_plugins/private-plugin-uuid",
            b'{"merge_variables":{"source_1":{"ok":true}}}',
            0.1,
        )
        self.assertEqual(status, 0)
        self.assertNotIn("private-plugin-uuid", message)

    def test_malformed_webhook_url_does_not_raise_or_print_private_url(self) -> None:
        status, message = push.post_payload(
            "codex",
            "trmnl.com/api/custom_plugins/private-plugin-uuid",
            b'{"merge_variables":{"source_1":{"ok":true}}}',
            0.1,
        )
        self.assertEqual(status, 0)
        self.assertIn("ValueError", message)
        self.assertNotIn("private-plugin-uuid", message)


class DemoAssetTests(unittest.TestCase):
    def test_readme_demo_pngs_are_800_by_480(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        for name in ("codex", "claude"):
            path = repo / "docs" / "assets" / f"{name}-demo.png"
            with path.open("rb") as handle:
                header = handle.read(24)
            self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n", name)
            width, height = struct.unpack(">II", header[16:24])
            self.assertEqual((width, height), (800, 480), name)


class StatuslineWrapperTests(unittest.TestCase):
    def test_wrapper_captures_cache_and_preserves_delegate_stdin(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        wrapper = repo / "scripts" / "claude_statusline_capture_wrapper.sh"
        payload = (
            '{"rate_limits":{"five_hour":{"used_percentage":12.5,"resets_at":1780000000,'
            '"ignored":"drop"},"seven_day":{"used_percentage":44,"resets_at":1780500000}}}\n'
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            delegate = root / "delegate.sh"
            delegate_out = root / "delegate.in"
            delegate.write_text('#!/bin/sh\ncat > "$TRMNL_TEST_DELEGATE_OUT"\n', encoding="utf-8")
            delegate.chmod(0o700)
            env = os.environ.copy()
            env.update(
                {
                    "TRMNL_AGENT_USAGE_CACHE_DIR": str(root / "cache"),
                    "TRMNL_AGENT_USAGE_TIMEZONE": "UTC",
                    "TRMNL_AGENT_USAGE_CLAUDE_STATUSLINE_DELEGATE": str(delegate),
                    "TRMNL_TEST_DELEGATE_OUT": str(delegate_out),
                }
            )
            subprocess.run(
                ["sh", str(wrapper)],
                input=payload,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                timeout=10,
                env=env,
            )
            self.assertEqual(delegate_out.read_text(encoding="utf-8"), payload)
            cache = json.loads((root / "cache" / "claude-statusline.json").read_text(encoding="utf-8"))
            self.assertEqual(cache["rate_limits"]["five_hour"]["used_percentage"], 12.5)
            self.assertEqual(cache["rate_limits"]["weekly"]["used_percentage"], 44)
            self.assertNotIn("ignored", json.dumps(cache))


if __name__ == "__main__":
    unittest.main()
