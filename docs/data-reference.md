# Data Sources and Compatibility

This document describes where dashboard values come from, which fallbacks are allowed, and what is intentionally not
published.

## Published Files

The collector writes:

```text
codex.json
claude.json
index.json
```

Each dashboard feed contains aggregate usage, formatted display values, local timestamps, source status, and sanitized
errors. `index.json` is a small discovery document for the two feeds.

The server allowlists `GET /codex.json`, `GET /claude.json`, `GET /index.json`, and `GET /health`. Directory listing is
disabled. Responses use `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.

## Publication Boundary

The collector may read local CLI metadata and operating-system credentials to call first-party endpoints, but published
feeds must not contain:

- prompts or command history;
- raw JSONL/session records;
- API keys, OAuth tokens, private keys, or auth files;
- local absolute paths or project directory names;
- real webhook URLs;
- raw exception tracebacks or failed command lines.

Project/session details are omitted or reduced to non-identifying aggregate counts. Source failures are converted to
sanitized status fields.

## Token Cost

`Token Cost*` converts observed token quantities using corresponding public API token rates. It is a reference value,
not money known to have been charged or billed. Subscription usage can be included without producing a matching API
invoice.

By default, the collector asks `ccusage` for current prices and uses Claude's `calculate` mode so input, output,
cache-write, and cache-read tokens receive their respective rates. Use cached pricing only when network price lookup is
unavailable:

```sh
trmnl-agent-usage-collect --offline
```

or:

```sh
export TRMNL_AGENT_USAGE_OFFLINE=true
```

The dashboard's `7d` usage and cost are rolling seven-day values, not a fixed calendar week.

## Native Plan Names

The collector does not infer plan multipliers or map subscriptions to marketing labels:

- Codex uses native `planType` from the structured app-server account/rate-limit response.
- Claude Code uses native `subscriptionType` from `claude auth status --json` when exposed, otherwise from valid
  secure OAuth metadata when OAuth usage collection is enabled.
- Availability in `claude auth status --json` varies with CLI version and auth/account state.

Tested Claude Code `2.1.215` states may omit `subscriptionType` from `claude auth status --json`. Reading secure OAuth
metadata remains opt-in with `TRMNL_AGENT_USAGE_ENABLE_CLAUDE_OAUTH_USAGE=true`; expired or HTTP-401-rejected OAuth
credentials are not used as plan evidence. Without either current native value, the plan is `n/a`.

The selected native value is published unchanged in `plan` and included in the default title. Optional title variables
change only display text and do not replace `plan`:

```sh
export TRMNL_AGENT_USAGE_CODEX_TITLE="Codex Usage"
export TRMNL_AGENT_USAGE_CLAUDE_TITLE="Claude Code Usage"
```

## Codex

### Rate limits and credits

The primary source is the supported Codex app-server account/rate-limit method. The collector reads the legacy
`rateLimits` view and the current `rateLimitsByLimitId` map. Buckets are classified by native `limitId` and
`windowDurationMins`:

- 300 minutes is shown as the 5-hour window.
- 10080 minutes is shown as the weekly window.

The collector does not assume that app-server `primary` and `secondary` positions correspond to those labels. When the
response contains multiple named buckets, the general Codex bucket is preferred and another named bucket such as
`Spark` can occupy the second dashboard slot. If the response does not expose a 5-hour bucket, `Weekly all` is promoted
to the first slot; weekly usage is never relabeled as a 5-hour percentage.

Supported reset-credit count and per-credit `expiresAt` values are authoritative when present.

### Account and local scope

Codex account lifetime, peak day, streak, and daily activity come from the supported app-server
`account/usage/read` method. Model mix, local rolling totals, and `Token Cost*` come from ccusage. The templates label
these scopes as `Account` and `Local`; the values are not added together.

### Rolling token totals

The rolling 5-hour token total follows current `ccusage` semantics:

- fork/subagent replay prefixes are discarded;
- matching events copied across session files are counted once;
- cumulative-only records are converted to deltas;
- cached input is separated from non-cached input.

This avoids inflated totals from summing every cumulative `last_token_usage` record.

### Optional private reset-credit fallback

The unofficial ChatGPT reset-credit endpoint is disabled by default:

```sh
export TRMNL_AGENT_USAGE_ENABLE_CODEX_PRIVATE_RESET_CREDITS=true
```

When explicitly enabled, it is used only to fill missing banked-reset expiry. The supported app-server count remains
authoritative. The collector requests an in-memory token from Codex app-server, performs one request, and does not write
the token to feeds. This endpoint is undocumented and may change or stop working without notice.

## Claude Code

### Live OAuth limits

Claude Code statusline percentages update only while a Claude Code session is running. To request the live limits shown
by Claude's first-party service, explicitly enable OAuth usage collection:

```sh
export TRMNL_AGENT_USAGE_ENABLE_CLAUDE_OAUTH_USAGE=true
```

The collector reads the existing credential from macOS Keychain or Claude Code's credential store, holds the access
token in memory, and sends it only to `api.anthropic.com`. Published values are limited to sanitized percentages, reset
timestamps, and scope labels.

The access-token expiry is checked before any request. If the token has expired or the endpoint returns HTTP 401, and
Claude Code has an existing refresh token, the full dashboard shows `Usage auth: run Claude`. The collector only checks
that a refresh token exists; it never sends or writes that token. If no refresh token exists, the dashboard shows
`Usage auth: run /login`. A stale statusline reset may remain as timing context, but stale percentages are never shown
as live limits.

`Usage auth: run Claude` specifically means the **command-line** Claude Code, and only a real request. Three things
that look like they should clear it do not:

- The Claude Code desktop app does not write the stored credential back, so working in it all day leaves the token
  expired.
- `claude auth status --json` does not refresh the token either. It keeps reporting `loggedIn: true` while the stored
  access token is expired, so it is not a usable liveness check for this feed.
- Installing the CLI is not enough; it has to issue an actual request.

Access tokens last roughly eight hours. If nobody runs the CLI within that window, the Claude limit cards fall back to
`n/a` with empty bars even though everything else is healthy.

### Unattended OAuth refresh

For unattended collectors, and for anyone whose day happens in the desktop app, the collector can keep the credential
current by itself. This extends the OAuth usage path, so it needs **both** variables:

```sh
export TRMNL_AGENT_USAGE_ENABLE_CLAUDE_OAUTH_USAGE=true
export TRMNL_AGENT_USAGE_ENABLE_CLAUDE_OAUTH_REFRESH=true
```

Setting only the refresh variable does nothing, because the refresh runs inside the usage path. That combination is not
silently ignored: the feed reports `Claude OAuth refresh ignored without Claude OAuth usage enabled`.

When both are enabled, and only when a stored credential with a refresh token has expired, the collector runs one
minimal `claude --print` prompt against `TRMNL_AGENT_USAGE_CLAUDE_OAUTH_REFRESH_MODEL` (default the current Haiku
model) and then rereads the credential. Claude Code performs its own rotation. As before, the collector only checks
that a refresh token exists; it never sends or writes that token. Calling the OAuth token endpoint directly was
deliberately rejected, because refresh-token rotation outside Claude Code can invalidate a working login.

`TRMNL_AGENT_USAGE_CLAUDE_OAUTH_REFRESH_SKEW_MINUTES` defaults to `0`, meaning the prompt waits for real expiry.
Claude Code rotates the stored token when it has expired, not when it is merely close to expiring, so a pre-expiry
prompt spends a request and changes nothing. Waiting costs no dashboard staleness either, because the collector
refreshes and then reads the limits within the same run.

The option is off by default and costs nothing while the token is healthy. A healthy token skips the prompt entirely,
so the expected cost is roughly three minimal prompts per day.

Those prompts consume real quota, so they are included in the limit percentages the dashboard shows, which come from
Anthropic. They do **not** appear in the local token and cost totals, because `--no-session-persistence` means Claude
Code writes no transcript for `ccusage` to read. The refresh is therefore visible in the limit cards and invisible in
the local usage figures.

With the default skew and cooldown, five safeguards keep that cost bounded, because each prompt is a real request:

- No stored OAuth credential, or one without a refresh token, means no prompt at all. Only Claude Code can rotate that
  credential, so on an API-key-only install a prompt would spend a request and change nothing.
- `TRMNL_AGENT_USAGE_CLAUDE_OAUTH_REFRESH_COOLDOWN_MINUTES` (default `30`) bounds retries through a
  `claude-oauth-refresh.stamp` marker in the cache directory. The marker is written before the prompt runs, so a
  hanging or failing prompt cannot be retried every collection cycle. If the marker cannot be written, the prompt is
  skipped rather than run unbounded.
- After the prompt, the stored expiry is compared with the value from before it. Claude Code prefers a direct API key,
  auth token, third-party provider, or gateway base URL over the stored subscription login, so a prompt can succeed
  while rotating nothing. Those variables are removed from the prompt's environment; everything else is inherited, so
  proxy, mTLS, custom CA, and relocated-config settings a deployment needs still apply. That list cannot be exhaustive,
  and managed enterprise settings can reintroduce such a variable regardless of what the collector passes, which is why
  the expiry comparison rather than the exit code is the actual guarantee. If the expiry did not move, the feed reports
  `Claude OAuth refresh ping did not rotate the stored token`.
- A prompt that ran but still left the feed without limits, whether from a persistent `401` or a failed credential
  reread, is recorded the same way. Otherwise the cooldown alone would let an unproductive paid prompt repeat.
- A confirmed non-rotation is remembered in `claude-oauth-refresh.state`. Repeating a prompt that already failed to
  move that exact expiry cannot help, so no further prompts run for it. The note keeps appearing at no cost, and
  prompts resume automatically once the stored credential changes. Only an *expired* token that failed to rotate is
  recorded this way; declining to rotate a still-valid token is normal and is never treated as stuck.

The prompt is deliberately minimal: `--safe-mode` so local hooks, plugins, skills, and project instructions do not load,
`--no-session-persistence` so it writes no transcript, `--strict-mcp-config` so no MCP servers load, and empty
`--tools` so no tools are enabled. It runs in a fixed `claude-oauth-refresh-cwd` directory inside the cache directory.
`--bare` is intentionally **not** used: it skips keychain reads and never reads OAuth, which is precisely the
credential the prompt exists to rotate. If the `claude` executable is missing or the prompt fails, the feed keeps the
existing `Usage auth: run Claude` recovery note instead of failing the collection.

On a managed installation, administrator settings take precedence over both command-line arguments and the environment
the collector passes, so managed hooks can still run and a managed credential or gateway can still serve the prompt.
The collector cannot prevent that; it detects it, because the stored expiry will not move.

Two more cases are handled so the guarantees above hold in practice:

- A `401` from the usage endpoint is treated as authoritative even when the stored expiry still looks valid, which
  covers a skewed clock, a missing or malformed `expiresAt`, and a server-side revocation. One rotation is attempted
  and the request retried once.
- Attempts are claimed with an exclusive lock, so two collectors sharing a cache directory cannot both spend a prompt
  or race each other's rotation. Every bound above lives in the cache directory, so a caller without one cannot run a
  prompt at all. The writability check rejects a directory occupying the state target and exercises the same directory
  and rename using probe files, deliberately without reading and rewriting the recorded value, which would risk
  restoring a stale one over another collector's write. A future-dated
  cooldown marker from a clock change does not hold the cooldown open.

### Statusline fallback

The repository includes a wrapper that captures only the structured rate-limit fields sent to Claude Code's
`statusLine` command:

```sh
chmod +x scripts/claude_statusline_capture_wrapper.sh
```

Example `settings.json` entry:

```json
{
  "statusLine": {
    "type": "command",
    "command": "sh /absolute/path/to/trmnl-agent-usage-dashboards/scripts/claude_statusline_capture_wrapper.sh",
    "refreshInterval": 60
  }
}
```

Preserve an existing statusline command by setting:

```sh
export TRMNL_AGENT_USAGE_CLAUDE_STATUSLINE_DELEGATE="/absolute/path/to/existing-statusline-command"
```

The wrapper reads only:

- `rate_limits.five_hour.used_percentage`
- `rate_limits.five_hour.resets_at`
- `rate_limits.seven_day.used_percentage`
- `rate_limits.seven_day.resets_at`

It writes `claude-statusline.json` in the configured cache directory. The collector maps `seven_day` to `weekly`.

Snapshots older than `TRMNL_AGENT_USAGE_CLAUDE_STATUSLINE_MAX_AGE_MINUTES` lose their percentage values immediately.
A still-future authoritative reset timestamp may remain as timing context. Live OAuth data supersedes stale statusline
warnings. An old statusline percentage is never presented as current.

This fallback only produces data while the **terminal** Claude Code session is open. `statusLine` commands are executed
to draw the terminal interface, so the desktop app never runs the wrapper and the snapshot simply stops updating. The
modification time of `~/.claude/history.jsonl` is a good check for when the terminal CLI was last used.

That matters because it shares a failure mode with the OAuth path above. Both of Claude's limit-percentage sources are
driven by the terminal CLI, so a setup that only uses the desktop app loses both at once and the Claude limit cards go
to `n/a` with empty bars. Enabling the unattended OAuth refresh removes that dependency; the statusline wrapper remains
useful as a fallback and as a source of reset timing.

## Compatibility Baseline

The collector was integration-tested on 2026-07-20 with:

| Tool | Version |
| --- | --- |
| Codex CLI | `0.144.6` |
| Claude Code | `2.1.215` |
| `ccusage` | `20.0.17` |

Tests cover current Codex multi-bucket and account-usage responses, supported reset-credit expiry, fork/subagent replay,
Codex cached-input aliases, Claude OAuth expiry and limit shapes, per-feed exception isolation, sanitized failure feeds,
and stale statusline behavior.

CLI output and private endpoints can change independently of this repository. When upgrading a CLI, run the complete
test suite and inspect a sanitized live feed before updating the compatibility table.

## Upstream References

- [Codex app-server API](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)
- [Claude Code authentication](https://code.claude.com/docs/en/authentication)
- [Claude Code error reference](https://code.claude.com/docs/en/errors)
- [ccusage guide](https://github.com/ryoppippi/ccusage/blob/main/docs/guide/index.md)
