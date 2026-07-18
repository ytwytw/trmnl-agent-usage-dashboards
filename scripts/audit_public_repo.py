#!/usr/bin/env python3
"""Fail when a public checkout contains common private-data or secret markers."""

from __future__ import annotations

import argparse
import re
import struct
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_BLOB_BYTES = 5 * 1024 * 1024
ALLOWED_EMAIL_DOMAINS = {"users.noreply.github.com", "example.com", "example.org", "example.net"}
PNG_PRIVATE_CHUNKS = {b"eXIf", b"iTXt", b"tEXt", b"zTXt"}

TEXT_PATTERNS = (
    ("absolute macOS home path", re.compile(r"/" + r"Users/(?!example(?:/|\b))[^/\s]+/")),
    ("absolute Linux home path", re.compile(r"/" + r"home/(?!example(?:/|\b))[^/\s]+/")),
    ("private IPv4 address", re.compile(r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b")),
    ("tailnet/CGNAT address", re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2}\b")),
    ("private key block", re.compile(r"BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")),
    ("OpenAI-style token", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}\b")),
    ("Anthropic token", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("bearer credential", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    (
        "live-looking TRMNL webhook URL",
        re.compile(
            r"https://trmnl\.com/api/custom_plugins/(?!REPLACE_ME\b|replace-with-)[A-Za-z0-9_-]{16,}"
        ),
    ),
)

EMAIL_PATTERN = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![A-Za-z0-9.-])")


def run_git(*args: str, text: bool = True) -> str | bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=text)


def public_files() -> list[Path]:
    output = run_git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    assert isinstance(output, str)
    return [ROOT / item for item in output.split("\0") if item]


def scan_text(label: str, text: str) -> list[str]:
    findings: list[str] = []
    for description, pattern in TEXT_PATTERNS:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            findings.append(f"{label}:{line}: {description}")

    for match in EMAIL_PATTERN.finditer(text):
        domain = match.group(2).lower()
        if domain not in ALLOWED_EMAIL_DOMAINS:
            line = text.count("\n", 0, match.start()) + 1
            findings.append(f"{label}:{line}: personal email address")
    return findings


def scan_png(label: str, data: bytes) -> list[str]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return []
    findings: list[str] = []
    offset = 8
    while offset + 12 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data):
            findings.append(f"{label}: malformed PNG chunk")
            break
        if chunk_type in PNG_PRIVATE_CHUNKS:
            findings.append(f"{label}: embedded PNG metadata chunk {chunk_type.decode('ascii')}")
        offset = end
        if chunk_type == b"IEND":
            break
    return findings


def scan_blob(label: str, data: bytes) -> list[str]:
    findings = scan_png(label, data)
    if b"\0" not in data[:8192]:
        findings.extend(scan_text(label, data.decode("utf-8", errors="replace")))
    return findings


def scan_worktree() -> list[str]:
    findings: list[str] = []
    for path in public_files():
        data = path.read_bytes()
        if len(data) > MAX_BLOB_BYTES:
            findings.append(f"{path.relative_to(ROOT)}: file exceeds {MAX_BLOB_BYTES} byte audit limit")
            continue
        findings.extend(scan_blob(str(path.relative_to(ROOT)), data))
    return findings


def history_blob_ids() -> list[tuple[str, str]]:
    output = run_git("rev-list", "--objects", "--all")
    assert isinstance(output, str)
    objects: list[tuple[str, str]] = []
    for line in output.splitlines():
        object_id, separator, path = line.partition(" ")
        if separator and path:
            objects.append((object_id, path))
    return objects


def scan_history() -> list[str]:
    findings: list[str] = []
    seen: set[str] = set()
    for object_id, path in history_blob_ids():
        if object_id in seen:
            continue
        seen.add(object_id)
        object_type = run_git("cat-file", "-t", object_id)
        assert isinstance(object_type, str)
        if object_type.strip() != "blob":
            continue
        size = int(str(run_git("cat-file", "-s", object_id)).strip())
        if size > MAX_BLOB_BYTES:
            findings.append(f"history:{path}@{object_id[:12]}: blob exceeds audit limit")
            continue
        data = run_git("cat-file", "blob", object_id, text=False)
        assert isinstance(data, bytes)
        findings.extend(scan_blob(f"history:{path}@{object_id[:12]}", data))

    email_output = run_git("log", "--all", "--format=%ae%n%ce")
    assert isinstance(email_output, str)
    for email in sorted(set(email_output.splitlines())):
        match = EMAIL_PATTERN.fullmatch(email.strip())
        if match and match.group(2).lower() not in ALLOWED_EMAIL_DOMAINS:
            findings.append("history: commit metadata contains a personal email address")
    return findings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", action="store_true", help="also scan reachable git blobs and commit emails")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    findings = scan_worktree()
    if args.history:
        findings.extend(scan_history())

    unique_findings = list(dict.fromkeys(findings))
    if unique_findings:
        print("Public repository audit failed:", file=sys.stderr)
        for finding in unique_findings:
            print(f"- {finding}", file=sys.stderr)
        return 1

    scope = "publishable files and reachable history" if args.history else "publishable working-tree files"
    print(f"Public repository audit passed: {scope}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
