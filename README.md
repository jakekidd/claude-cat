# claude-cat

Not to be confused with [cat(1)](https://man7.org/linux/man-pages/man1/cat.1.html).

> Too many Claudes in the kitchen? Keep tabs on your litter.

A 1-bit pixel art cat companion for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that curls up in your terminal and reacts to what your AI is doing in real time.

**claude-cat makes zero network requests.** All network activity in a `clat code` session comes exclusively from the wrapped Claude Code process. claude-cat only reads local files (transcripts, temp state files) and writes to your terminal. Session data is cached locally in `~/.claude-cat/` and regularly pruned. No telemetry, no external calls, no additional trust surface beyond Anthropic's own CLI.

## Install

```bash
pip install claude-cat
clat install
```

`install` adds [hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) to `~/.claude/settings.json` so Claude Code sends events to the cat. Also available as `clat`.

## Usage

### `clat code` -- launch Claude Code sessions

The primary way to work with Claude Code through claude-cat:

```bash
clat code                    # interactive session picker
clat code my-feature         # resume "my-feature" or create new
clat code <session-uuid>     # rescue a native session into a wrap
```

On first run, you'll see a session selector:
```
> (new)
  my-feature        contracts  322 turns  51.6M tok
  ui-redesign       v2         45 turns   6.1M tok
```

Arrow keys to navigate, Enter to select. Picking `(new)` prompts for a session name (with a random cat name as default). Sessions are stored in a local registry and auto-resume by name.

### `clat` -- monitor all sessions

Run in a side terminal (tmux split, separate tab, etc.):

```bash
clat
```

In litter mode (default), every active Claude Code session gets its own cat. Each cat shows what its Claude is up to -- reading, cooking, thinking -- and reacts to events with brief expressions.

Features:
- Burn rate status bar (tokens today, tok/m, $/m, total, reset time)
- Per-cat burn rate on project line
- State indicator dots (green = working, orange = needs help, red = idle)
- Permission prompt widget (Y/A/N to respond from litter for wrapped sessions)
- Graveyard (top 5 cats by tokens, persistent leaderboard)
- Context bar (vertical fill indicator, auto-detects 1M context models)

```bash
clat --target <session_id>   # watch one specific session
clat --tmux-ccm              # dashboard: CCM on top, litter below
clat --rename <name-or-id>   # rename a session in registry
```

## Commands

| Command | Description |
|---|---|
| `clat code` | Launch Claude Code (session picker) |
| `clat code my-feature` | Resume or create named session |
| `clat` | Litter mode (all sessions) |
| `clat --target <id>` | Single cat for one session |
| `clat --rename <name>` | Rename a session |
| `clat --tmux-ccm` | Launch CCM + litter in tmux |
| `clat --sprite <name\|path>` | Use a custom sprite |
| `clat install` | Set up Claude Code hooks |
| `clat uninstall` | Remove hooks |
| `clat --demo` | Preview all states and reactions |
| `clat list-sprites` | Show available sprites |

## States

Each state has its own animated face. The cat's state tracks what Claude is actually doing.

| State | Tools | Indicator |
|---|---|---|
| idle | (none) | red square, looking around, napping after 10min |
| reading | Read, Grep, Glob | green dot, scanning eyes |
| cooking | Edit, Write, Bash, Skill | green dot, focused |
| browsing | WebFetch, WebSearch | green dot, scanning |
| thinking | Agent, SubagentStart | green dot, contemplative |
| waiting... | PermissionRequest | orange square, needs help |
| compacting | PreCompact | light blue dot |

## Reactions

Brief face flashes from events. The cat holds the expression, then goes back to its state animation.

| Reaction | Trigger | Hold |
|---|---|---|
| happy | task complete | 4s |
| womp womp | tool failure | 4s |
| surprised | waking up | 0.5s |
| interrupted | went quiet mid-task | 10s |

## How it works

1. `clat install` adds hooks to Claude Code settings
2. Hooks fire on tool use, prompts, errors, and completion
3. Hook writes a session-specific state file to `~/.claude-cat/state/`
4. The litter process scans state files and renders a cat for each
5. Each cat independently animates, blinks, and reacts to its session's events
6. `clat code` wraps Claude Code in a PTY for bidirectional control (permission responses, auto-approve)
7. Cats are identified by a stable wrapper ID, so `/clear` and session restarts don't lose your cat

### Local data

All local data lives in `~/.claude-cat/`:
- `registry.json` -- cat identity (name, color, approve mode, stats). Pruned after 30 days of inactivity.
- `graveyard.json` -- top 5 cats by total tokens (persistent leaderboard).
- `state/` -- ephemeral state files and `.out` tee files for active sessions.
- `logs/` -- per-cat and combined litter logs. Rotated at 1MB.

## Custom sprites

Sprites use an extended hex format (17 symbols -- not real hex, don't @ us). One character per terminal cell:

```
0 = empty    8 = ▘
1 = ▗        9 = ▚
2 = ▖        A = ▌
3 = ▄        B = ▙
4 = ▝        C = ▀
5 = ▐        D = ▜
6 = ▞        E = ▛
7 = ▟        F = █ (foreground)
I = inverse video (gap-free fill)
```

States have multiple frames with animation modes (`shuffle` or `loop`). Reactions are single frames with a hold duration. Filled areas use inverse video so the cat looks purrfect regardless of your terminal's line spacing.

### Contributing sprites

Drop your JSON in `src/claude_cat/sprites/` and open a PR. Run `python3 edit-sprite.py` to draw -- it has a brush palette, mirror mode for symmetric faces, frame management with play/pawse preview, and per-frame labels.

## Development

```bash
python3 src/claude_cat/__main__.py           # run directly
python3 src/claude_cat/__main__.py --demo    # preview states + reactions
python3 edit-sprite.py                       # sprite editor
python3 view-sprite.py                       # quick preview
python3 -m pip install -e .                  # editable install
```

Zero dependencies. Python 3.9+. macOS and Linux only.

## Uninstall

```bash
clat uninstall
pip uninstall claude-cat
rm -rf ~/.claude-cat
```

## License

MIT
