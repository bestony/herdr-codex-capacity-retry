# herdr-codex-capacity-retry

Auto-continue [Herdr](https://herdr.dev) Codex agents when the model is at capacity or the
upstream servers are overloaded.

[中文文档](./README.zh-CN.md)

Codex sometimes stops mid-task with a banner like:

```
Selected model is at capacity. Please try a different model.
```

```
stream disconnected before completion: Our servers are currently overloaded.
Please try again later.
```

The agent is then idle and waits for a human. This watcher polls your Codex panes, notices
those banners, waits with exponential backoff, and submits a `continue` prompt so the run
picks up on its own. Polling is [adaptive](#adaptive-poll-cadence): fast while an agent is
stuck, slow when no Codex agent is running at all.

## Requirements

- [`herdr`](https://herdr.dev) on `PATH` (the script shells out to `herdr agent …`)
- Python 3.8+ — standard library only, no dependencies
- At least one running Codex agent in a Herdr session

## Install

Downloads the script into `~/.local/bin` and makes it executable:

```bash
mkdir -p ~/.local/bin \
  && curl -fsSL https://raw.githubusercontent.com/bestony/herdr-codex-capacity-retry/main/herdr-codex-capacity-retry.py \
       -o ~/.local/bin/herdr-codex-capacity-retry \
  && chmod +x ~/.local/bin/herdr-codex-capacity-retry
```

Make sure `~/.local/bin` is on your `PATH` (add to `~/.zshrc` if `which herdr-codex-capacity-retry`
comes up empty):

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Verify:

```bash
herdr-codex-capacity-retry --help
```

**Upgrade** — re-run the install command (it overwrites the file).

**Uninstall**:

```bash
rm -f ~/.local/bin/herdr-codex-capacity-retry
rm -rf ~/.local/state/herdr-codex-capacity-retry
```

## Usage

```bash
herdr-codex-capacity-retry [TARGET ...] [OPTIONS]
```

```bash
# Watch every Codex agent forever (Ctrl-C to stop)
herdr-codex-capacity-retry

# Single scan, then exit — good for cron or a manual check
herdr-codex-capacity-retry --once

# Only watch specific panes / agent names
herdr-codex-capacity-retry w2V:p1 backend-agent

# Detect and log, but never actually prompt
herdr-codex-capacity-retry --dry-run

# Send something other than "continue"
herdr-codex-capacity-retry --prompt "keep going"

# Watch a different set of error banners (replaces the built-ins)
herdr-codex-capacity-retry --match "rate limit" --match "at capacity"

# Tune the backoff between retries
herdr-codex-capacity-retry --min-wait 20 --max-wait 90

# Tune the adaptive poll cadence (stuck / healthy / no codex agent at all)
herdr-codex-capacity-retry --busy-interval 5 --interval 10 --idle-interval 60

# Cap how many targets are scanned / continued in parallel per poll
herdr-codex-capacity-retry --workers 8

# Log skip/wait decisions too, to see why nothing is happening
herdr-codex-capacity-retry --verbose
```

## Arguments

| Argument | Description |
| --- | --- |
| `TARGET ...` | Optional pane IDs (`w2V:p1`) or agent names. When omitted, every agent reported by `herdr agent list` with `agent == "codex"` is watched. Non-Codex targets are skipped even if named explicitly. |

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--once` | off | Run one scan and exit instead of looping. |
| `--dry-run` | off | Detect and log everything, but log `DRY-RUN: would prompt …` instead of submitting. Backoff state still advances. |
| `--verbose` | off | Also log skip/wait decisions (`not settled`, `Ns until next continue`, `pane unreadable`, …). |
| `--prompt TEXT` | `continue` | Text submitted to the agent when a retry fires. |
| `--match TEXT` | see below | Substring that marks a capacity failure. Repeatable. **Any value given replaces the built-in patterns** rather than adding to them. |
| `--min-wait N` | `15` | Seconds to wait after the first sighting before the first `continue`, and the base of the exponential backoff. |
| `--max-wait N` | `60` | Upper bound for the backoff between retries. |
| `--interval N` | `10` | **active** poll interval: codex agents are alive and none is stuck. |
| `--busy-interval N` | `5` | **busy** poll interval: at least one target is inside a capacity episode. |
| `--idle-interval N` | `60` | **idle** poll interval: no codex agent exists at all. |
| `--workers N` | `8` | Max number of targets processed in parallel in one scan (pane reads and `continue` prompts fan out together). |

All three intervals are in seconds, have a floor of 1, and are ignored with `--once`. See
[Adaptive poll cadence](#adaptive-poll-cadence).

Built-in `--match` patterns:

- `Selected model is at capacity`
- `stream disconnected before completion: Our servers are currently overloaded`

The trailing sentences (`Please try a different model.` / `Please try again later.`) are left
out on purpose: the shortest unambiguous prefix is the least likely to be broken up by pane
rendering. Matching collapses every whitespace run on both sides, so a banner the pane
word-wraps across several lines still matches a single-line pattern.

## Environment variables

Every setting has an env fallback. Precedence is **CLI flag > env var > built-in default**.

| Variable | Maps to | Default |
| --- | --- | --- |
| `HERDR_CAPACITY_PROMPT` | `--prompt` | `continue` |
| `HERDR_CAPACITY_MATCH` | `--match` | built-ins — one pattern per line; blank lines ignored |
| `HERDR_CAPACITY_MIN_WAIT` | `--min-wait` | `15` |
| `HERDR_CAPACITY_MAX_WAIT` | `--max-wait` | `60` |
| `HERDR_CAPACITY_INTERVAL` | `--interval` | `10` |
| `HERDR_CAPACITY_BUSY_INTERVAL` | `--busy-interval` | `5` |
| `HERDR_CAPACITY_IDLE_INTERVAL` | `--idle-interval` | `60` |
| `HERDR_CAPACITY_WORKERS` | `--workers` | `8` |
| `HERDR_CAPACITY_VERBOSE` | `--verbose` | unset — any value other than empty or `0` enables it |
| `HERDR_CAPACITY_STATE_DIR` | — | `~/.local/state/herdr-codex-capacity-retry` |

A non-integer value for the numeric variables is ignored with a warning on stderr, and the
default is used.

Multi-line patterns via env:

```bash
export HERDR_CAPACITY_MATCH='Selected model is at capacity
Our servers are currently overloaded
rate limit'
```

Setting `HERDR_CAPACITY_MATCH` to an empty string is refused (it would match every pane) —
the script exits with code `2`.

## How it works

Each scan runs every target in parallel (up to `--workers`), so when several agents are due
for a `continue` in the same poll they are prompted together instead of one after another.

Per target:

1. `herdr agent get <target>` — skip anything that is not a Codex agent, and skip agents
   whose `agent_status` is not **settled**. Settled means `idle`, `done`, `blocked`, or
   `unknown`; a working agent is never interrupted.
2. `herdr agent read <target> --source detection --lines 60`, falling back to
   `--source recent-unwrapped --lines 80` — the pane tail. A read failure counts as *no
   observation* (it is not treated as "the banner cleared").
3. Look for any `--match` pattern in the whitespace-collapsed tail.
4. First sighting opens an **episode**: log the hit and arm a timer for `--min-wait` seconds.
   Nothing is sent yet — transient capacity blips resolve on their own.
5. Once the timer expires and the banner is still up, `herdr agent prompt <target> <prompt>`
   is submitted and the next wait doubles.

Backoff is `min(min_wait * 2^attempts, max_wait)`, with `attempts` capped at 6. With the
defaults that gives 15s → 30s → 60s → 60s → …

### Adaptive poll cadence

Polling every few seconds is wasted work when no Codex agent is even running, and too slow
when one is sitting on a capacity banner. So each scan tallies what it saw and the next sleep
is chosen from that tally:

| Tier | Condition | Default |
| --- | --- | --- |
| `busy` | At least one target is inside a capacity episode — banner up, waiting out the backoff, or inside the 3-scan clear debounce. | `--busy-interval`, 5s |
| `active` | Codex agents exist and none is stuck (includes agents that are currently working). | `--interval`, 10s |
| `idle` | No Codex agent at all — the scan can only ever cost one `herdr agent list` that says "nothing to do". | `--idle-interval`, 60s |

Tier changes are logged, so the cadence in effect is always visible:

```
[10:01:22] poll cadence -> idle (60s): no codex agents
[10:03:04] poll cadence -> active (10s): 2 codex agent(s) healthy
[10:07:41] capacity hit on w36:p1 (pane=%1 status=idle match='Selected model is at capacity'); wait 15s then continue
[10:07:41] poll cadence -> busy (5s): 1/2 codex agent(s) in a capacity episode
```

A target that cannot be read, or that raised an unexpected error mid-scan, counts toward
`busy` when it has an open episode: a missing observation is never taken as evidence that
things are fine. If a whole scan fails, the loop falls back to the `active` interval rather
than napping through a capacity episode.

Note that the cadence only controls how quickly the watcher *notices* things. How long a
stuck agent actually waits between retries is set by `--min-wait` / `--max-wait`, not by the
poll interval.

Retry scheduling is purely time-based per episode. Pane snapshots are deliberately never
hashed to decide freshness: spinners, redraws, and status-line updates change the tail even
while the agent is settled (observed on macOS), so the banner substring plus the settled
status are the only signals.

An episode closes only after the banner has been missing for **3 consecutive scans**, which
tolerates single-poll flicker between the two snapshot sources. An episode whose banner has
not been seen for `max(4 × max_wait, 600)` seconds is considered stale (watcher was stopped,
or the pane scrolled past it) and resets, so the next sighting gets the full initial wait
again instead of an inherited long backoff.

The watcher is designed to stay alive: a missing `herdr` binary mid-run, malformed JSON, an
unreadable pane, or an unexpected exception in one target is logged and the loop continues.

### State files

One JSON file per target under `HERDR_CAPACITY_STATE_DIR`, named after the target with `/`
and `:` escaped (`w2V:p1` → `w2V__p1.json`):

```json
{
  "first_seen_at": 1754880000.0,
  "last_match_at": 1754880042.0,
  "miss_count": 0,
  "hit_count": 2,
  "next_retry_at": 1754880102.0
}
```

Disk is only for persistence across restarts — the in-process cache is the source of truth,
so backoff survives a state directory that becomes unwritable (it logs a warning instead of
dying). Deleting the files resets all backoff state.

## Running it in the background

Quick and dirty:

```bash
nohup herdr-codex-capacity-retry > ~/.local/state/herdr-codex-capacity-retry/watch.log 2>&1 &
```

As a launchd agent on macOS — write `~/Library/LaunchAgents/dev.herdr.codex-capacity-retry.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>dev.herdr.codex-capacity-retry</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOUR_NAME/.local/bin/herdr-codex-capacity-retry</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/herdr-codex-capacity-retry.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/herdr-codex-capacity-retry.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/dev.herdr.codex-capacity-retry.plist
```

`PATH` matters: launchd does not inherit your shell environment, and the script needs to
find `herdr`.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Normal exit — `--once` finished, or the loop was interrupted with Ctrl-C |
| `1` | `herdr` not found on `PATH` |
| `2` | Empty `--match` / `HERDR_CAPACITY_MATCH` — refused because it would match everything |

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `missing dependency: herdr` | `herdr` is not on `PATH` for this process — common under launchd/cron. |
| `poll cadence -> idle: no codex agents` | `herdr agent list` shows no agent with `"agent": "codex"`, or your explicit targets are wrong. The watcher stays alive and keeps checking every `--idle-interval` seconds. |
| Banner is visible but nothing fires | Run with `--verbose`. `status=…, not settled` means Codex still looks busy; `pane unreadable` means `herdr agent read` failed. |
| It never matches your custom banner | Compare against the real pane tail: `herdr agent read <target> --source detection --lines 60`. Remember whitespace is collapsed, so pick a short single-line substring. |
| Retries fire too fast / too slow | Tune `--min-wait` and `--max-wait`. The interval flags only control polling granularity, not the gap between retries. |
| A new banner takes too long to be noticed | Lower `--idle-interval` (nothing running) or `--interval` (agents running). The tier in effect is in the last `poll cadence ->` line. |
| Want to confirm behaviour safely | `herdr-codex-capacity-retry --dry-run --verbose`. |

## License

[GPL-3.0](./LICENSE)
