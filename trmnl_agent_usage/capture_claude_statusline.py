#!/usr/bin/env python3
"""Capture sanitized Claude Code statusline rate limits for TRMNL feeds."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


DEFAULT_CACHE_DIR = Path(
    os.environ.get("TRMNL_AGENT_USAGE_CACHE_DIR", Path.home() / ".cache" / "trmnl-agent-usage")
).expanduser()
DEFAULT_TIMEZONE = os.environ.get("TRMNL_AGENT_USAGE_TIMEZONE") or os.environ.get("TZ") or "UTC"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default=str(DEFAULT_CACHE_DIR / "claude-statusline.json"),
    )
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    return parser.parse_args()


def safe_window(window: Any) -> dict[str, Any] | None:
    if not isinstance(window, dict):
        return None
    out: dict[str, Any] = {}
    if isinstance(window.get("used_percentage"), (int, float)):
        out["used_percentage"] = float(window["used_percentage"])
    if isinstance(window.get("resets_at"), (int, float)):
        out["resets_at"] = int(window["resets_at"])
    elif isinstance(window.get("resets_at"), str):
        out["resets_at"] = window["resets_at"]
    return out or None


def atomic_write_private(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=True, sort_keys=True, indent=2)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def main() -> int:
    args = parse_args()
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return 0

    rate_limits = data.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return 0

    captured: dict[str, Any] = {
        "ok": True,
        "captured_at": datetime.now(ZoneInfo(args.timezone)).isoformat(timespec="seconds"),
        "source": "Claude Code statusLine stdin",
        "rate_limits": {},
    }
    for source_key, dest_key in (("five_hour", "five_hour"), ("seven_day", "weekly")):
        window = safe_window(rate_limits.get(source_key))
        if window:
            captured["rate_limits"][dest_key] = window

    if captured["rate_limits"]:
        atomic_write_private(Path(args.output).expanduser(), captured)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
