#!/usr/bin/env python3
"""Auto-continue Codex agents in Herdr when the model is unavailable.

Detects (any one of them):
  Selected model is at capacity. Please try a different model.
  stream disconnected before completion: Our servers are currently
  overloaded. Please try again later.

When a codex agent is idle/done/blocked/unknown and a banner is still
visible, waits with exponential backoff, then sends a continue prompt.

Retry scheduling is time-based per capacity episode. Terminal snapshots
are unstable across polls (spinners, redraws, status-line updates change
the tail even while the agent is settled — observed on macOS), so pane
content is never hashed to decide freshness; the banner substring plus
the agent's settled status are the only signals. A banner must stay gone
for MISS_RESET consecutive scans before the episode is considered over,
which tolerates single-poll flicker between snapshot sources.

Matching collapses whitespace on both sides, so a banner that the pane
word-wraps across lines still matches a single-line pattern.

Usage:
  herdr-codex-capacity-retry                 # watch all codex agents forever
  herdr-codex-capacity-retry --once          # single scan
  herdr-codex-capacity-retry w2V:p1 name     # only these targets
  herdr-codex-capacity-retry --dry-run
  herdr-codex-capacity-retry --prompt continue
  herdr-codex-capacity-retry --match "rate limit" --match "at capacity"
  herdr-codex-capacity-retry --min-wait 20 --max-wait 180 --interval 8
  herdr-codex-capacity-retry --verbose       # log skip/wait decisions too

HERDR_CAPACITY_MATCH overrides the patterns too; one pattern per line.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Trailing sentences ("Please try a different model." / "Please try again
# later.") are left out on purpose: the shortest unambiguous prefix is the
# least likely to be broken up by pane rendering.
DEFAULT_MATCHES = (
    "Selected model is at capacity",
    "stream disconnected before completion: Our servers are currently overloaded",
)
DEFAULT_PROMPT = "continue"
SETTLED = frozenset({"idle", "done", "blocked", "unknown"})
# Consecutive banner-free scans required before an episode is closed.
MISS_RESET = 3
# An active episode whose banner has not been seen for this long is stale
# (watcher was stopped, or the pane moved on while the agent was working);
# treat the next sighting as a fresh episode so it gets the initial wait.
STALE_AFTER_FLOOR = 600

VERBOSE = False


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def debug(msg: str) -> None:
    if VERBOSE:
        log(f"debug: {msg}")


def run_herdr(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["herdr", *args],
            check=check,
            text=True,
            capture_output=True,
        )
    except OSError as exc:
        # herdr binary missing/unreadable mid-run must not kill the watcher.
        return subprocess.CompletedProcess(
            ["herdr", *args], returncode=127, stdout="", stderr=str(exc)
        )


def herdr_json(args: list[str]) -> dict[str, Any] | None:
    proc = run_herdr(args)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        if err:
            log(f"herdr {' '.join(args)} failed: {err[:300]}")
        return None
    text = (proc.stdout or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        log(f"invalid JSON from herdr {' '.join(args)}: {exc}")
        return None
    if not isinstance(data, dict):
        log(f"unexpected JSON shape from herdr {' '.join(args)}")
        return None
    return data


def safe_key(target: str) -> str:
    return target.replace("/", "_").replace(":", "__")


def flatten(text: str) -> str:
    """Collapse every whitespace run to a single space.

    Panes wrap long banners across lines and indent the continuation, so
    the raw tail rarely contains the pattern verbatim.
    """
    return " ".join(text.split())


def find_match(haystack: str, patterns: list[str]) -> str | None:
    """First pattern present in the tail, or None."""
    flat = flatten(haystack)
    for pattern in patterns:
        if flatten(pattern) in flat:
            return pattern
    return None


@dataclass
class TargetState:
    first_seen_at: float = 0.0
    last_match_at: float = 0.0
    miss_count: int = 0
    hit_count: int = 0
    next_retry_at: float = 0.0

    @property
    def active(self) -> bool:
        return self.first_seen_at > 0

    @classmethod
    def load(cls, path: Path) -> "TargetState":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return cls()
            return cls(
                first_seen_at=float(data.get("first_seen_at", 0)),
                last_match_at=float(data.get("last_match_at", 0)),
                miss_count=int(data.get("miss_count", 0)),
                hit_count=int(data.get("hit_count", 0)),
                next_retry_at=float(data.get("next_retry_at", 0)),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "first_seen_at": self.first_seen_at,
                    "last_match_at": self.last_match_at,
                    "miss_count": self.miss_count,
                    "hit_count": self.hit_count,
                    "next_retry_at": self.next_retry_at,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


# In-process source of truth per target; disk is only persistence across
# restarts. Keeps backoff intact when the state dir becomes unwritable.
STATE_CACHE: dict[str, TargetState] = {}


def load_state(target: str, path: Path) -> TargetState:
    if target not in STATE_CACHE:
        STATE_CACHE[target] = TargetState.load(path)
    return STATE_CACHE[target]


def store_state(target: str, path: Path, state: TargetState) -> None:
    STATE_CACHE[target] = state
    try:
        state.save(path)
    except OSError as exc:
        log(f"warning: cannot persist state for {target}: {exc}")


def backoff_seconds(hit_count: int, min_wait: int, max_wait: int) -> int:
    exp = min(hit_count, 6)
    wait = min_wait * (2**exp)
    return min(wait, max_wait)


def list_codex_targets() -> list[str]:
    data = herdr_json(["agent", "list"])
    if not data:
        return []
    result = data.get("result")
    agents = result.get("agents") if isinstance(result, dict) else None
    if not isinstance(agents, list):
        return []
    targets: list[str] = []
    for agent in agents:
        if not isinstance(agent, dict) or agent.get("agent") != "codex":
            continue
        targets.append(agent.get("name") or agent.get("pane_id") or "")
    return [t for t in targets if t]


def get_agent(target: str) -> dict[str, Any] | None:
    data = herdr_json(["agent", "get", target])
    if not data:
        return None
    result = data.get("result")
    agent = result.get("agent") if isinstance(result, dict) else None
    return agent if isinstance(agent, dict) else None


def read_tail(target: str) -> str | None:
    """Tail of the pane, "" if readable but empty, None if unreadable.

    None means "no observation" and must not feed the miss debounce —
    a failing read is not evidence that the banner cleared.
    """
    readable = False
    for source, lines in (("detection", "60"), ("recent-unwrapped", "80")):
        proc = run_herdr(
            ["agent", "read", target, "--source", source, "--lines", lines]
        )
        if proc.returncode != 0:
            continue
        readable = True
        if proc.stdout:
            return proc.stdout
    return "" if readable else None


def send_continue(target: str, prompt: str, dry_run: bool) -> bool:
    if dry_run:
        log(f"DRY-RUN: would prompt {target} with: {prompt!r}")
        return True
    log(f"prompting {target}: {prompt!r}")
    proc = run_herdr(["agent", "prompt", target, prompt])
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        log(f"prompt failed for {target}: {err[:300]}")
        return False
    return True


def process_target(
    target: str,
    *,
    matches: list[str],
    prompt: str,
    min_wait: int,
    max_wait: int,
    dry_run: bool,
    state_dir: Path,
) -> None:
    agent = get_agent(target)
    if not agent:
        return
    if agent.get("agent") != "codex":
        return

    status = str(agent.get("agent_status") or "")
    pane = str(agent.get("pane_id") or "?")
    if status not in SETTLED:
        debug(f"{target}: status={status}, not settled; skip")
        return

    out = read_tail(target)
    if out is None:
        debug(f"{target}: pane unreadable; no observation this scan")
        return

    state_path = state_dir / f"{safe_key(target)}.json"
    state = load_state(target, state_path)
    now = time.time()

    stale_after = max(4 * max_wait, STALE_AFTER_FLOOR)
    if state.active and state.last_match_at and now - state.last_match_at > stale_after:
        debug(
            f"{target}: episode stale "
            f"(last match {int(now - state.last_match_at)}s ago); reset"
        )
        state = TargetState()
        store_state(target, state_path, state)

    hit = find_match(out, matches)
    if hit is None:
        if not state.active:
            return
        state.miss_count += 1
        if state.miss_count >= MISS_RESET:
            log(
                f"capacity banner cleared on {target} "
                f"after {state.hit_count} continue(s)"
            )
            store_state(target, state_path, TargetState())
        else:
            debug(
                f"{target}: banner not seen "
                f"({state.miss_count}/{MISS_RESET} before reset)"
            )
            store_state(target, state_path, state)
        return

    state.last_match_at = now
    state.miss_count = 0

    # Fresh episode: start a wait window before the first continue.
    if not state.active:
        wait = backoff_seconds(0, min_wait, max_wait)
        state.first_seen_at = now
        state.hit_count = 0
        state.next_retry_at = now + wait
        store_state(target, state_path, state)
        log(
            f"capacity hit on {target} (pane={pane} status={status} "
            f"match={hit!r}); wait {wait}s then continue"
        )
        return

    if now < state.next_retry_at:
        store_state(target, state_path, state)
        debug(
            f"{target}: banner still up, "
            f"{int(state.next_retry_at - now)}s until next continue"
        )
        return

    if send_continue(target, prompt, dry_run):
        state.hit_count += 1
        wait = backoff_seconds(state.hit_count, min_wait, max_wait)
        state.next_retry_at = now + wait
        store_state(target, state_path, state)
        log(
            f"continue sent to {target} (attempt={state.hit_count}); "
            f"next retry in {wait}s if still stuck"
        )
    else:
        wait = backoff_seconds(state.hit_count, min_wait, max_wait)
        state.next_retry_at = now + wait
        store_state(target, state_path, state)


def scan_once(args: argparse.Namespace, state_dir: Path) -> None:
    targets = list(args.targets) if args.targets else list_codex_targets()
    if not targets:
        log("no codex agents found")
        return
    for target in targets:
        try:
            process_target(
                target,
                matches=args.match,
                prompt=args.prompt,
                min_wait=args.min_wait,
                max_wait=args.max_wait,
                dry_run=args.dry_run,
                state_dir=state_dir,
            )
        except Exception as exc:  # keep watcher alive
            log(f"error processing {target}: {exc}")


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(
            f"warning: ignoring non-integer {name}={raw!r}, using {default}",
            file=sys.stderr,
        )
        return default


def env_matches(name: str = "HERDR_CAPACITY_MATCH") -> list[str] | None:
    """Patterns from the env var (one per line), or None if unset."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    return [line.strip() for line in raw.splitlines() if line.strip()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Auto-continue Herdr Codex agents on capacity/overload errors."
    )
    p.add_argument(
        "targets",
        nargs="*",
        help="Optional pane IDs or agent names (default: all codex agents)",
    )
    p.add_argument("--once", action="store_true", help="Scan once and exit")
    p.add_argument("--dry-run", action="store_true", help="Detect only, do not prompt")
    p.add_argument(
        "--verbose",
        action="store_true",
        default=os.environ.get("HERDR_CAPACITY_VERBOSE", "") not in ("", "0"),
        help="Also log skip/wait decisions (default via HERDR_CAPACITY_VERBOSE=1)",
    )
    p.add_argument(
        "--prompt",
        default=os.environ.get("HERDR_CAPACITY_PROMPT", DEFAULT_PROMPT),
        help=f"Text to submit (default: {DEFAULT_PROMPT!r})",
    )
    p.add_argument(
        "--match",
        action="append",
        metavar="TEXT",
        help=(
            "Substring that marks a capacity failure; repeat to watch several. "
            "Given patterns replace the built-in ones "
            "(also settable via HERDR_CAPACITY_MATCH, one per line)"
        ),
    )
    p.add_argument(
        "--min-wait",
        type=int,
        default=env_int("HERDR_CAPACITY_MIN_WAIT", 15),
        help="Seconds to wait after first hit before continue",
    )
    p.add_argument(
        "--max-wait",
        type=int,
        default=env_int("HERDR_CAPACITY_MAX_WAIT", 180),
        help="Max backoff seconds between continues",
    )
    p.add_argument(
        "--interval",
        type=int,
        default=env_int("HERDR_CAPACITY_INTERVAL", 8),
        help="Poll interval seconds",
    )
    args = p.parse_args(argv)
    if args.match is None:
        # CLI wins over env, env over the built-ins; blank env = no pattern,
        # which main() rejects rather than silently matching everything.
        from_env = env_matches()
        args.match = list(DEFAULT_MATCHES) if from_env is None else from_env
    return args


def main(argv: list[str] | None = None) -> int:
    global VERBOSE

    if not shutil.which("herdr"):
        log("missing dependency: herdr")
        return 1

    args = parse_args(argv)
    if not args.match or any(not m.strip() for m in args.match):
        log("refusing to run with an empty --match (it would match everything)")
        return 2
    VERBOSE = args.verbose
    state_dir = Path(
        os.environ.get(
            "HERDR_CAPACITY_STATE_DIR",
            str(Path.home() / ".local/state/herdr-codex-capacity-retry"),
        )
    )
    state_dir.mkdir(parents=True, exist_ok=True)

    log(
        f"watching Codex capacity errors ({len(args.match)} pattern(s): "
        + ", ".join(repr(m) for m in args.match)
        + f"; prompt={args.prompt!r} "
        f"interval={args.interval}s min_wait={args.min_wait}s "
        f"max_wait={args.max_wait}s dry_run={args.dry_run})"
    )

    if args.once:
        scan_once(args, state_dir)
        return 0

    try:
        while True:
            try:
                scan_once(args, state_dir)
            except Exception as exc:  # keep watcher alive
                log(f"scan failed: {exc}")
            time.sleep(max(1, args.interval))
    except KeyboardInterrupt:
        log("interrupted; exiting")
        return 0


if __name__ == "__main__":
    sys.exit(main())
