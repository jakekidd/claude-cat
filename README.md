# claude-cat

> Too many Claudes in the kitchen? Keep tabs on your litter.

A 1-bit pixel art cat companion for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) that curls up in your terminal and reacts to what your AI is doing in real time.

## Install

```bash
python3 -m pip install claude-cat
claude-cat install
```

`install` adds [hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) to `~/.claude/settings.json` so Claude Code sends events to the cat. Also available as `clat`.

## Usage

Run in a side terminal (tmux split, separate tab, etc.):

```bash
clat
```

In litter mode (default), every active Claude Code session gets its own cat. Each cat shows what its Claude is up to -- reading, cooking, thinking -- and reacts to events with brief expressions.

```bash
clat --target <session_id>    # watch one specific session
clat --tmux-ccm               # dashboard: CCM on top, litter below
```

## Commands

| Command | Description |
|---|---|
| `clat` | Litter mode (all sessions) |
| `clat --target <id>` | Single cat for one session |
| `clat --tmux-ccm` | Launch CCM + litter in tmux |
| `clat --sprite <name\|path>` | Use a custom sprite |
| `clat install` | Set up Claude Code hooks |
| `clat uninstall` | Remove hooks |
| `clat --demo` | Preview all states and reactions |
| `clat list-sprites` | Show available sprites |

## States

Each state has its own animated face. The cat's state tracks what Claude is actually doing.

| State | Tools | Animation |
|---|---|---|
| idle | (none) | looking around, blinking, napping after 2min |
| reading | Read, Grep, Glob | scanning eyes |
| cooking | Edit, Write, Bash, Skill | focused, heads-down |
| browsing | WebFetch, WebSearch | scanning (online) |
| thinking | Agent, SubagentStart | contemplative |

## Reactions

Brief face flashes from events. The cat holds the expression, then goes back to its state animation.

| Reaction | Trigger | Hold |
|---|---|---|
| happy | task complete | 4s |
| error | tool failure | 4s |
| surprised | waking up | 0.5s |
| interrupted | went quiet mid-task | 10s |

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

## How it works

1. `clat install` adds hooks to Claude Code settings
2. Hooks fire on tool use, prompts, errors, and completion
3. Hook mode writes a session-specific state file to the OS temp directory
4. The litter process scans for session files and renders a cat for each
5. Each cat independently animates, blinks, and reacts to its session's events

Zero dependencies. Runs on Python 3.9+.

## Development

```bash
python3 src/claude_cat/__main__.py           # run directly
python3 src/claude_cat/__main__.py --demo    # preview states + reactions
python3 edit-sprite.py                       # sprite editor
python3 view-sprite.py                       # quick preview
python3 -m pip install -e .                  # editable install
```

## Uninstall

```bash
clat uninstall
python3 -m pip uninstall claude-cat
```

## License

MIT
