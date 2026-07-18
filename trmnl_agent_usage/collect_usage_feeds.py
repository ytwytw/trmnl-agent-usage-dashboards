#!/usr/bin/env python3
"""Build sanitized local usage feeds for TRMNL dashboards.

The collector intentionally emits aggregate usage, rate-limit, and health
fields only. It does not copy prompts, credentials, raw JSONL lines, or auth
material into the published feed directory.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:  # POSIX only; the claim degrades to a no-op elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms.
    fcntl = None


DEFAULT_CACHE_DIR = Path(
    os.environ.get("TRMNL_AGENT_USAGE_CACHE_DIR", Path.home() / ".cache" / "trmnl-agent-usage")
).expanduser()
DEFAULT_TIMEZONE = os.environ.get("TRMNL_AGENT_USAGE_TIMEZONE") or os.environ.get("TZ") or "UTC"
DEFAULT_CODEX_TITLE = os.environ.get("TRMNL_AGENT_USAGE_CODEX_TITLE")
DEFAULT_CLAUDE_TITLE = os.environ.get("TRMNL_AGENT_USAGE_CLAUDE_TITLE")
CODEX_FIVE_HOUR_WINDOW_MINUTES = 300
CODEX_WEEKLY_WINDOW_MINUTES = 10080
CLAUDE_OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


DEFAULT_CLAUDE_STATUSLINE_MAX_AGE_MINUTES = env_int(
    "TRMNL_AGENT_USAGE_CLAUDE_STATUSLINE_MAX_AGE_MINUTES",
    30,
)
# Claude Code rotates the stored token when it is expired, not when it is
# merely close to expiring. A pre-expiry prompt therefore spends a request and
# changes nothing, so the default waits for real expiry. The collector refreshes
# and then reads usage within the same run, so this costs no dashboard staleness.
DEFAULT_CLAUDE_OAUTH_REFRESH_SKEW_MINUTES = env_int(
    "TRMNL_AGENT_USAGE_CLAUDE_OAUTH_REFRESH_SKEW_MINUTES",
    0,
)
DEFAULT_CLAUDE_OAUTH_REFRESH_COOLDOWN_MINUTES = env_int(
    "TRMNL_AGENT_USAGE_CLAUDE_OAUTH_REFRESH_COOLDOWN_MINUTES",
    30,
)
DEFAULT_CLAUDE_OAUTH_REFRESH_MODEL = (
    os.environ.get("TRMNL_AGENT_USAGE_CLAUDE_OAUTH_REFRESH_MODEL") or "claude-haiku-4-5-20251001"
)
CLAUDE_OAUTH_REFRESH_STAMP_NAME = "claude-oauth-refresh.stamp"
CLAUDE_OAUTH_REFRESH_WORKDIR_NAME = "claude-oauth-refresh-cwd"
CLAUDE_OAUTH_REFRESH_STATE_NAME = "claude-oauth-refresh.state"
CLAUDE_OAUTH_REFRESH_NO_ROTATION_NOTE = "Claude OAuth refresh ping did not rotate the stored token"
# Variables that would route the prompt to a credential other than the stored
# subscription login. An allowlist was tried here and rejected: it broke
# supported deployments that need proxy, mTLS, custom CA, or relocated-config
# variables, and it still could not guarantee the routing, because managed
# enterprise settings can inject variables regardless. The actual guarantee is
# the post-prompt check that the stored expiry moved.
CLAUDE_REFRESH_PING_ENV_DENYLIST = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_MANTLE",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    }
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_CACHE_DIR),
    )
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--codex-root", default=str(Path.home() / ".codex"))
    parser.add_argument("--claude-root", default=str(Path.home() / ".claude"))
    parser.add_argument(
        "--claude-statusline-cache",
        default=str(DEFAULT_CACHE_DIR / "claude-statusline.json"),
    )
    parser.add_argument(
        "--claude-statusline-max-age-minutes",
        default=DEFAULT_CLAUDE_STATUSLINE_MAX_AGE_MINUTES,
        type=int,
    )
    parser.add_argument("--ccusage-bin", default=os.environ.get("CCUSAGE_BIN"))
    parser.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN"))
    parser.add_argument("--claude-bin", default=os.environ.get("CLAUDE_BIN"))
    parser.add_argument("--codex-title", default=DEFAULT_CODEX_TITLE)
    parser.add_argument("--claude-title", default=DEFAULT_CLAUDE_TITLE)
    parser.add_argument(
        "--enable-codex-private-reset-credits",
        action="store_true",
        default=env_flag("TRMNL_AGENT_USAGE_ENABLE_CODEX_PRIVATE_RESET_CREDITS"),
        help=(
            "Opt in to the unofficial ChatGPT reset-credit endpoint. "
            "The collector reads the Codex app-server token in memory only and does not write it to feeds."
        ),
    )
    parser.add_argument(
        "--enable-claude-oauth-usage",
        action="store_true",
        default=env_flag("TRMNL_AGENT_USAGE_ENABLE_CLAUDE_OAUTH_USAGE"),
        help=(
            "Opt in to Claude Code's first-party OAuth usage endpoint. "
            "The access token stays in memory and is never written to feeds."
        ),
    )
    parser.add_argument(
        "--enable-claude-oauth-refresh",
        action="store_true",
        default=env_flag("TRMNL_AGENT_USAGE_ENABLE_CLAUDE_OAUTH_REFRESH"),
        help=(
            "Requires --enable-claude-oauth-usage. Lets the collector run a minimal headless "
            "Claude Code prompt when the stored OAuth access token is expired or nearly expired, "
            "so Claude Code rotates its own token. The collector only checks that a refresh token "
            "exists; it never sends or writes that token."
        ),
    )
    pricing_mode = parser.add_mutually_exclusive_group()
    pricing_mode.add_argument(
        "--offline",
        dest="offline",
        action="store_true",
        help="Use ccusage's bundled pricing cache instead of fetching current model prices.",
    )
    pricing_mode.add_argument(
        "--no-offline",
        dest="offline",
        action="store_false",
        help="Fetch current model prices before calculating API-equivalent cost estimates (default).",
    )
    parser.set_defaults(offline=env_flag("TRMNL_AGENT_USAGE_OFFLINE", False))
    return parser.parse_args()


def ccusage_path(explicit: str | None) -> str:
    candidates = [
        explicit,
        shutil.which("ccusage"),
        "/opt/homebrew/bin/ccusage",
        "/usr/local/bin/ccusage",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise FileNotFoundError("ccusage executable not found")


def optional_executable(explicit: str | None, name: str) -> str | None:
    candidates = [explicit, shutil.which(name), f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def usage_title(product: str, plan: Any, override: str | None = None) -> str:
    custom = str(override or "").strip()
    if custom:
        return custom
    native_plan = str(plan or "").strip()
    return f"{product} {native_plan} Usage" if native_plan else f"{product} Usage"


def read_claude_subscription_type(claude_bin: str | None, notes: list[str]) -> str | None:
    if not claude_bin:
        notes.append("Claude Code CLI not found; subscription type unavailable")
        return None
    try:
        result = subprocess.run(
            [claude_bin, "auth", "status", "--json"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
        payload = json.loads(result.stdout)
    except Exception as exc:  # noqa: BLE001 - subscription metadata is optional.
        notes.append(f"Claude subscription read failed: {safe_exception(exc)}")
        return None
    subscription_type = payload.get("subscriptionType") if isinstance(payload, dict) else None
    if not isinstance(subscription_type, str) or not subscription_type.strip():
        notes.append("Claude auth status did not expose subscriptionType")
        return None
    notes.append("Claude subscription from claude auth status")
    return subscription_type.strip()


def normalize_claude_oauth_usage(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}

    normalized: dict[str, Any] = {}
    for source_key, dest_key in (("five_hour", "five_hour"), ("seven_day", "weekly")):
        source = payload.get(source_key)
        if not isinstance(source, dict):
            continue
        normalized[dest_key] = {
            "used_percentage": source.get("utilization"),
            "resets_at": source.get("resets_at"),
        }

    scoped: list[dict[str, Any]] = []
    for item in payload.get("limits") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        window = {
            "used_percentage": item.get("percent"),
            "resets_at": item.get("resets_at"),
        }
        if kind == "session":
            normalized["five_hour"] = window
        elif kind == "weekly_all":
            normalized["weekly"] = window
        elif kind == "weekly_scoped":
            scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
            model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
            label = model.get("display_name") or scope.get("surface") or "Scoped"
            scoped.append(
                {
                    **window,
                    "label": str(label),
                    "active": bool(item.get("is_active")),
                }
            )

    if scoped:
        normalized["scoped"] = max(
            scoped,
            key=lambda item: (bool(item.get("active")), float(item.get("used_percentage") or 0)),
        )
    return normalized


def read_claude_oauth_credentials(claude_root: Path, notes: list[str]) -> dict[str, Any] | None:
    raw = ""
    security_bin = shutil.which("security")
    if security_bin:
        try:
            result = subprocess.run(
                [security_bin, "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                raw = result.stdout
        except Exception:
            raw = ""

    if not raw:
        try:
            raw = (claude_root / ".credentials.json").read_text(encoding="utf-8")
        except OSError:
            notes.append("Claude OAuth usage unavailable: secure credentials not found")
            return None

    try:
        credentials = json.loads(raw)
    except json.JSONDecodeError:
        notes.append("Claude OAuth usage unavailable: credentials unreadable")
        return None
    oauth = credentials.get("claudeAiOauth") if isinstance(credentials, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token:
        notes.append("Claude OAuth usage unavailable: access token missing")
        return None
    return oauth


def claude_oauth_expiry_epoch(oauth: dict[str, Any] | None) -> float:
    if not isinstance(oauth, dict):
        return 0.0
    try:
        expires_at = float(oauth.get("expiresAt") or 0)
    except (TypeError, ValueError):
        return 0.0
    # NaN and infinity would poison every later comparison, including the one
    # deciding whether a rotation actually happened.
    if not math.isfinite(expires_at):
        return 0.0
    if expires_at > 10_000_000_000:
        expires_at /= 1000
    return expires_at


def claude_oauth_refresh_is_useful(
    oauth: dict[str, Any] | None,
    expires_at: float,
    skew_minutes: int,
    now: float | None = None,
    ignore_expiry: bool = False,
) -> bool:
    """Whether a refresh prompt can actually accomplish anything.

    Only Claude Code can rotate the stored credential, and only when one
    already exists with a refresh token. Without that, the prompt would be a
    real request that changes nothing, repeated on every cooldown expiry.

    `ignore_expiry` is for the case where the service itself rejected the
    credential. That verdict outranks the local expiry field, which may be
    missing, malformed, or read against a skewed clock.
    """
    if not isinstance(oauth, dict):
        return False
    refresh_token = oauth.get("refreshToken")
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        return False
    if ignore_expiry:
        return True
    if not expires_at:
        return False
    return expires_at - skew_minutes * 60 <= (time.time() if now is None else now)


def read_claude_refresh_non_rotating_expiry(state: Path | None) -> float | None:
    if state is None:
        return None
    try:
        # ValueError covers both JSONDecodeError and a UnicodeDecodeError from
        # a corrupt marker; neither should turn the feed into an error feed.
        payload = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = payload.get("non_rotating_expires_at") if isinstance(payload, dict) else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def write_claude_refresh_non_rotating_expiry(state: Path | None, expires_at: float) -> bool:
    """Remember an expiry a prompt already failed to move.

    Repeating that prompt cannot help until the stored credential changes, and
    each attempt is a real request, so the observed expiry is recorded and used
    to skip further attempts. Written atomically so a concurrent reader never
    sees a partial marker.
    """
    if state is None:
        return False
    try:
        state.parent.mkdir(parents=True, exist_ok=True)
        scratch = state.with_name(f"{state.name}.tmp")
        scratch.write_text(json.dumps({"non_rotating_expires_at": expires_at}), encoding="utf-8")
        os.replace(scratch, state)
        return True
    except OSError:
        return False


def claude_refresh_state_is_writable(state: Path | None) -> bool:
    """Whether a non-rotation result could be recorded if one occurred.

    Checked before spending a prompt: without persistence the suppression that
    bounds repeat attempts cannot work, so the prompt is skipped instead.

    The check must not read and rewrite the real marker, because it runs before
    the lock is held and would restore a stale value over another collector's
    write. It therefore rejects a directory occupying the target and exercises
    the same directory and rename using probe files beside it.
    """
    if state is None:
        return False
    if state.is_dir():
        # os.replace onto a directory fails, and a probe beside the target
        # would not notice.
        return False
    try:
        # Exercise the same directory and rename the real write uses, without
        # touching the recorded value: another collector's marker must not be
        # read and rewritten from a stale snapshot.
        state.parent.mkdir(parents=True, exist_ok=True)
        source = state.with_name(f"{state.name}.probe")
        target = state.with_name(f"{state.name}.probe.moved")
        source.write_text("", encoding="utf-8")
        os.replace(source, target)
        target.unlink()
        return True
    except OSError:
        return False


def clear_claude_refresh_non_rotating_expiry(state: Path | None) -> None:
    if state is None:
        return
    try:
        state.unlink()
    except OSError:
        pass


def claude_oauth_refresh_cooldown_active(
    stamp: Path | None,
    cooldown_minutes: int,
    now: float | None = None,
) -> bool:
    if stamp is None or cooldown_minutes <= 0:
        return False
    try:
        last_attempt = stamp.stat().st_mtime
    except OSError:
        return False
    elapsed = (time.time() if now is None else now) - last_attempt
    if elapsed < 0:
        # A future-dated marker, from a clock change or a copied cache, would
        # otherwise hold the cooldown for as long as it is ahead.
        return False
    return elapsed < cooldown_minutes * 60


@contextlib.contextmanager
def claude_refresh_claim(stamp: Path | None):
    """Exclusive, non-blocking claim on the refresh attempt.

    Yields False when another collector already holds it, so a shared cache
    directory cannot produce two prompts, or two rotations racing each other.
    """
    if stamp is None or fcntl is None:
        yield True
        return
    handle = None
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        handle = open(stamp.with_name(f"{stamp.name}.lock"), "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if handle is not None:
            handle.close()
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def claude_refresh_ping_environment() -> dict[str, str]:
    """Environment for the refresh prompt.

    Claude Code prefers a direct API key, auth token, third-party provider, or
    gateway base URL over the stored subscription login. Any of those would let
    the prompt succeed without rotating the credential the dashboard reads, so
    they are removed. Everything else is inherited, including proxy, mTLS,
    custom CA, and relocated-config variables that a deployment may require.
    This cannot be exhaustive, which is why the caller verifies that the stored
    expiry actually moved rather than trusting the exit code.
    """
    return {
        name: value
        for name, value in os.environ.items()
        if name not in CLAUDE_REFRESH_PING_ENV_DENYLIST
    }


def run_claude_refresh_ping(binary: str, model: str, workdir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            binary,
            "--print",
            "ok",
            "--model",
            model,
            # Keep an unattended maintenance prompt from writing a transcript,
            # loading MCP servers, or enabling tools. `--bare` is deliberately
            # not used: it skips keychain reads and never reads OAuth, which is
            # exactly the credential this prompt exists to rotate.
            "--safe-mode",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--tools",
            "",
        ],
        check=False,
        cwd=str(workdir),
        env=claude_refresh_ping_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=120,
    )


def trigger_claude_oauth_refresh(
    claude_bin: str | None,
    stamp: Path | None,
    cooldown_minutes: int,
    model: str,
    notes: list[str],
    workdir: Path | None = None,
    state: Path | None = None,
) -> bool:
    """Ask Claude Code to rotate its own OAuth access token.

    Claude Code only refreshes the stored access token when the CLI actually
    issues an API request; `auth status` does not, and the desktop app never
    writes that entry back. A minimal headless prompt is the cheapest supported
    trigger, so the collector never handles the refresh token itself.
    """
    binary = claude_bin or shutil.which("claude")
    if not binary:
        notes.append("Claude OAuth refresh skipped: claude executable not found")
        return False
    # The prompt runs with a different working directory, so a relative
    # executable would resolve against that directory instead.
    binary = str(Path(binary).resolve())
    # Locking, the cooldown, and the non-rotation record all live beside the
    # stamp. Without it nothing bounds repeat attempts, so a caller that omits
    # it does not get to spend a request at all.
    if stamp is None or state is None:
        notes.append("Claude OAuth refresh skipped: no cache directory for its retry bounds")
        return False
    # Without persistence the suppression that bounds repeat attempts cannot
    # work, so skip rather than spend a request that may repeat indefinitely.
    if not claude_refresh_state_is_writable(state):
        notes.append("Claude OAuth refresh skipped: refresh state unwritable")
        return False
    # An exclusive claim makes the cooldown check and its marker atomic, so two
    # collectors sharing a cache directory cannot both spend a prompt.
    with claude_refresh_claim(stamp) as claimed:
        if not claimed:
            return False
        if claude_oauth_refresh_cooldown_active(stamp, cooldown_minutes):
            return False
        # The prompt costs a real request, so an unrecordable cooldown must stop
        # it rather than let every collection cycle retry.
        if stamp is not None:
            try:
                stamp.parent.mkdir(parents=True, exist_ok=True)
                stamp.touch()
            except OSError:
                notes.append("Claude OAuth refresh skipped: cooldown marker unwritable")
                return False
        # A stable working directory keeps Claude Code's project history for
        # these prompts under one entry instead of one per prompt.
        try:
            if workdir is not None:
                workdir.mkdir(parents=True, exist_ok=True)
                result = run_claude_refresh_ping(binary, model, workdir)
            else:
                with tempfile.TemporaryDirectory(prefix="trmnl-claude-refresh-") as scratch:
                    result = run_claude_refresh_ping(binary, model, Path(scratch))
        except Exception:  # noqa: BLE001 - refresh is best-effort maintenance.
            notes.append("Claude OAuth refresh ping did not run")
            return False
    if result.returncode != 0:
        notes.append("Claude OAuth refresh ping failed")
        return False
    return True


def claude_oauth_recovery_note(reason: str, oauth: dict[str, Any]) -> str:
    refresh_token = oauth.get("refreshToken")
    if isinstance(refresh_token, str) and refresh_token.strip():
        return f"{reason}; run Claude Code once to refresh"
    return f"{reason}; run /login in Claude Code"


def claude_usage_auth(oauth_notes: list[str], fallback: str) -> str:
    if any("run Claude Code once to refresh" in note for note in oauth_notes):
        return "run Claude"
    if any("run /login" in note for note in oauth_notes):
        return "run /login"
    return fallback


def read_claude_oauth_usage(
    claude_root: Path,
    notes: list[str],
    claude_bin: str | None = None,
    enable_refresh: bool = False,
    refresh_stamp: Path | None = None,
    refresh_skew_minutes: int = DEFAULT_CLAUDE_OAUTH_REFRESH_SKEW_MINUTES,
    refresh_cooldown_minutes: int = DEFAULT_CLAUDE_OAUTH_REFRESH_COOLDOWN_MINUTES,
    refresh_model: str = DEFAULT_CLAUDE_OAUTH_REFRESH_MODEL,
) -> tuple[dict[str, Any] | None, str | None]:
    read_notes: list[str] = []
    oauth = read_claude_oauth_credentials(claude_root, read_notes)
    expires_at = claude_oauth_expiry_epoch(oauth)
    refresh_state = (refresh_stamp.parent / CLAUDE_OAUTH_REFRESH_STATE_NAME) if refresh_stamp else None
    refresh_notes: list[str] = []
    attempted_refresh = False
    spent_prompt = False

    def record_unproductive_prompt() -> None:
        """Bound a prompt that ran but still left the feed without limits.

        Rotation can succeed while the usage endpoint keeps rejecting the
        account, or the reread can fail. Without a record, the cooldown alone
        would let that same paid prompt repeat indefinitely.
        """
        if spent_prompt:
            write_claude_refresh_non_rotating_expiry(refresh_state, expires_at)

    def attempt_refresh(reason_expired: bool) -> bool:
        """Try once to have Claude Code rotate the stored credential."""
        nonlocal oauth, expires_at, read_notes, attempted_refresh, spent_prompt
        attempted_refresh = True
        known_stuck = read_claude_refresh_non_rotating_expiry(refresh_state)
        if known_stuck is not None and known_stuck == expires_at:
            # A prompt already failed to move this exact expiry. Keep reporting
            # it, but stop paying for the same request every cooldown.
            refresh_notes.append(CLAUDE_OAUTH_REFRESH_NO_ROTATION_NOTE)
            return False
        if not trigger_claude_oauth_refresh(
            claude_bin,
            refresh_stamp,
            refresh_cooldown_minutes,
            refresh_model,
            refresh_notes,
            workdir=(refresh_stamp.parent / CLAUDE_OAUTH_REFRESH_WORKDIR_NAME) if refresh_stamp else None,
            state=refresh_state,
        ):
            return False
        spent_prompt = True
        retry_notes: list[str] = []
        refreshed = read_claude_oauth_credentials(claude_root, retry_notes)
        if refreshed is None:
            # The prompt ran but the credential became unreadable; report that
            # instead of the pre-refresh diagnosis.
            read_notes = retry_notes
            return False
        refreshed_expiry = claude_oauth_expiry_epoch(refreshed)
        rotated = refreshed_expiry > expires_at
        if rotated:
            clear_claude_refresh_non_rotating_expiry(refresh_state)
        elif reason_expired:
            # A credential the service or the clock says is unusable, which a
            # successful prompt did not move, means the prompt authenticated as
            # something else. Declining to rotate a valid token is normal and is
            # not recorded.
            refresh_notes.append(CLAUDE_OAUTH_REFRESH_NO_ROTATION_NOTE)
            if not write_claude_refresh_non_rotating_expiry(refresh_state, refreshed_expiry):
                # Without the record this prompt would be repurchased every
                # cooldown, so say so rather than repeat it silently.
                refresh_notes.append("Claude OAuth refresh state could not be recorded")
        oauth = refreshed
        read_notes = retry_notes
        expires_at = refreshed_expiry
        return rotated

    if enable_refresh and claude_oauth_refresh_is_useful(oauth, expires_at, refresh_skew_minutes):
        attempt_refresh(bool(expires_at) and expires_at <= time.time())

    while True:
        if not oauth:
            record_unproductive_prompt()
            notes.extend(refresh_notes)
            notes.extend(read_notes)
            return None, None
        token = oauth["accessToken"]
        raw_subscription = oauth.get("subscriptionType")
        subscription_type = (
            raw_subscription.strip()
            if isinstance(raw_subscription, str) and raw_subscription.strip()
            else None
        )
        if expires_at and expires_at <= time.time():
            record_unproductive_prompt()
            notes.extend(refresh_notes)
            notes.extend(read_notes)
            notes.append(claude_oauth_recovery_note("Claude OAuth access token expired", oauth))
            return None, None
        request = urllib.request.Request(
            CLAUDE_OAUTH_USAGE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "TRMNLAgentUsageCollector/1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read(50000))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                # The service rejected a credential the local clock still
                # considers valid, which is the authoritative signal. Try one
                # rotation before giving up for this run.
                if enable_refresh and not attempted_refresh and claude_oauth_refresh_is_useful(
                    oauth, expires_at, refresh_skew_minutes, ignore_expiry=True
                ):
                    if attempt_refresh(True):
                        continue
                record_unproductive_prompt()
                notes.extend(refresh_notes)
                notes.extend(read_notes)
                notes.append(claude_oauth_recovery_note("Claude OAuth usage endpoint HTTP 401", oauth))
                return None, None
            notes.extend(refresh_notes)
            notes.extend(read_notes)
            notes.append(f"Claude OAuth usage endpoint HTTP {exc.code}")
            return None, subscription_type
        except Exception as exc:  # noqa: BLE001 - optional first-party endpoint.
            notes.extend(refresh_notes)
            notes.extend(read_notes)
            notes.append(f"Claude OAuth usage endpoint failed: {type(exc).__name__}")
            return None, subscription_type

        notes.extend(refresh_notes)
        notes.extend(read_notes)
        limits = normalize_claude_oauth_usage(payload)
        if not limits.get("five_hour") and not limits.get("weekly"):
            notes.append("Claude OAuth usage endpoint returned no recognized limits")
            return None, subscription_type
        return limits, subscription_type


def run_json(cmd: list[str], errors: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
        return json.loads(result.stdout)
    except Exception as exc:  # noqa: BLE001 - feed should degrade, not crash.
        errors.append(f"{Path(cmd[0]).name}: {safe_exception(exc)}")
        return {}


def safe_exception(exc: BaseException) -> str:
    summary = type(exc).__name__
    if isinstance(exc, subprocess.CalledProcessError):
        summary += f" rc={exc.returncode}"
    elif isinstance(exc, subprocess.TimeoutExpired):
        summary += " timeout"
    return summary


def ccusage(ccusage_bin: str, args: list[str], tz_name: str, offline: bool, errors: list[str]) -> dict[str, Any]:
    cmd = [ccusage_bin, *args, "--json", "--timezone", tz_name]
    if args and args[0] == "claude":
        cmd.extend(["--mode", "calculate"])
    cmd.append("--offline" if offline else "--no-offline")
    return run_json(cmd, errors)


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), timezone.utc)
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def local_dt(dt: datetime | None, tz: ZoneInfo) -> str:
    if not dt:
        return "n/a"
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def local_time(dt: datetime | None, tz: ZoneInfo) -> str:
    if not dt:
        return "n/a"
    return dt.astimezone(tz).strftime("%H:%M")


def compact_reset_label(
    dt: datetime | None,
    tz: ZoneInfo,
    window: str,
    now: datetime | None = None,
) -> str:
    if not dt:
        return "n/a"
    local = dt.astimezone(tz)
    if window != "7d":
        return local.strftime("%H:%M")
    today = (now or datetime.now(tz)).astimezone(tz).date()
    days_until = (local.date() - today).days
    if days_until == 0:
        return f"Today {local:%H:%M}"
    if days_until == 1:
        return f"Tomorrow {local:%H:%M}"
    return local.strftime("%b %d %H:%M")


def age_label(dt: datetime | None, now: datetime) -> str:
    if not dt:
        return "n/a"
    delta = now.astimezone(timezone.utc) - dt.astimezone(timezone.utc)
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 90:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def short_number(value: Any) -> str:
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    sign = "-" if num < 0 else ""
    num = abs(num)
    for suffix, size in (("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)):
        if num >= size:
            value = num / size
            if value >= 100:
                return f"{sign}{value:.0f}{suffix}"
            if value >= 10:
                return f"{sign}{value:.1f}{suffix}"
            return f"{sign}{value:.2f}{suffix}"
    return f"{sign}{num:.0f}"


def money(value: Any) -> str:
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        return "$0"
    if abs(num) >= 100:
        return f"${num:,.0f}"
    return f"${num:,.2f}"


def pct(value: Any) -> int:
    try:
        return max(0, min(100, int(round(float(value or 0)))))
    except (TypeError, ValueError):
        return 0


def normalize_metrics(row: dict[str, Any]) -> dict[str, float]:
    def number(*keys: str) -> float:
        for key in keys:
            if key not in row or row.get(key) is None:
                continue
            try:
                return float(row[key])
            except (TypeError, ValueError):
                continue
        return 0.0

    input_tokens = number("inputTokens")
    output_tokens = number("outputTokens")
    reasoning_tokens = number("reasoningOutputTokens")
    cache_creation = number("cacheCreationTokens", "cacheCreationInputTokens")
    cache_read = number("cacheReadTokens", "cachedInputTokens", "cacheReadInputTokens")
    total_tokens = number("totalTokens")
    if not total_tokens:
        total_tokens = input_tokens + output_tokens + cache_creation + cache_read
    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "reasoningOutputTokens": reasoning_tokens,
        "cacheCreationTokens": cache_creation,
        "cacheReadTokens": cache_read,
        "totalTokens": total_tokens,
        "costUSD": number("costUSD", "totalCost"),
    }


def add_metrics(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    merged = dict(a)
    for key, value in b.items():
        merged[key] = merged.get(key, 0) + float(value or 0)
    return merged


def aggregate(rows: list[dict[str, Any]]) -> dict[str, float]:
    total: dict[str, float] = {}
    for row in rows:
        total = add_metrics(total, normalize_metrics(row))
    return total


def usage_card(label: str, metrics: dict[str, float], sessions: int | None = None) -> dict[str, Any]:
    total = metrics.get("totalTokens", 0)
    cache = metrics.get("cacheReadTokens", 0) + metrics.get("cacheCreationTokens", 0)
    input_tokens = metrics.get("inputTokens", 0)
    cache_hit = int(round((cache / max(cache + input_tokens, 1)) * 100))
    card = {
        "label": label,
        "total": short_number(total),
        "total_raw": int(total),
        "input": short_number(input_tokens),
        "output": short_number(metrics.get("outputTokens", 0)),
        "reasoning": short_number(metrics.get("reasoningOutputTokens", 0)),
        "cache_read": short_number(metrics.get("cacheReadTokens", 0)),
        "cache_write": short_number(metrics.get("cacheCreationTokens", 0)),
        "cache": short_number(cache),
        "cache_hit": f"{cache_hit}%",
        "cost": money(metrics.get("costUSD", 0)),
    }
    if sessions is not None:
        card["sessions"] = str(sessions)
    return card


def unknown_cost(card: dict[str, Any]) -> dict[str, Any]:
    updated = dict(card)
    updated["cost"] = "n/a"
    return updated


def daily_rows(data: dict[str, Any], key: str = "daily") -> list[dict[str, Any]]:
    rows = data.get(key)
    return rows if isinstance(rows, list) else []


def rows_from_date(rows: list[dict[str, Any]], start_date: str, end_date: str | None = None) -> list[dict[str, Any]]:
    end = end_date or "9999-99-99"
    return [row for row in rows if start_date <= str(row.get("date", "")) <= end]


def row_for_date(rows: list[dict[str, Any]], date: str) -> dict[str, Any]:
    for row in rows:
        if row.get("date") == date:
            return row
    return {}


def model_totals_codex(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: Counter[str] = Counter()
    for row in rows:
        models = row.get("models") or {}
        if not isinstance(models, dict):
            continue
        for name, data in models.items():
            if not isinstance(data, dict):
                continue
            total = int(data.get("totalTokens") or 0)
            totals[str(name)] += total
    grand = sum(totals.values()) or 1
    return [
        {
            "name": name,
            "tokens": short_number(tokens),
            "raw_tokens": int(tokens),
            "pct": pct(tokens * 100 / grand),
        }
        for name, tokens in totals.most_common(5)
    ]


def display_model_name(name: str) -> str:
    if name.startswith("claude-haiku-4-5-"):
        return "claude-haiku-4-5"
    if len(name) > 22:
        return name[:21]
    return name


def model_totals_claude(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: Counter[str] = Counter()
    costs: Counter[str] = Counter()
    for row in rows:
        for item in row.get("modelBreakdowns") or []:
            name = str(item.get("modelName") or "unknown")
            tokens = (
                int(item.get("inputTokens") or 0)
                + int(item.get("outputTokens") or 0)
                + int(item.get("cacheReadTokens") or 0)
                + int(item.get("cacheCreationTokens") or 0)
            )
            totals[name] += tokens
            costs[name] += float(item.get("cost") or 0)
    grand = sum(totals.values()) or 1
    return [
        {
            "name": display_model_name(name),
            "raw_name": name,
            "tokens": short_number(tokens),
            "raw_tokens": int(tokens),
            "pct": pct(tokens * 100 / grand),
            "cost": money(costs.get(name, 0)),
        }
        for name, tokens in totals.most_common(5)
    ]


def sessions_summary(sessions: list[dict[str, Any]], today: datetime, week_start: datetime) -> dict[str, Any]:
    today_date = today.date()
    week_date = week_start.date()
    five_hours_ago = today - timedelta(hours=5)
    counts = {"today": 0, "week": 0, "five_hour": 0}
    latest: datetime | None = None

    for session in sessions:
        dt = parse_dt(session.get("lastActivity") or session.get("last_activity"))
        if not dt:
            continue
        local_date = dt.astimezone(today.tzinfo).date()
        if local_date == today_date:
            counts["today"] += 1
        if local_date >= week_date:
            counts["week"] += 1
        if dt >= five_hours_ago.astimezone(timezone.utc):
            counts["five_hour"] += 1
        if latest is None or dt > latest:
            latest = dt

    return {
        "today": counts["today"],
        "week": counts["week"],
        "five_hour": counts["five_hour"],
        "all": len(sessions),
        "latest": local_time(latest, today.tzinfo),  # type: ignore[arg-type]
        "top_project": "n/a",
    }


def streak(rows: list[dict[str, Any]], today: datetime) -> int:
    by_date = {str(row.get("date")): int(row.get("totalTokens") or 0) for row in rows}
    count = 0
    current = today.date()
    while by_date.get(current.isoformat(), 0) > 0:
        count += 1
        current -= timedelta(days=1)
    return count


def daily_bars(rows: list[dict[str, Any]], today: datetime, days: int = 7) -> list[dict[str, Any]]:
    by_date = {str(row.get("date")): int(row.get("totalTokens") or 0) for row in rows}
    dates = [(today.date() - timedelta(days=offset)) for offset in range(days)]
    values = [by_date.get(day.isoformat(), 0) for day in dates]
    high = max(values) or 1
    return [
        {
            "day": day.strftime("%a"),
            "tokens": short_number(value),
            "pct": max(4, pct(value * 100 / high)) if value else 0,
        }
        for day, value in zip(dates, values)
    ]


def trend_label(today_metrics: dict[str, float], yesterday_metrics: dict[str, float]) -> str:
    today_total = today_metrics.get("totalTokens", 0)
    yesterday_total = yesterday_metrics.get("totalTokens", 0)
    if yesterday_total <= 0 and today_total > 0:
        return "new"
    if yesterday_total <= 0:
        return "flat"
    change = (today_total - yesterday_total) / yesterday_total
    if change > 0.15:
        return "up"
    if change < -0.15:
        return "down"
    return "flat"


def codex_app_server_rpc(codex_bin: str, requests: list[dict[str, Any]], timeout: float = 30.0) -> dict[Any, dict[str, Any]]:
    process = subprocess.Popen(
        [codex_bin, "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    assert process.stdin and process.stdout
    init = {
        "id": "__init__",
        "method": "initialize",
        "params": {
            "clientInfo": {
                "name": "trmnl-agent-usage-collector",
                "title": "TRMNL Agent Usage Collector",
                "version": "1",
            },
            "capabilities": {"experimentalApi": True, "requestAttestation": False},
        },
    }
    for request in [init, *requests]:
        process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        process.stdin.flush()

    wanted = {request.get("id") for request in requests}
    responses: dict[Any, dict[str, Any]] = {}
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while wanted - responses.keys() and time.monotonic() < deadline:
            for key, _ in selector.select(max(0.1, min(0.5, deadline - time.monotonic()))):
                line = key.fileobj.readline()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("id") in wanted:
                    responses[payload["id"]] = payload
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
    return responses


def normalize_app_rate_limit_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None

    def window(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        return {
            "used_percent": raw.get("usedPercent"),
            "window_minutes": raw.get("windowDurationMins"),
            "resets_at": raw.get("resetsAt"),
        }

    credits = snapshot.get("credits")
    return {
        "limit_id": snapshot.get("limitId"),
        "limit_name": snapshot.get("limitName"),
        "primary": window(snapshot.get("primary")),
        "secondary": window(snapshot.get("secondary")),
        "credits": {
            "has_credits": bool(credits.get("hasCredits")),
            "unlimited": bool(credits.get("unlimited")),
            "balance": credits.get("balance"),
        }
        if isinstance(credits, dict)
        else None,
        "plan_type": snapshot.get("planType"),
        "rate_limit_reached_type": snapshot.get("rateLimitReachedType"),
    }


def normalize_codex_rate_limit_response(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    raw_by_id = result.get("rateLimitsByLimitId")
    raw_by_id = raw_by_id if isinstance(raw_by_id, dict) else {}
    normalized_by_id: dict[str, dict[str, Any]] = {}
    for limit_id, raw in raw_by_id.items():
        normalized = normalize_app_rate_limit_snapshot(raw)
        if not normalized:
            continue
        normalized["limit_id"] = str(normalized.get("limit_id") or limit_id)
        normalized_by_id[str(limit_id)] = normalized

    legacy_raw = result.get("rateLimits")
    legacy = None
    if isinstance(legacy_raw, dict):
        legacy_id = str(legacy_raw.get("limitId") or "")
        matching_raw = raw_by_id.get(legacy_id)
        merged_raw = {**matching_raw, **legacy_raw} if isinstance(matching_raw, dict) else legacy_raw
        legacy = normalize_app_rate_limit_snapshot(merged_raw)
        if legacy and legacy_id:
            normalized_by_id[legacy_id] = legacy

    selected = normalized_by_id.get("codex") or legacy
    if not selected and normalized_by_id:
        selected = next(iter(normalized_by_id.values()))
    if not selected:
        return None
    return {**selected, "limits_by_id": normalized_by_id}


def normalize_codex_account_usage(result: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(result, dict) or not isinstance(result.get("summary"), dict):
        return None
    summary = result["summary"]
    rows = []
    for item in result.get("dailyUsageBuckets") or []:
        if not isinstance(item, dict) or not item.get("startDate") or item.get("tokens") is None:
            continue
        try:
            tokens = int(item["tokens"])
        except (TypeError, ValueError):
            continue
        rows.append({"date": str(item["startDate"]), "totalTokens": tokens})

    def optional_int(key: str) -> int | None:
        value = summary.get(key)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    lifetime = optional_int("lifetimeTokens")
    peak = optional_int("peakDailyTokens")
    return {
        "lifetime": short_number(lifetime) if lifetime is not None else None,
        "lifetime_raw": lifetime,
        "peak_day": short_number(peak) if peak is not None else None,
        "peak_day_raw": peak,
        "current_streak": optional_int("currentStreakDays"),
        "longest_streak": optional_int("longestStreakDays"),
        "daily_rows": rows,
    }


def select_app_rate_limit_snapshot(result: dict[str, Any]) -> dict[str, Any] | None:
    """Compatibility helper that returns the selected bucket plus the current multi-bucket map."""
    return normalize_codex_rate_limit_response(result)


def classify_codex_rate_limit_windows(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """Map legacy and multi-limit app-server buckets by duration and limit id."""
    if not isinstance(snapshot, dict):
        return None

    classified = {**snapshot, "primary": None, "secondary": None, "extra": None}
    unclassified: list[dict[str, Any]] = []
    by_id = snapshot.get("limits_by_id") if isinstance(snapshot.get("limits_by_id"), dict) else {}
    snapshots = list(by_id.values()) if by_id else [snapshot]
    candidates: list[dict[str, Any]] = []
    for limit in snapshots:
        if not isinstance(limit, dict):
            continue
        for source_key in ("primary", "secondary"):
            item = limit.get(source_key)
            if not isinstance(item, dict):
                continue
            candidates.append(
                {
                    **item,
                    "source": source_key,
                    "limit_id": str(limit.get("limit_id") or ""),
                    "limit_name": limit.get("limit_name"),
                }
            )

    five_hour = [item for item in candidates if int(item.get("window_minutes") or 0) == CODEX_FIVE_HOUR_WINDOW_MINUTES]
    weekly = [item for item in candidates if int(item.get("window_minutes") or 0) == CODEX_WEEKLY_WINDOW_MINUTES]
    classified["primary"] = next((item for item in five_hour if item.get("limit_id") == "codex"), None)
    if classified["primary"] is None and five_hour:
        classified["primary"] = five_hour[0]
    classified["secondary"] = next((item for item in weekly if item.get("limit_id") == "codex"), None)
    if classified["secondary"] is None and weekly:
        classified["secondary"] = weekly[0]
    chosen = {id(item) for item in (classified["primary"], classified["secondary"]) if item is not None}
    extras = [item for item in candidates if id(item) not in chosen]
    classified["extra"] = extras[0] if extras else None
    for item in candidates:
        try:
            minutes = int(item.get("window_minutes") or 0)
        except (TypeError, ValueError):
            minutes = 0
        if item is not classified["extra"] and (
            item in extras[1:] or minutes not in {CODEX_FIVE_HOUR_WINDOW_MINUTES, CODEX_WEEKLY_WINDOW_MINUTES}
        ):
            unclassified.append(item)
    classified["unclassified_windows"] = unclassified
    return classified


def read_codex_app_data(
    codex_bin: str | None,
    notes: list[str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    if not codex_bin:
        notes.append("Codex app-server not found")
        return None, None, None
    try:
        responses = codex_app_server_rpc(
            codex_bin,
            [
                {"id": "rate-limits", "method": "account/rateLimits/read"},
                {"id": "account-usage", "method": "account/usage/read"},
            ],
            timeout=25,
        )
    except Exception as exc:  # noqa: BLE001 - optional app-server path.
        notes.append(f"Codex app-server read failed: {type(exc).__name__}")
        return None, None, None
    rate_result = (responses.get("rate-limits") or {}).get("result")
    usage_result = (responses.get("account-usage") or {}).get("result")
    if not isinstance(rate_result, dict):
        notes.append("Codex app-server rate limits unavailable")
    rate_limits = normalize_codex_rate_limit_response(rate_result)
    reset_credits = (
        rate_result.get("rateLimitResetCredits")
        if isinstance(rate_result, dict) and isinstance(rate_result.get("rateLimitResetCredits"), dict)
        else None
    )
    account_usage = normalize_codex_account_usage(usage_result)
    if rate_limits:
        notes.append("Codex rate limits from supported app-server")
        if (rate_limits.get("limits_by_id") or {}):
            notes.append("Codex multi-limit buckets from rateLimitsByLimitId")
    if reset_credits:
        notes.append("Banked reset count from supported app-server")
    if account_usage:
        notes.append("Account lifetime and activity from supported app-server")
    return rate_limits, reset_credits, account_usage


def read_codex_private_reset_credits(codex_bin: str | None, notes: list[str]) -> dict[str, Any] | None:
    if not codex_bin:
        return None
    try:
        response = codex_app_server_rpc(
            codex_bin,
            [
                {
                    "id": "auth",
                    "method": "getAuthStatus",
                    "params": {"includeToken": True, "refreshToken": True},
                }
            ],
            timeout=25,
        ).get("auth")
        token = ((response or {}).get("result") or {}).get("authToken")
        if not token:
            notes.append("Private reset-credit endpoint unavailable: no app-server token")
            return None
        request = urllib.request.Request(
            "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "TRMNLAgentUsageCollector/1",
            },
        )
        with urllib.request.urlopen(request, timeout=20) as resp:
            data = json.loads(resp.read(20000))
    except urllib.error.HTTPError as exc:
        notes.append(f"Private reset-credit endpoint HTTP {exc.code}")
        return None
    except Exception as exc:  # noqa: BLE001 - optional private endpoint.
        notes.append(f"Private reset-credit endpoint failed: {type(exc).__name__}")
        return None
    if isinstance(data, dict):
        notes.append("Banked reset expiry from private wham endpoint")
        return data
    return None


def supported_reset_credit_expiries(data: dict[str, Any] | None) -> list[datetime]:
    expires: list[datetime] = []
    for credit in (data or {}).get("credits") or []:
        if not isinstance(credit, dict) or str(credit.get("status") or "").lower() != "available":
            continue
        dt = parse_dt(credit.get("expiresAt"))
        if dt:
            expires.append(dt)
    return expires


def reset_credit_display(
    supported: dict[str, Any] | None,
    private: dict[str, Any] | None,
    tz: ZoneInfo,
) -> dict[str, str]:
    available: int | None = None
    if isinstance(supported, dict) and supported.get("availableCount") is not None:
        try:
            available = int(supported.get("availableCount"))
        except (TypeError, ValueError):
            available = None
    if available is None and isinstance(private, dict) and private.get("available_count") is not None:
        try:
            available = int(private.get("available_count"))
        except (TypeError, ValueError):
            pass

    supported_expires = supported_reset_credit_expiries(supported)

    private_expires: list[datetime] = []
    for credit in (private or {}).get("credits") or []:
        if not isinstance(credit, dict) or credit.get("status") != "available":
            continue
        dt = parse_dt(credit.get("expires_at"))
        if dt:
            private_expires.append(dt)

    expires = supported_expires or private_expires
    earliest_expiry = min(expires) if expires else None
    if available is None:
        count = "not exposed"
    else:
        count = str(available)
    return {
        "banked_reset": count,
        "banked_reset_expires": local_dt(earliest_expiry, tz) if earliest_expiry else "not exposed",
        "banked_reset_expires_short": earliest_expiry.astimezone(tz).strftime("%m-%d %H:%M") if earliest_expiry else "not exposed",
        "banked_reset_summary": f"{count} @ {earliest_expiry.astimezone(tz).strftime('%m-%d %H:%M')}"
        if earliest_expiry and count != "not exposed"
        else (f"{count} resets" if count != "not exposed" else "not exposed"),
        "banked_reset_source": (
            "app-server"
            if supported_expires
            else (
                "app-server + private wham"
                if private_expires and isinstance(supported, dict) and supported.get("availableCount") is not None
                else ("private wham" if private_expires else ("app-server" if available is not None else "not exposed"))
            )
        ),
    }


def load_claude_statusline_cache(
    path: Path,
    warnings: list[str],
    now: datetime,
    tz: ZoneInfo,
    max_age_minutes: int = DEFAULT_CLAUDE_STATUSLINE_MAX_AGE_MINUTES,
) -> dict[str, Any]:
    try:
        data = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except FileNotFoundError:
        warnings.append("Claude statusline cache pending")
        return {}
    except Exception as exc:  # noqa: BLE001 - optional cache.
        warnings.append(f"Claude statusline cache unreadable: {type(exc).__name__}")
        return {}
    rate_limits = data.get("rate_limits")
    if not isinstance(rate_limits, dict):
        warnings.append("Claude statusline cache has no rate_limits")
        return {}
    captured = parse_dt(data.get("captured_at"))
    if not captured:
        # Keep compatibility with an early local-only timestamp format.
        try:
            captured = datetime.strptime(str(data.get("captured_at")), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
        except Exception:
            captured = None
    source = str(data.get("source") or "Claude Code statusLine stdin")
    captured_age = age_label(captured, now)
    if not captured:
        warnings.append("Claude statusline cache has no captured_at")
        return {"rate_limits": rate_limits, "captured_age": captured_age, "source": source, "stale": True}
    if max_age_minutes > 0:
        age = now.astimezone(timezone.utc) - captured.astimezone(timezone.utc)
        if age > timedelta(minutes=max_age_minutes):
            warnings.append(f"Claude statusline cache stale: {captured_age}")
            return {"rate_limits": rate_limits, "captured_age": captured_age, "source": source, "stale": True}
    return {
        "rate_limits": rate_limits,
        "captured_age": captured_age,
        "source": source,
        "stale": False,
    }


def valid_claude_statusline_limits(
    rate_limits: dict[str, Any] | None,
    now: datetime,
    stale: bool,
) -> dict[str, Any]:
    """Keep future resets from stale snapshots, but never stale percentages."""
    valid: dict[str, Any] = {}
    for key, item in (rate_limits or {}).items():
        if not isinstance(item, dict):
            continue
        reset = parse_dt(item.get("resets_at"))
        if reset and reset <= now.astimezone(timezone.utc):
            continue
        if stale and not reset:
            continue
        valid[key] = {"resets_at": item.get("resets_at")} if stale else item
    return valid


def statusline_limit_card(
    raw: dict[str, Any] | None,
    key: str,
    label: str,
    window: str,
    tz: ZoneInfo,
    fallback_reset: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    item = (raw or {}).get(key)
    if not isinstance(item, dict):
        return {
            "label": label,
            "used": "n/a",
            "used_percent": 0,
            "window": window,
            "reset": compact_reset_label(fallback_reset, tz, window, now),
            "reset_full": local_dt(fallback_reset, tz),
        }

    reset_dt = parse_dt(item.get("resets_at")) or fallback_reset
    has_used = item.get("used_percentage") is not None
    used = pct(item.get("used_percentage")) if has_used else 0
    return {
        "label": label,
        "used": f"{used}%" if has_used else "n/a",
        "used_percent": used,
        "window": window,
        "reset": compact_reset_label(reset_dt, tz, window, now),
        "reset_full": local_dt(reset_dt, tz),
    }


CODEX_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


def codex_usage_values(raw: Any) -> tuple[int, int, int, int, int] | None:
    if not isinstance(raw, dict):
        return None
    return tuple(max(0, int(raw.get(key) or 0)) for key in CODEX_USAGE_KEYS)


def subtract_codex_usage(
    current: tuple[int, int, int, int, int],
    previous: tuple[int, int, int, int, int] | None,
) -> tuple[int, int, int, int, int]:
    return tuple(max(0, value - (previous[index] if previous else 0)) for index, value in enumerate(current))


def codex_model_from(raw: Any) -> str | None:
    if not isinstance(raw, dict):
        return None
    for key in ("model", "model_name"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = raw.get("metadata")
    if isinstance(metadata, dict):
        value = metadata.get("model")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def codex_replay_second(path: Path) -> str | None:
    """Return the creation second containing a fork/subagent replay prefix."""
    try:
        with path.open("rb") as handle:
            prefix = handle.read(16 * 1024)
        if b"thread_spawn" not in prefix and b"forked_from_id" not in prefix:
            return None

        first_second = None
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if "token_count" not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = row.get("payload") or {}
                info = payload.get("info") or {}
                if (
                    row.get("type") != "event_msg"
                    or payload.get("type") != "token_count"
                    or not isinstance(info, dict)
                    or not (
                        isinstance(info.get("last_token_usage"), dict)
                        or isinstance(info.get("total_token_usage"), dict)
                    )
                ):
                    continue
                timestamp = str(row.get("timestamp") or "")
                if len(timestamp) < 19:
                    continue
                second = timestamp[:19]
                if first_second is None:
                    first_second = second
                else:
                    return second if second == first_second else None
    except OSError:
        return None
    return None


def codex_session_candidates(sessions_dir: Path, since: datetime) -> list[Path]:
    """Select files that can contain rolling-window events without rereading all history."""
    recent: list[Path] = []
    newest: tuple[float, Path] | None = None
    cutoff = since.timestamp()

    for path in sessions_dir.rglob("*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest[0]:
            newest = (mtime, path)
        if mtime >= cutoff:
            recent.append(path)

    if not recent and newest is not None:
        recent.append(newest[1])
    return sorted(recent)


def latest_codex_rate_limits(codex_root: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, float], int]:
    latest: tuple[datetime, dict[str, Any]] | None = None
    latest_credits: tuple[datetime, dict[str, Any]] | None = None
    five_hour_usage: dict[str, float] = {}
    five_hour_sessions: set[Path] = set()
    seen_usage_events: set[tuple[int, str, int, int, int, int, int]] = set()
    since = datetime.now(timezone.utc) - timedelta(hours=5)
    sessions_dir = codex_root / "sessions"

    for path in codex_session_candidates(sessions_dir, since):
        replay_second = codex_replay_second(path)
        skip_replay = replay_second is not None
        previous_totals: tuple[int, int, int, int, int] | None = None
        current_model: str | None = None
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if "rate_limits" not in line and "token_count" not in line and "turn_context" not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = parse_dt(row.get("timestamp"))
                    payload = row.get("payload") or {}
                    if not isinstance(payload, dict):
                        continue
                    if row.get("type") == "turn_context":
                        current_model = codex_model_from(payload) or current_model
                        continue
                    rate_limits = payload.get("rate_limits")
                    if isinstance(rate_limits, dict) and ts:
                        if latest is None or ts > latest[0]:
                            latest = (ts, rate_limits)
                        if isinstance(rate_limits.get("credits"), dict):
                            if latest_credits is None or ts > latest_credits[0]:
                                latest_credits = (ts, rate_limits.get("credits") or {})
                    if row.get("type") != "event_msg" or payload.get("type") != "token_count":
                        continue
                    info = payload.get("info") or {}
                    if not isinstance(info, dict):
                        continue
                    total_usage = codex_usage_values(info.get("total_token_usage"))
                    raw_timestamp = str(row.get("timestamp") or "")
                    if replay_second and skip_replay:
                        if raw_timestamp[:19] == replay_second:
                            if total_usage is not None:
                                previous_totals = total_usage
                            continue
                        skip_replay = False

                    usage = codex_usage_values(info.get("last_token_usage"))
                    if usage is None and total_usage is not None:
                        usage = subtract_codex_usage(total_usage, previous_totals)
                    if total_usage is not None:
                        previous_totals = total_usage
                    if usage is None or not any(usage) or ts is None or ts < since:
                        continue

                    model = codex_model_from(payload) or codex_model_from(info) or current_model or "gpt-5"
                    current_model = model
                    cached_input = min(usage[1], usage[0])
                    event_key = (
                        int(ts.timestamp() * 1000),
                        model,
                        usage[0],
                        cached_input,
                        usage[2],
                        usage[3],
                        usage[4],
                    )
                    if event_key in seen_usage_events:
                        continue
                    seen_usage_events.add(event_key)
                    mapped = {
                        "inputTokens": float(usage[0] - cached_input),
                        "outputTokens": float(usage[2]),
                        "reasoningOutputTokens": float(usage[3]),
                        "cacheReadTokens": float(cached_input),
                        "cacheCreationTokens": 0.0,
                        "totalTokens": float(usage[4]),
                        "costUSD": 0.0,
                    }
                    five_hour_usage = add_metrics(five_hour_usage, mapped)
                    five_hour_sessions.add(path)
        except OSError:
            continue

    return (
        latest[1] if latest else None,
        latest_credits[1] if latest_credits else None,
        five_hour_usage,
        len(five_hour_sessions),
    )


def limit_card(
    raw: dict[str, Any] | None,
    key: str,
    label: str,
    tz: ZoneInfo,
    expected_window: str = "n/a",
    now: datetime | None = None,
) -> dict[str, Any]:
    item = (raw or {}).get(key)
    if not isinstance(item, dict):
        return {
            "label": label,
            "used": "n/a",
            "used_percent": 0,
            "window": expected_window,
            "reset": "n/a",
            "reset_full": "n/a",
        }
    reset_dt = parse_dt(item.get("resets_at"))
    minutes = int(item.get("window_minutes") or 0)
    if minutes and minutes % 1440 == 0:
        window = f"{int(minutes / 1440)}d"
    elif minutes and minutes % 60 == 0:
        window = f"{int(minutes / 60)}h"
    elif minutes:
        window = f"{minutes}m"
    else:
        window = "n/a"
    used = pct(item.get("used_percent"))

    return {
        "label": label,
        "used": f"{used}%",
        "used_percent": used,
        "window": window,
        "reset": compact_reset_label(reset_dt, tz, window, now),
        "reset_full": local_dt(reset_dt, tz),
    }


def codex_limit_label(item: dict[str, Any] | None, fallback: str) -> str:
    if not isinstance(item, dict):
        return fallback
    if item.get("limit_id") == "codex":
        return "Weekly all" if int(item.get("window_minutes") or 0) == CODEX_WEEKLY_WINDOW_MINUTES else fallback
    name = str(item.get("limit_name") or "").strip()
    if name:
        return name.rsplit("-", 1)[-1][:18]
    return str(item.get("limit_id") or fallback).replace("codex_", "")[:18]


def codex_dashboard_limit_cards(
    classified: dict[str, Any] | None,
    tz: ZoneInfo,
    now: datetime,
) -> dict[str, dict[str, Any]]:
    rate_limits = classified or {}
    five_hour = rate_limits.get("primary")
    weekly = rate_limits.get("secondary")
    extra = rate_limits.get("extra")
    if isinstance(five_hour, dict):
        left_item, left_label, left_window = five_hour, "5h", "5h"
        right_item, right_label, right_window = weekly, codex_limit_label(weekly, "Weekly all"), "7d"
    elif isinstance(weekly, dict):
        left_item, left_label, left_window = weekly, codex_limit_label(weekly, "Weekly all"), "7d"
        right_item, right_label, right_window = extra, codex_limit_label(extra, "Other limit"), "7d"
    else:
        left_item, left_label, left_window = None, "5h", "5h"
        right_item, right_label, right_window = extra, codex_limit_label(extra, "Weekly all"), "7d"
    return {
        "primary": limit_card({"item": left_item}, "item", left_label, tz, left_window, now),
        "weekly": limit_card({"item": right_item}, "item", right_label, tz, right_window, now),
    }


def codex_activity_rows(
    account_usage: dict[str, Any] | None,
    local_rows: list[dict[str, Any]],
    today: str,
) -> tuple[list[dict[str, Any]], str]:
    account_rows = (account_usage or {}).get("daily_rows") or []
    if not account_rows:
        return local_rows, "Local activity"
    by_date = {str(row.get("date")): int(row.get("totalTokens") or 0) for row in account_rows}
    local_today = int(normalize_metrics(row_for_date(local_rows, today)).get("totalTokens") or 0)
    by_date[today] = max(by_date.get(today, 0), local_today)
    return ([{"date": date, "totalTokens": tokens} for date, tokens in by_date.items()], "Account activity")


def build_codex(
    ccusage_bin: str,
    tz_name: str,
    tz: ZoneInfo,
    codex_root: Path,
    offline: bool,
    codex_bin: str | None,
    enable_private_reset_credits: bool,
    title: str | None = DEFAULT_CODEX_TITLE,
) -> dict[str, Any]:
    errors: list[str] = []
    now = datetime.now(tz)
    today = now.date().isoformat()
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    week_start = now - timedelta(days=(now.weekday() + 1) % 7)
    month_start_date = now.strftime("%Y-%m-01")

    daily = ccusage(ccusage_bin, ["codex", "daily"], tz_name, offline, errors)
    monthly = ccusage(ccusage_bin, ["codex", "monthly"], tz_name, offline, errors)
    session = ccusage(ccusage_bin, ["codex", "session"], tz_name, offline, errors)
    rows = daily_rows(daily)
    sessions = session.get("sessions") or []
    if not isinstance(sessions, list):
        sessions = []

    today_metrics = normalize_metrics(row_for_date(rows, today))
    yesterday_metrics = normalize_metrics(row_for_date(rows, yesterday))
    month_rows = rows_from_date(rows, month_start_date, today)
    seven_rows = rows_from_date(rows, (now.date() - timedelta(days=6)).isoformat(), today)
    week_metrics = aggregate(seven_rows)
    month_metrics = aggregate(month_rows)
    all_metrics = normalize_metrics(daily.get("totals") or {})
    source_notes = ["Costs are API-rate estimates calculated from tokens, not subscription charges"]
    jsonl_rate_limits, jsonl_credits, five_hour_metrics, five_hour_events = latest_codex_rate_limits(codex_root)
    app_rate_limits, supported_reset_credits, account_usage = read_codex_app_data(codex_bin, source_notes)
    supported_reset_expiries = supported_reset_credit_expiries(supported_reset_credits)
    private_reset_credits = None
    if enable_private_reset_credits and not supported_reset_expiries:
        private_reset_credits = read_codex_private_reset_credits(codex_bin, source_notes)
    elif enable_private_reset_credits:
        source_notes.append("Banked reset expiry from supported app-server; private fallback not needed")
    if not enable_private_reset_credits:
        source_notes.append("Private reset-credit endpoint disabled")
    rate_limits = classify_codex_rate_limit_windows(app_rate_limits or jsonl_rate_limits)
    if not app_rate_limits:
        source_notes.append("Codex rate limits from session JSONL" if jsonl_rate_limits else "Codex rate_limits not found")
    if (rate_limits or {}).get("unclassified_windows"):
        source_notes.append("Unrecognized Codex rate-limit window ignored")

    credit_source = (rate_limits or {}).get("credits") or jsonl_credits
    balance = "n/a"
    has_credits = "n/a"
    if isinstance(credit_source, dict):
        raw_balance = credit_source.get("balance")
        balance = str(raw_balance) if raw_balance is not None else "0"
        has_credits = "yes" if credit_source.get("has_credits") else "no"
    reset_display = reset_credit_display(supported_reset_credits, private_reset_credits, tz)

    latest_month = (monthly.get("monthly") or [])[-1] if monthly.get("monthly") else {}
    session_counts = sessions_summary(sessions, now, week_start)

    plan = (rate_limits or {}).get("plan_type")
    display_limits = codex_dashboard_limit_cards(rate_limits, tz, now)
    activity_rows, activity_scope = codex_activity_rows(account_usage, rows, today)
    account_summary = {
        key: value
        for key, value in (account_usage or {}).items()
        if key in {"lifetime", "peak_day", "current_streak", "longest_streak"}
    }

    return {
        "ok": not errors,
        "kind": "codex",
        "title": usage_title("Codex", plan, title),
        "local_date": now.strftime("%Y-%m-%d"),
        "local_time": now.strftime("%H:%M"),
        "local_datetime": now.strftime("%Y-%m-%d %H:%M"),
        "refresh": "15m",
        "cost_basis": "Token Cost*",
        "source": "ccusage + Codex account + local sessions",
        "errors": errors[:3],
        "plan": str(plan or "n/a"),
        "limit_id": str((rate_limits or {}).get("limit_id") or "n/a"),
        "limit_name": str((rate_limits or {}).get("limit_name") or (rate_limits or {}).get("limit_id") or "Codex"),
        "limits": display_limits,
        "credits": {
            "has_credits": has_credits,
            "balance": balance,
            **reset_display,
        },
        "usage": {
            "five_hour": unknown_cost(usage_card("Last 5h", five_hour_metrics, five_hour_events)),
            "today": usage_card("Today", today_metrics),
            "yesterday": usage_card("Yesterday", yesterday_metrics),
            "week": usage_card("Last 7d", week_metrics),
            "month": usage_card("This month", month_metrics),
            "all": usage_card("All time", all_metrics),
        },
        "sessions": session_counts,
        "account_usage": account_summary,
        "activity_scope": activity_scope,
        "models": model_totals_codex(seven_rows),
        "daily_bars": daily_bars(activity_rows, now),
        "streak": streak(rows, now),
        "trend": trend_label(today_metrics, yesterday_metrics),
        "month_label": str(latest_month.get("month") or now.strftime("%Y-%m")),
        "notes": source_notes,
    }


def build_claude(
    ccusage_bin: str,
    tz_name: str,
    tz: ZoneInfo,
    claude_root: Path,
    offline: bool,
    statusline_cache: Path,
    claude_bin: str | None,
    statusline_max_age_minutes: int = DEFAULT_CLAUDE_STATUSLINE_MAX_AGE_MINUTES,
    title: str | None = DEFAULT_CLAUDE_TITLE,
    enable_oauth_usage: bool = False,
    enable_oauth_refresh: bool = False,
    oauth_refresh_stamp: Path | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    now = datetime.now(tz)
    today = now.date().isoformat()
    yesterday = (now.date() - timedelta(days=1)).isoformat()
    week_start = now - timedelta(days=(now.weekday() + 1) % 7)
    month_start_date = now.strftime("%Y-%m-01")

    daily = ccusage(ccusage_bin, ["claude", "daily"], tz_name, offline, errors)
    weekly = ccusage(ccusage_bin, ["claude", "weekly"], tz_name, offline, errors)
    monthly = ccusage(ccusage_bin, ["claude", "monthly"], tz_name, offline, errors)
    session = ccusage(ccusage_bin, ["claude", "session"], tz_name, offline, errors)
    blocks = ccusage(ccusage_bin, ["claude", "blocks", "--active"], tz_name, offline, errors)
    rows = daily_rows(daily)
    sessions = session.get("sessions") or []
    if not isinstance(sessions, list):
        sessions = []

    active_blocks = blocks.get("blocks") or []
    active_block = active_blocks[0] if active_blocks else {}
    block_tokens = {
        "inputTokens": float(((active_block.get("tokenCounts") or {}).get("inputTokens")) or 0),
        "outputTokens": float(((active_block.get("tokenCounts") or {}).get("outputTokens")) or 0),
        "reasoningOutputTokens": 0.0,
        "cacheCreationTokens": float(((active_block.get("tokenCounts") or {}).get("cacheCreationInputTokens")) or 0),
        "cacheReadTokens": float(((active_block.get("tokenCounts") or {}).get("cacheReadInputTokens")) or 0),
        "totalTokens": float(active_block.get("totalTokens") or 0),
        "costUSD": float(active_block.get("costUSD") or 0),
    }

    today_metrics = normalize_metrics(row_for_date(rows, today))
    yesterday_metrics = normalize_metrics(row_for_date(rows, yesterday))
    month_rows = rows_from_date(rows, month_start_date, today)
    seven_rows = rows_from_date(rows, (now.date() - timedelta(days=6)).isoformat(), today)
    week_metrics = aggregate(seven_rows)
    month_metrics = aggregate(month_rows)
    all_metrics = normalize_metrics(daily.get("totals") or {})
    reset_dt = parse_dt(active_block.get("endTime"))
    actual_dt = parse_dt(active_block.get("actualEndTime"))

    stats = {}
    stats_path = claude_root / "stats-cache.json"
    try:
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - stats are useful but optional.
        warnings.append(f"Claude stats-cache unavailable: {safe_exception(exc)}")
        stats = {}

    session_counts = sessions_summary(sessions, now, week_start)

    today_activity = {}
    for item in stats.get("dailyActivity") or []:
        if item.get("date") == today:
            today_activity = item
            break

    latest_week = {}
    for row in weekly.get("weekly") or []:
        if row.get("week") == week_start.date().isoformat():
            latest_week = row
            break
    if not latest_week and weekly.get("weekly"):
        latest_week = weekly.get("weekly")[-1]

    latest_month = (monthly.get("monthly") or [])[-1] if monthly.get("monthly") else {}
    burn = active_block.get("burnRate") or {}
    projection = active_block.get("projection") or {}
    statusline = load_claude_statusline_cache(
        statusline_cache,
        warnings,
        now,
        tz,
        statusline_max_age_minutes,
    )
    statusline_limits = valid_claude_statusline_limits(
        statusline.get("rate_limits") if isinstance(statusline.get("rate_limits"), dict) else {},
        now,
        bool(statusline.get("stale")),
    )
    oauth_notes: list[str] = []
    # Refresh only runs inside the OAuth usage path. Say so, rather than letting
    # the flag look effective while doing nothing. This leads the note list so it
    # survives the single-note compaction used for webhook payloads.
    misconfiguration_notes = (
        ["Claude OAuth refresh ignored without Claude OAuth usage enabled"]
        if enable_oauth_refresh and not enable_oauth_usage
        else []
    )
    oauth_limits, oauth_subscription = (
        read_claude_oauth_usage(
            claude_root,
            oauth_notes,
            claude_bin=claude_bin,
            enable_refresh=enable_oauth_refresh,
            refresh_stamp=oauth_refresh_stamp,
        )
        if enable_oauth_usage
        else (None, None)
    )
    subscription_notes: list[str] = []
    cli_subscription = read_claude_subscription_type(claude_bin, subscription_notes)
    if cli_subscription:
        subscription_type = cli_subscription
    elif oauth_subscription:
        subscription_type = oauth_subscription
        subscription_notes.append("Claude subscription from secure OAuth credentials")
    else:
        subscription_type = None
    if oauth_limits:
        warnings = [warning for warning in warnings if not warning.startswith("Claude statusline cache")]
        effective_limits = oauth_limits
        limit_note = "Live limits from Claude Code OAuth usage"
        limit_source = "Claude Code OAuth usage"
        limit_age = "live"
        usage_auth = "live"
    elif statusline_limits and statusline.get("stale"):
        effective_limits = statusline_limits
        limit_note = f"Stale Claude statusLine only supplies reset times ({statusline.get('captured_age', 'n/a')})"
        limit_source = str(statusline.get("source") or "Claude Code statusLine stdin")
        limit_age = str(statusline.get("captured_age") or "n/a")
        usage_auth = claude_usage_auth(oauth_notes, "stale line")
    elif statusline_limits:
        effective_limits = statusline_limits
        limit_note = f"Limit % from Claude Code statusLine ({statusline.get('captured_age', 'n/a')})"
        limit_source = str(statusline.get("source") or "Claude Code statusLine stdin")
        limit_age = str(statusline.get("captured_age") or "n/a")
        usage_auth = "statusLine"
    elif statusline.get("stale"):
        effective_limits = {}
        limit_note = (
            f"Claude statusLine cache stale ({statusline.get('captured_age', 'n/a')}); "
            "using ccusage block fallback"
        )
        limit_source = str(statusline.get("source") or "Claude Code statusLine stdin")
        limit_age = str(statusline.get("captured_age") or "n/a")
        usage_auth = claude_usage_auth(oauth_notes, "unavailable")
    else:
        effective_limits = {}
        limit_note = "Claude statusLine rate_limits pending"
        limit_source = str(statusline.get("source") or "Claude Code statusLine stdin")
        limit_age = str(statusline.get("captured_age") or "n/a")
        usage_auth = claude_usage_auth(oauth_notes, "pending")

    scoped_item = effective_limits.get("scoped") if isinstance(effective_limits.get("scoped"), dict) else {}
    scoped_label = str(scoped_item.get("label") or "Scoped")
    source = "ccusage + Claude OAuth usage + ~/.claude stats" if oauth_limits else "ccusage + Claude statusLine + ~/.claude stats"

    return {
        "ok": not errors,
        "kind": "claude",
        "title": usage_title("Claude Code", subscription_type, title),
        "local_date": now.strftime("%Y-%m-%d"),
        "local_time": now.strftime("%H:%M"),
        "local_datetime": now.strftime("%Y-%m-%d %H:%M"),
        "refresh": "15m",
        "cost_basis": "Token Cost*",
        "source": source,
        "errors": errors[:3],
        "warnings": warnings[:3],
        "plan": subscription_type or "n/a",
        "limits": {
            "primary": statusline_limit_card(
                effective_limits,
                "five_hour",
                "Session",
                "5h",
                tz,
                reset_dt,
                now,
            ),
            "weekly": statusline_limit_card(effective_limits, "weekly", "Weekly all", "7d", tz, now=now),
            "scoped": statusline_limit_card(effective_limits, "scoped", scoped_label, "7d", tz, now=now),
        },
        "statusline": {
            "captured_age": limit_age,
            "source": limit_source,
        },
        "auth": {"usage": usage_auth},
        "block": {
            "active": bool(active_block),
            "started": local_time(parse_dt(active_block.get("startTime")), tz),
            "updated": local_time(actual_dt, tz),
            "reset": local_time(reset_dt, tz),
            "remaining": str(int(projection.get("remainingMinutes") or 0)),
            "burn_tokens_min": short_number(burn.get("tokensPerMinute") or 0),
            "burn_cost_hr": money(burn.get("costPerHour") or 0),
            "projected_tokens": short_number(projection.get("totalTokens") or 0),
            "projected_cost": money(projection.get("totalCost") or 0),
            "models": ", ".join(active_block.get("models") or [])[:60],
        },
        "usage": {
            "five_hour": usage_card("Active block", block_tokens, int(active_block.get("entries") or 0)),
            "today": usage_card("Today", today_metrics, int(session_counts.get("today") or 0)),
            "yesterday": usage_card("Yesterday", yesterday_metrics),
            "week": usage_card("Last 7d", week_metrics),
            "month": usage_card("This month", month_metrics),
            "all": usage_card("All time", all_metrics),
        },
        "sessions": session_counts,
        "activity_scope": "Local activity",
        "activity": {
            "messages_today": int(today_activity.get("messageCount") or 0),
            "tools_today": int(today_activity.get("toolCallCount") or 0),
            "messages_all": int(stats.get("totalMessages") or 0),
            "sessions_cache_all": int(stats.get("totalSessions") or 0),
            "stats_cache_date": str(stats.get("lastComputedDate") or "n/a"),
        },
        "models": model_totals_claude(seven_rows),
        "daily_bars": daily_bars(rows, now),
        "streak": streak(rows, now),
        "trend": trend_label(today_metrics, yesterday_metrics),
        "week_label": str(latest_week.get("week") or week_start.date().isoformat()),
        "month_label": str(latest_month.get("month") or now.strftime("%Y-%m")),
        "notes": [
            *misconfiguration_notes,
            limit_note,
            *oauth_notes,
            "Costs are API-rate estimates calculated from tokens, not subscription charges",
            "ccusage blocks provides active 5h block reset and burn rate",
            *subscription_notes,
        ],
    }


def atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=True, sort_keys=True, indent=2)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(payload)
        handle.write("\n")
        temp_name = handle.name
    os.replace(temp_name, path)


def collector_error_feed(kind: str, title: str, tz: ZoneInfo, exc: BaseException) -> dict[str, Any]:
    now = datetime.now(tz)
    return {
        "ok": False,
        "kind": kind,
        "title": title,
        "local_date": now.strftime("%Y-%m-%d"),
        "local_time": now.strftime("%H:%M"),
        "local_datetime": now.strftime("%Y-%m-%d %H:%M"),
        "refresh": "15m",
        "errors": [safe_exception(exc)],
    }


def build_feed_safely(kind: str, title: str, tz: ZoneInfo, builder) -> dict[str, Any]:
    try:
        return builder()
    except Exception as exc:  # noqa: BLE001 - publish a visible per-feed failure.
        return collector_error_feed(kind, title, tz, exc)


def main() -> int:
    args = parse_args()
    tz = ZoneInfo(args.timezone)
    output_dir = Path(args.output_dir).expanduser()
    offline = bool(args.offline)

    try:
        ccusage_bin = ccusage_path(args.ccusage_bin)
    except FileNotFoundError as exc:
        now = datetime.now(tz)
        error_feed = {
            "ok": False,
            "local_datetime": now.strftime("%Y-%m-%d %H:%M"),
            "errors": [str(exc)],
        }
        atomic_write(output_dir / "codex.json", {"kind": "codex", **error_feed})
        atomic_write(output_dir / "claude.json", {"kind": "claude", **error_feed})
        return 2

    codex_bin = optional_executable(args.codex_bin, "codex")
    claude_bin = optional_executable(args.claude_bin, "claude")
    codex = build_feed_safely(
        "codex",
        args.codex_title or "Codex Usage",
        tz,
        lambda: build_codex(
            ccusage_bin,
            args.timezone,
            tz,
            Path(args.codex_root).expanduser(),
            offline,
            codex_bin,
            bool(args.enable_codex_private_reset_credits),
            args.codex_title,
        ),
    )
    claude = build_feed_safely(
        "claude",
        args.claude_title or "Claude Code Usage",
        tz,
        lambda: build_claude(
            ccusage_bin,
            args.timezone,
            tz,
            Path(args.claude_root).expanduser(),
            offline,
            Path(args.claude_statusline_cache).expanduser(),
            claude_bin,
            int(args.claude_statusline_max_age_minutes),
            args.claude_title,
            bool(args.enable_claude_oauth_usage),
            bool(args.enable_claude_oauth_refresh),
            output_dir / CLAUDE_OAUTH_REFRESH_STAMP_NAME,
        ),
    )
    index = {
        "ok": bool(codex.get("ok")) and bool(claude.get("ok")),
        "generated_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M"),
        "feeds": ["codex.json", "claude.json"],
        "sources": [
            "ccusage",
            "Codex app-server",
            "Claude OAuth usage",
            "Claude statusLine",
            "~/.codex/sessions",
            "~/.claude/stats-cache.json",
        ],
    }
    atomic_write(output_dir / "codex.json", codex)
    atomic_write(output_dir / "claude.json", claude)
    atomic_write(output_dir / "index.json", index)
    return 0 if index["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
