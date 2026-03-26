# TODO

## v0.2.0 (done)

### ~~Litter mode~~ DONE
Auto-discovers all active sessions, groups by project, renders stacked with labels/stats.

### ~~Cat registry~~ DONE
`~/.claude-cat/registry.json` — persistent name/color/stats per session_id. 30-day auto-prune. Color diversity on 'c' key.

### ~~Always-on logging~~ DONE
Per-cat + combined litter log in `~/.claude-cat/logs/`. Last log line shown per cat in UI. `--debug` adds stderr output.

### ~~clat code (pty wrapper)~~ DONE
`clat code` launches Claude Code inside a pty. Session selector on boot (arrow keys, enter). Auto-resume by name from registry. UUID rescue for native sessions. `--name` passthrough to Claude Code.

### ~~Session naming~~ DONE
Interactive session selector with existing sessions or (new). Names stored in registry, shown in litter. Sanitized to lowercase-hyphenated. Cat names as fallback for unnamed sessions.

### ~~Permission prompt widget~~ DONE
Hook saves tool_input on PermissionRequest. Litter shows prompt widget with center-truncated content. Y/A/N input, queue for multiple cats. Response file mechanism for wrapped sessions.

### ~~Graveyard (rip)~~ DONE
Persistent top-5 leaderboard by tokens. Deduped by name. Alive cats hidden. Sorted by total tokens.

### ~~Burn rate status bar~~ DONE
Top line: tokens today, tok/m, $/m, total, reset time. Per-cat tok/m on project line.

### ~~State indicator dots~~ DONE
Green dot = working. Light blue = compacting. Orange square = waiting (needs help). Red square = idle/sleeping/dead. "waiting..." label override on permission pending.

### ~~Idle gaze + blinks~~ DONE
Neighbor-weighted gaze drift. Escalating long-blink probability. Hold direction for 1-3 ticks.

### ~~Context bar model detection~~ DONE
Detects model from transcript (opus-4-6 = 1M context). Fixes bar showing 0% for large-context models.

### ~~Wrapped session indicator~~ DONE
Star (*) next to name for sessions launched via clat code. Registry tracks wrapped flag.

### ~~Subagent tracking~~ DONE
subagent_depth tracked per cat (SubagentStart/SubagentStop). PermissionRequest events skipped when depth > 0 (prevents broken Y/A/N prompt for subagent permissions that can't be routed).

### ~~Auto-approve modes~~ DONE
Three modes: manual (default), guarded (auto-approve safe in-repo reads/writes), automatic (always approve). M/G/A keys toggle on selected cat. Badge on cat name: [A] green, [G] yellow. GUARDED_BLACKLIST for dangerous commands.

### ~~Cat selector + input routing~~ DONE
Tab/arrows to cycle cats, Enter for input mode (type text, Enter to send, Esc to cancel). Response file mechanism sends text to wrapper stdin. Input buffer rendered in prompt widget area.

### ~~Dumb wrapper refactor~~ DONE
Wrapper only does: stdin/stdout passthrough, stdout tee to .out file (rolling 4KB, debounced 200ms), response file -> stdin injection, interrupt detection (Escape + "Interrupted"). All parsing (spinner/error/compaction/idle detection) moved to litter. Update clat only, not sessions.

### ~~State machine fixes~~ DONE
Fixed disappearing cats (WrapperState events cleared cwd), dead cat rendering gap (30s invisible between death and graveyard), stdout idle thrashing (now requires both spinner silence AND hook event silence), wrapper heartbeat (15s .out file touch).

## v0.3.0 (in progress)

### State machine stability
Known issues:
- False "compacting" detection: stdout parser matches "ompact" in any text (e.g. tool output mentioning compaction). Should only trigger on actual Claude Code compaction messages.
- Permission response flaky: Y/A/N responses via response files work most of the time but occasionally not picked up. Timing issue between litter writing and wrapper polling (100ms select loop).
- State transitions need validation against actual Claude Code output patterns. Need diagnostic tooling (see below).

### Diagnostic logging mode
Dense per-cat logging that captures every state change with full context: stdout chunk that triggered it, hook event that triggered it, previous state, new state, all timestamps. Enables post-hoc debugging of state mismatches without reproducing. Log format should be machine-parseable for automated analysis.

### Model + effort display
Show model type (opus/sonnet/haiku) and effort level (low/medium/high/max) per cat. Model already detected from transcript. Effort in `~/.claude/settings.json` (global, not per-session). Poll settings file periodically.

### Tmux pane jumping
Detect which tmux pane each session lives in (match by PID or cwd). Arrow keys to select a cat, enter to switch to that pane. Makes clat a session switcher, not just a display.

### Group mode (`--group`)
Filter litter to one project. `clat --group v2` shows only v2 cats.

### Compacting mood (broken hooks, needs workaround)
PreCompact/PostCompact hooks registered but Claude Code never fires them. Workaround: detect compaction from transcript (type="summary" entries, timestamp gaps). File Claude Code bug.

### Stats lerp animation
Linearly interpolate token/cost/ctx values over ~1s for smooth counting-up effect.

### Substates (tool-level granularity)
Show "reading/Grep" vs "reading/Read" etc. More granular labels from tool name.

### More overlays
Thought bubble during Agent/subagent work. Heart on positive interactions. Musical notes on long idle.

## Backlog

### Read-only observer mode
Allow multiple clat instances that only monitor (no response file writing). Main instance holds the lock and handles interactions. Observers get a `--watch` flag.

### Targeted single-cat mode
`clat --focus <name>` shows one cat full-screen with expanded stats, full log tail, detailed state history. Read-only (no response routing).

### Stream stdout to selected cat display
Show Claude's actual output (not tool results, just assistant text) streamed live in the cat's display area. When a user prompt is active, temporarily overwrite with the prompt widget. Requires filtering .out file for assistant text vs tool output noise.

### Cat personality titles
Derived from tool usage ratios. "bookworm" (mostly reads), "chaos gremlin" (lots of errors), "crazy cat lady" (many agent spawns).

### Cat growth
Starts small, gets bigger/fatter with more tokens consumed.

### Achievements
Little badges: "$50 club", "100 turns", "marathon" (>8h), "speed demon" (>500 tok/m).

### Nighttime mode
After midnight, idle cats get sleepier faster. Half-closed eyes by default.

### Inter-cat interactions
Two cats in same project occasionally look at each other. One finishes, other does surprised reaction.

### Custom overlay art in sprite JSON
Move overlays from __main__.py to sprite JSON so sprite authors define their own.

### PyPI publish
Package is ready (pyproject.toml, console_scripts). Needs PyPI account.

### CI/CD for Claude Code breakage detection
GitHub Actions cron (daily/weekly) that installs latest claude-code, fires mock hook events through `clat --hook`, asserts state files are written correctly. Tests hook config schema hasn't changed. Opens a GitHub issue automatically on failure. No API keys needed for hook contract tests. Optional: add Anthropic API key secret for real session integration tests (`claude -p "hello" --print`). Can't catch: hook event name changes without a real session, terminal escape sequence changes, permission prompt rendering changes.

## Sprite art

### Polish all moods
Idle is hand-drawn and great. Other moods could use the same love.

### Community sprites directory
`src/claude_cat/sprites/` accepts PRs. JSON format documented in README.

## Editor improvements
- Undo/redo (history stack)
- Eyedropper tool
- Mirror mode (symmetric faces)
- Resize grid
