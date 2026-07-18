# Deployment Guide

This guide deploys the dashboard collector and connects it to an existing Terminus/BYOS or TRMNL SaaS installation.
It does not replace the upstream instructions for deploying Terminus itself.

## Choose a Path

| Target | Data delivery | Inbound access to collector host | Long-running process |
| --- | --- | --- | --- |
| Self-hosted Terminus/BYOS | Terminus Poll Extension reads local JSON | Trusted LAN access to port 8787 | Collector schedule plus feed server |
| TRMNL SaaS | Collector pushes Private Plugin webhooks | None | Collector and push schedule |

Run the collector on the machine that owns the Codex and Claude Code metadata and credentials. Moving only the
collector into a remote container normally prevents it from reading the local CLI data and operating-system credential
store. Terminus itself may run on another host.

## Common Setup

Install the project as shown in the [README Quick Start](../README.md#quick-start), then set the runtime environment
outside the repository:

```sh
export TRMNL_AGENT_USAGE_CACHE_DIR="$HOME/.cache/trmnl-agent-usage"
export TRMNL_AGENT_USAGE_TIMEZONE="UTC"
export TRMNL_AGENT_USAGE_CLAUDE_STATUSLINE_MAX_AGE_MINUTES=30
```

Use the IANA time zone expected on the dashboard. All displayed local timestamps are generated with this setting.

Build and inspect the initial feeds:

```sh
trmnl-agent-usage-collect
python -m json.tool "$TRMNL_AGENT_USAGE_CACHE_DIR/codex.json"
python -m json.tool "$TRMNL_AGENT_USAGE_CACHE_DIR/claude.json"
```

An uncaught provider failure produces a sanitized `ok: false` feed without exposing a traceback, command, token, or
local path. Optional-source failures may instead leave only the affected fields as `n/a`; review sanitized `errors`,
`warnings`, and `notes` before relying on the dashboard.

## Self-Hosted Terminus/BYOS

### 1. Serve the feeds

The server defaults to loopback. Bind it to a trusted interface when Terminus runs on another host:

```sh
trmnl-agent-usage-serve \
  --directory "$TRMNL_AGENT_USAGE_CACHE_DIR" \
  --host 0.0.0.0 \
  --port 8787
```

`0.0.0.0` listens on every host interface. Restrict port 8787 with host firewall rules to the Terminus host or trusted
LAN. The server has no authentication or TLS and must not be published through a public router, tunnel, or reverse
proxy without adding an authentication layer.

Verify locally:

```sh
curl --fail http://127.0.0.1:8787/health
curl --fail http://127.0.0.1:8787/codex.json
curl --fail http://127.0.0.1:8787/claude.json
```

Then verify from the Terminus host using the collector host's LAN name or address. The reserved hostname below is only
an example and must be replaced:

```sh
curl --fail http://feed-host.example:8787/health
```

### 2. Create two Poll Extensions

The current Terminus workflow uses Extensions, Exchanges, Screens, and Playlists:

1. Open **Extensions** and create an Extension.
2. Set **Kind** to **Poll**.
3. Select the target model in **Build Matrix**.
4. Replace the template editor content with the complete chosen file from `templates/`. Do not nest the 800x480
   dashboard inside a second padded layout.
5. Save the Extension, open **Exchanges**, and add one GET Exchange URL.
6. Set the Extension schedule to every 15 minutes.
7. Build or preview the Extension and confirm that its Exchange **Data** is populated without **Errors**.
8. Add the generated Screen for the correct model to the device Playlist.

Create one Extension per feed:

```text
Codex: http://feed-host.example:8787/codex.json
Claude: http://feed-host.example:8787/claude.json
```

Terminus exposes each Exchange response as `source_1`, which is the variable consumed by the shipped templates. The
extension schedule controls source polling and screen generation. The device's refresh setting separately controls how
often it asks Terminus for the next Playlist screen.

Terminus is beta software and its UI may change. Consult the upstream
[Terminus Extensions documentation](https://github.com/usetrmnl/terminus/blob/main/doc/extensions.adoc) when labels or
navigation differ.

### 3. Keep both processes running

Schedule `trmnl-agent-usage-collect` every 15 minutes and keep `trmnl-agent-usage-serve` under the operating system's
user-level service manager. Run both as the same user that owns the Codex and Claude Code data.

Minimal cron example for collection:

```cron
*/15 * * * * TRMNL_AGENT_USAGE_TIMEZONE=UTC TRMNL_AGENT_USAGE_CACHE_DIR="$HOME/.cache/trmnl-agent-usage" /absolute/path/to/.venv/bin/trmnl-agent-usage-collect >>"$HOME/.cache/trmnl-agent-usage/collector.log" 2>&1
```

A scheduled collector like this one is exactly the case where the Claude limit cards decay to `n/a`, because nothing
runs the command-line Claude Code to keep its credential current. Add both
`TRMNL_AGENT_USAGE_ENABLE_CLAUDE_OAUTH_USAGE=true` and `TRMNL_AGENT_USAGE_ENABLE_CLAUDE_OAUTH_REFRESH=true` to the same
environment to let the collector handle that itself; the refresh extends the usage path and does nothing on its own.
See [Live OAuth limits](data-reference.md#live-oauth-limits) for what it does and what it costs.

Use `launchd` on macOS or a user-level `systemd` service on Linux to supervise the feed server. Service definitions must
use absolute executable/cache paths and must remain outside this repository if they contain local usernames or paths.
After a reboot, verify both `/health` and the most recent `local_datetime` in each feed.

## TRMNL SaaS Webhooks

TRMNL cloud workers cannot poll a LAN-only feed URL. Use outbound Webhook delivery unless the feed server is already
published through a separately secured internet endpoint.

### 1. Create the Private Plugins

Create two TRMNL Private Plugins:

```text
Codex Usage       Strategy: Webhook
Claude Code Usage Strategy: Webhook
```

Save each plugin, open its markup editor, paste the chosen template, and set **Remove bleed margin?** to **Yes** for the
full-size 800x480 templates. Keep each generated webhook URL outside git.

The push command wraps each compact feed as:

```json
{
  "merge_variables": {
    "source_1": {}
  }
}
```

That shape matches the first Liquid assignment in the templates.

### 2. Configure and test delivery

```sh
export TRMNL_AGENT_USAGE_CODEX_WEBHOOK_URL="https://trmnl.com/api/custom_plugins/REPLACE_ME"
export TRMNL_AGENT_USAGE_CLAUDE_WEBHOOK_URL="https://trmnl.com/api/custom_plugins/REPLACE_ME"
trmnl-agent-usage-push --dry-run
trmnl-agent-usage-push
```

The sender removes unused fields and rejects payloads larger than 2 KB by default. A dry run prints only the payload
size; it does not print the webhook URL.

### 3. Schedule collection and push

Run collection before push in the same scheduled job:

```sh
#!/bin/sh
set -eu
/absolute/path/to/.venv/bin/trmnl-agent-usage-collect
/absolute/path/to/.venv/bin/trmnl-agent-usage-push
```

Store this wrapper and its environment outside the repository, make it executable, and run it every 15 minutes. Current
TRMNL documentation describes a normal 2 KB webhook limit and 12 webhook requests per hour per Private Plugin. A
15-minute cycle sends four requests per hour per plugin and stays below that limit. Account capabilities may change;
check the current [Private Plugin guide](https://help.trmnl.com/en/articles/9510536-private-plugins) and
[Webhook documentation](https://docs.trmnl.com/go/private-plugins/webhooks).

## Refresh Model

Do not use one refresh value to describe the whole system:

| Layer | Recommended baseline | Effect |
| --- | --- | --- |
| Collector | 15 minutes | Rebuilds the JSON source data |
| Terminus Extension | 15 minutes | Polls JSON and renders a new Screen |
| Device | User-selected | Requests and rotates Playlist screens |
| TRMNL SaaS | Account/plugin dependent | Renders and delivers hosted screens |

A five-minute device interval can rotate screens every five minutes while the usage data itself remains on a
15-minute collection cadence.

## Upgrade

From the repository checkout:

```sh
git pull --ff-only
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests
python scripts/audit_public_repo.py
trmnl-agent-usage-collect
```

Restart the feed server or scheduled service so it uses the updated environment. Re-paste the selected Liquid template
into Terminus or TRMNL only when that template changed. Verify `/health`, both feed timestamps, one rendered Codex
screen, and one rendered Claude screen.

## Troubleshooting

### `/health` is not successful

- Confirm `codex.json` and `claude.json` exist in the server's `--directory`.
- Run the collector manually and inspect each feed's `ok` and sanitized `errors` fields.
- Confirm the service user is the same user that owns the CLI data.

### Terminus Exchange has no data

- Fetch the URL from the Terminus host, not only from the collector host.
- Check host firewall rules and use the collector's reachable LAN address.
- Inspect the Exchange **Errors** and **Data** tabs, then force a build.
- Confirm the template reads `source_1`.

### TRMNL webhook does not render

- Run `trmnl-agent-usage-push --dry-run` and confirm both payloads are below the configured limit.
- Confirm both webhook variables are available to the scheduled process.
- Use the Private Plugin debug logs and force-refresh controls.
- Confirm the template uses `source_1` for Webhook payloads.

### Dashboard values are stale or `n/a`

- Compare `local_datetime` with the collector schedule.
- Run the relevant CLI manually and update it if its structured output has changed.
- Review [Data sources and compatibility](data-reference.md), especially Claude OAuth/statusline freshness.
- Do not substitute guessed subscription plans or rate-limit windows for missing native values.

### Only the Claude limit cards are `n/a`, everything else is healthy

Both Claude limit-percentage sources are maintained by the command-line Claude Code, so they go stale together on a
machine where nobody runs it, including one where the work happens in the Claude Code desktop app. Check the stored
credential expiry and the statusline snapshot rather than the collector, feed transport, template, or device:

```sh
# When was the terminal CLI last used?
ls -l ~/.claude/history.jsonl

# How old is the statusline snapshot?
ls -l "${TRMNL_AGENT_USAGE_CACHE_DIR:-$HOME/.cache/trmnl-agent-usage}/claude-statusline.json"
```

`claude auth status --json` is not a useful check here; it reports `loggedIn: true` even while the stored access token
is expired. The feed's `notes` are more reliable, and report the expired token and the statusline age directly.

Fix it either by running the command-line Claude Code once, or, for an unattended collector, by enabling
`TRMNL_AGENT_USAGE_ENABLE_CLAUDE_OAUTH_USAGE=true` and `TRMNL_AGENT_USAGE_ENABLE_CLAUDE_OAUTH_REFRESH=true` so the
collector refreshes the credential itself. See [Live OAuth limits](data-reference.md#live-oauth-limits).
