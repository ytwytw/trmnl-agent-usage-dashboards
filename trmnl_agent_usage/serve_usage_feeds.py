#!/usr/bin/env python3
"""Serve prebuilt TRMNL agent usage JSON feeds."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_CACHE_DIR = Path(
    os.environ.get("TRMNL_AGENT_USAGE_CACHE_DIR", Path.home() / ".cache" / "trmnl-agent-usage")
).expanduser()
ALLOWED_FEED_PATHS = {"/codex.json", "/claude.json", "/index.json"}


class FeedHandler(SimpleHTTPRequestHandler):
    server_version = "trmnl-agent-usage/1.0"

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def guess_type(self, path: str) -> str:
        if path.endswith(".json"):
            return "application/json"
        return super().guess_type(path)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API.
        request_path = urlsplit(self.path).path
        if request_path == "/health":
            payload, status = self.health_payload()
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def send_head(self):  # type: ignore[override]
        request_path = urlsplit(self.path).path
        if request_path not in ALLOWED_FEED_PATHS:
            self.send_error(404, "Unknown feed")
            return
        self.path = request_path
        return super().send_head()

    def list_directory(self, path: str):  # type: ignore[override]
        self.send_error(404, "Directory listing disabled")
        return None

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib API.
        return

    def health_payload(self) -> tuple[dict[str, object], int]:
        root = Path(self.directory)  # type: ignore[attr-defined]
        feeds: dict[str, bool] = {}
        for name in ("codex.json", "claude.json"):
            try:
                data = json.loads((root / name).read_text(encoding="utf-8"))
                feeds[name] = bool(data.get("ok"))
            except Exception:
                feeds[name] = False
        ok = all(feeds.values())
        return {"ok": ok, "feeds": feeds}, 200 if ok else 503


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--directory",
        default=str(DEFAULT_CACHE_DIR),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8787, type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    directory = Path(args.directory).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    handler = lambda *handler_args, **handler_kwargs: FeedHandler(  # noqa: E731
        *handler_args,
        directory=str(directory),
        **handler_kwargs,
    )
    while True:
        try:
            server = ThreadingHTTPServer((args.host, args.port), handler)
            server.serve_forever()
        except OSError as exc:
            print(
                f"trmnl-agent-usage: bind/listen failed on {args.host}:{args.port}: {exc}; retrying in 30s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(30)


if __name__ == "__main__":
    main()
