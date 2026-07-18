from __future__ import annotations

import importlib.util
import struct
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_public_repo.py"
SPEC = importlib.util.spec_from_file_location("audit_public_repo", SCRIPT)
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


class PublicRepositoryAuditTests(unittest.TestCase):
    def test_flags_private_network_and_personal_email(self) -> None:
        private_address = "192" + ".168.10.20"
        personal_email = "person" + "@private.invalid"
        findings = AUDIT.scan_text("fixture", f"host={private_address}\nemail={personal_email}\n")
        self.assertTrue(any("private IPv4" in finding for finding in findings))
        self.assertTrue(any("personal email" in finding for finding in findings))

    def test_allows_reserved_examples_and_github_noreply(self) -> None:
        text = "\n".join(
            (
                "http://feed-host.example:8787/health",
                "https://trmnl.com/api/custom_plugins/REPLACE_ME",
                "2984062+public-handle@users.noreply.github.com",
                "/Users/example/project",
            )
        )
        self.assertEqual([], AUDIT.scan_text("fixture", text))

    def test_rejects_png_text_metadata(self) -> None:
        signature = b"\x89PNG\r\n\x1a\n"
        text_chunk = struct.pack(">I", 0) + b"tEXt" + b"\x00\x00\x00\x00"
        end_chunk = struct.pack(">I", 0) + b"IEND" + b"\x00\x00\x00\x00"
        findings = AUDIT.scan_png("fixture.png", signature + text_chunk + end_chunk)
        self.assertEqual(["fixture.png: embedded PNG metadata chunk tEXt"], findings)


if __name__ == "__main__":
    unittest.main()
