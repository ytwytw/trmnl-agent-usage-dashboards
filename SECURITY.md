# Security Policy

## Reporting a Vulnerability

Please do not open a public issue with secrets, tokens, webhook URLs, prompts, raw logs, or local file paths.

Open a private GitHub security advisory for this repository, or contact the maintainer through GitHub with a sanitized
summary and reproduction steps. Include the affected version or commit, the command or workflow involved, and the type of
data that could be exposed.

## Data Handling Scope

This project is designed to publish aggregate dashboard metrics only. It should not publish prompts, raw JSONL lines,
API keys, bearer tokens, private keys, local file paths, command history, or real TRMNL webhook URLs.

The optional Codex banked-reset expiry lookup is disabled by default. If enabled, it uses an auth token in memory for one
request to an undocumented endpoint and should be treated as best-effort, user-opt-in behavior.

The optional Claude OAuth paths are also disabled by default. When enabled, the collector reads Claude Code's stored
credential from the operating-system credential store, holds the access token in memory, and sends it only to
`api.anthropic.com`. It checks that a refresh token exists but never transmits or writes one. The separate refresh
option additionally runs the official Claude Code CLI to make one minimal request, which consumes real Claude usage; it
is opt-in and off unless both variables are set.

## Supported Versions

Only the latest commit on `main` is supported. Fixes are best-effort with no service-level commitment.

## Public Repository Hygiene

Public fixtures and screenshots must be synthetic, schema-valid, and visually representative. Do not commit live feeds,
raw logs, screenshots from private dashboards, local service files, or copied command output.

The public tree and reachable git history must not contain:

- personal email addresses or non-public real names;
- private, tailnet, or home-network addresses and hostnames;
- absolute user home paths or project names from a real workstation;
- webhook URLs, bearer tokens, API keys, private keys, or credential files;
- real usage timestamps or account-specific measurements presented as fixtures;
- embedded PNG text, EXIF, comments, or location metadata.

The maintainer's public GitHub handle, repository URL, and GitHub-provided noreply commit address are intentional public
project identifiers. Reserved domains, documentation-only addresses, and obvious replacement tokens may be used in
examples.

Before publishing changes, run:

```sh
python scripts/audit_public_repo.py --history
python -m unittest discover -s tests
git diff --check
```

Automated checks reduce accidental disclosure but cannot infer every contextual identifier. Review fixtures, images,
documentation, diffs, and commit metadata before pushing.
