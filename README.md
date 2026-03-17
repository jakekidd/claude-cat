# claude-cat

> WIP -- sprite art is placeholder. PRs welcome. Run `python3 view-sprite.py` to preview, `python3 edit-sprite.py` to draw.

A 1-bit pixel art cat that lives in your terminal and reacts to [Claude Code](https://docs.anthropic.com/en/docs/claude-code)'s activity in real time. Uses inverse video rendering for gap-free display and quadrant block characters for smooth edges.

## Install

```bash
python3 -m pip install claude-cat
claude-cat install
```

`install` adds [hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) to `~/.claude/settings.json` so Claude Code sends events to the cat.

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
| `claude-cat --sprite <name>` | Use a custom sprite |
| `claude-cat list-sprites` | Show available sprites |
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

## Custom sprites

Sprites are JSON files defining pixel bitmaps for each mood. Use `#` for filled pixels and `.` for empty. Width and height must be even.

```bash
claude-cat --sprite my-cat          # load sprites/my-cat.json
claude-cat --sprite ./my-sprite.json  # load from path
```

### JSON format

```json
{
  "name": "my-cat",
  "author": "your-name",
  "description": "A cool cat",
  "width": 24,
  "height": 16,
  "moods": {
    "idle":      ["..##..", ...],
    "blink":     ["..##..", ...],
    "working":   ["..##..", ...],
    "happy":     ["..##..", ...],
    "error":     ["..##..", ...],
    "sleeping":  ["..##..", ...],
    "surprised": ["..##..", ...]
  }
}
```

All 7 moods are required. Each mood is a list of equal-length strings. Pairs of rows and pairs of columns map to quadrant block characters, so every 2x2 pixel group becomes one terminal character.

### Contributing sprites

Drop your JSON file in `src/claude_cat/sprites/` and open a PR. The `name`, `author`, and `description` fields show up in `claude-cat list-sprites`.

## How it works

1. `claude-cat install` adds hooks to Claude Code settings
2. Hooks fire on tool use, errors, and completion, piping event data to `claude-cat --hook`
3. Hook mode writes a small JSON state file to the OS temp directory
4. The display process polls that file and updates the cat's expression

Zero dependencies. Python 3.9+.

## Rendering

Each character cell is a 2x2 pixel grid mapped to one of 16 Unicode quadrant block elements:

```
 ▘▝▀▖▌▞▛▗▚▐▜▄▙▟█ (space)
```

This gives 2x resolution in both axes compared to full-block characters.

## Development

```bash
python3 src/claude_cat/__main__.py          # run directly
python3 src/claude_cat/__main__.py --demo   # preview expressions
python3 -m pip install -e .                            # editable install
```

## Uninstall

```bash
claude-cat uninstall
python3 -m pip uninstall claude-cat
```

## License

MIT
