#!/usr/bin/env python3
"""Push compact TRMNL Private Plugin webhook payloads."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .collect_usage_feeds import DEFAULT_CACHE_DIR, safe_exception


DEFAULT_MAX_BYTES = 2000


def pick(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: source.get(key) for key in keys if source.get(key) is not None}


def compact_feed(feed: dict[str, Any]) -> dict[str, Any]:
    compact = pick(
        feed,
        ("ok", "kind", "title", "local_datetime", "refresh", "cost_basis", "source", "plan", "activity_scope"),
    )
    compact["errors"] = (feed.get("errors") or [])[:1]
    compact["notes"] = (feed.get("notes") or [])[:1]

    limits = feed.get("limits") if isinstance(feed.get("limits"), dict) else {}
    compact["limits"] = {
        key: pick(value, ("label", "used", "used_percent", "window", "reset", "reset_full"))
        for key, value in limits.items()
        if key in {"primary", "weekly", "scoped"} and isinstance(value, dict)
    }

    usage = feed.get("usage") if isinstance(feed.get("usage"), dict) else {}
    compact["usage"] = {}
    for key in ("five_hour", "today", "week", "month", "all"):
        value = usage.get(key) if isinstance(usage.get(key), dict) else {}
        keys = (
            ("label", "total", "cost", "cache_hit", "sessions", "input", "output", "cache_read", "reasoning")
            if key == "five_hour"
            else ("label", "total", "cost")
        )
        compact["usage"][key] = pick(value, keys)

    sessions = feed.get("sessions") if isinstance(feed.get("sessions"), dict) else {}
    compact["sessions"] = pick(sessions, ("today", "week", "all"))
    compact["models"] = [
        pick(item, ("name", "tokens", "pct"))
        for item in (feed.get("models") or [])[:5]
        if isinstance(item, dict)
    ]
    compact["daily_bars"] = [
        pick(item, ("day", "tokens", "pct"))
        for item in (feed.get("daily_bars") or [])[:7]
        if isinstance(item, dict)
    ]

    if feed.get("kind") == "codex":
        credits = feed.get("credits") if isinstance(feed.get("credits"), dict) else {}
        compact["credits"] = pick(credits, ("has_credits", "balance", "banked_reset_summary"))
        account_usage = feed.get("account_usage") if isinstance(feed.get("account_usage"), dict) else {}
        compact["account_usage"] = pick(account_usage, ("lifetime", "peak_day", "current_streak", "longest_streak"))
    else:
        block = feed.get("block") if isinstance(feed.get("block"), dict) else {}
        compact["block"] = pick(
            block,
            ("started", "reset", "remaining", "burn_tokens_min", "burn_cost_hr", "projected_tokens", "projected_cost"),
        )
        auth = feed.get("auth") if isinstance(feed.get("auth"), dict) else {}
        compact["auth"] = pick(auth, ("usage",))
    return compact


def webhook_payload(feed: dict[str, Any]) -> bytes:
    payload = {"merge_variables": {"source_1": compact_feed(feed)}}
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def post_payload(name: str, url: str, body: bytes, timeout: float) -> tuple[int, str]:
    try:
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "trmnl-agent-usage/1"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(1024)
            return response.status, f"{name}: posted {len(body)} bytes status={response.status}"
    except urllib.error.HTTPError as exc:
        return exc.code, f"{name}: webhook HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - do not include the private webhook URL.
        return 0, f"{name}: webhook failed {safe_exception(exc)}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", default=str(DEFAULT_CACHE_DIR))
    parser.add_argument("--codex-webhook-url", default=os.environ.get("TRMNL_AGENT_USAGE_CODEX_WEBHOOK_URL"))
    parser.add_argument("--claude-webhook-url", default=os.environ.get("TRMNL_AGENT_USAGE_CLAUDE_WEBHOOK_URL"))
    parser.add_argument("--max-bytes", default=DEFAULT_MAX_BYTES, type=int)
    parser.add_argument("--timeout", default=20.0, type=float)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    directory = Path(args.directory).expanduser()
    targets = {
        "codex": (directory / "codex.json", args.codex_webhook_url),
        "claude": (directory / "claude.json", args.claude_webhook_url),
    }
    exit_code = 0

    for name, (path, webhook_url) in targets.items():
        if not webhook_url:
            print(f"{name}: webhook URL not configured", file=sys.stderr)
            exit_code = 2
            continue
        try:
            feed = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - path is local and expected.
            print(f"{name}: feed unreadable {safe_exception(exc)}", file=sys.stderr)
            exit_code = 2
            continue

        body = webhook_payload(feed)
        if len(body) > args.max_bytes:
            print(f"{name}: payload {len(body)} bytes exceeds max {args.max_bytes}", file=sys.stderr)
            exit_code = 2
            continue
        if args.dry_run:
            print(f"{name}: dry-run payload {len(body)} bytes")
            continue

        status, message = post_payload(name, webhook_url, body, args.timeout)
        print(message)
        if status < 200 or status >= 300:
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
