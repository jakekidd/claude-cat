# claude-cat

NOTE: WIP, the actual 1-bit cat is an ugly placeholder at the moment.

A 1-bit pixel art cat that lives in your terminal and reacts to [Claude Code](https://docs.anthropic.com/en/docs/claude-code)'s activity in real time.

## Install

```bash
pip install claude-cat
claude-cat install
```

The `install` command adds [hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) to `~/.claude/settings.json` so Claude Code sends events to the cat.

## Usage

Run in a side terminal (tmux split, separate tab, etc.):

```bash
claude-cat
```

The cat reacts as Claude Code works -- reading files, editing code, searching, thinking, finishing tasks.

## Commands

| Command | Description |
|---|---|
| `claude-cat` | Start the cat display |
| `claude-cat install` | Set up Claude Code hooks |
| `claude-cat uninstall` | Remove hooks |
| `claude-cat --demo` | Preview all 7 expressions |
| `claude-cat --version` | Show version |

## Expressions

| Mood | Trigger |
|---|---|
| idle | Default state, blinks every few seconds |
| working | Tool use (Read, Edit, Bash, Grep, etc.) |
| happy | Task complete (Stop event) |
| error | Tool failure |
| surprised | Waking from sleep |
| sleeping | After 2 minutes idle |

## How it works

1. `claude-cat install` adds hooks to Claude Code settings
2. Hooks fire on tool use, errors, and completion, piping event data to `claude-cat --hook`
3. Hook mode writes a small JSON state file to the OS temp directory
4. The display process polls that file and updates the cat's expression

## Rendering

Sprites use Unicode quadrant block characters for 2x resolution in both axes. Each character cell is a 2x2 pixel grid, mapped to one of 16 block elements:

```
 ▘▝▀▖▌▞▛▗▚▐▜▄▙▟█ (space)
```

Sprites are defined as pixel bitmaps (`#`/`.`) in `src/claude_cat/sprites.py` for easy editing.

## Development

```bash
python3 src/claude_cat/__main__.py          # run directly
python3 src/claude_cat/__main__.py --demo   # preview expressions
pip install -e .                            # editable install
```

## Uninstall

```bash
claude-cat uninstall
pip uninstall claude-cat
```

## License

MIT
