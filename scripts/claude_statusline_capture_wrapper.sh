#!/bin/sh
# Capture Claude Code statusline rate-limit JSON, then preserve an optional existing statusline command.

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
capture="${TRMNL_AGENT_USAGE_CAPTURE_SCRIPT:-$script_dir/../trmnl_agent_usage/capture_claude_statusline.py}"
cache_root="${TRMNL_AGENT_USAGE_CACHE_DIR:-$HOME/.cache/trmnl-agent-usage}"
cache="${TRMNL_AGENT_USAGE_CLAUDE_STATUSLINE_CACHE:-$cache_root/claude-statusline.json}"
timezone="${TRMNL_AGENT_USAGE_TIMEZONE:-${TZ:-UTC}}"
delegate="${TRMNL_AGENT_USAGE_CLAUDE_STATUSLINE_DELEGATE:-}"
python_bin="${PYTHON:-python3}"
umask 077
tmp=$(mktemp "${TMPDIR:-/tmp}/claude-statusline.XXXXXX") || {
  if [ -n "$delegate" ] && [ -f "$delegate" ]; then
    exec /bin/sh "$delegate"
  fi
  exit 0
}
trap 'rm -f "$tmp"' EXIT HUP INT TERM
cat > "$tmp"

if [ -f "$capture" ]; then
  "$python_bin" "$capture" --output "$cache" --timezone "$timezone" < "$tmp" >/dev/null 2>&1
else
  "$python_bin" -m trmnl_agent_usage.capture_claude_statusline --output "$cache" --timezone "$timezone" < "$tmp" >/dev/null 2>&1
fi

if [ -n "$delegate" ] && [ -f "$delegate" ]; then
  /bin/sh "$delegate" < "$tmp"
fi
