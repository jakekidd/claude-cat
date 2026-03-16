# claude-cat

A 1-bit pixel art cat that lives in your terminal and reacts to [Claude Code](https://docs.anthropic.com/en/docs/claude-code)'s activity in real time.

```
 ╭──────────╮
 │ reading  │
 ╰──────────╯
 ▄██▄   ▄██▄
█████████████
███  ███  ███
██████▀██████
█████▀▀▀█████
█████████████
 ▀█████████▀
```

## Install

```bash
npm install -g claude-cat
claude-cat install
```

The `install` command adds hooks to `~/.claude/settings.json` so Claude Code sends events to the cat.

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

1. `claude-cat install` adds [hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) to Claude Code settings
2. Hooks fire on tool use, errors, and completion, piping event data to `claude-cat --hook`
3. Hook mode writes a small JSON state file to the OS temp directory
4. The display process polls that file and updates the cat's expression

Zero dependencies. Works with Node.js 18+.

## Customizing sprites

Sprites are defined as pixel bitmaps in `src/index.ts`. Each mood is a grid of `#` (filled) and `.` (empty). Pairs of rows are converted to Unicode half-block characters:

- `(1,1)` = `█` (both halves filled)
- `(1,0)` = `▀` (top half)
- `(0,1)` = `▄` (bottom half)
- `(0,0)` = ` ` (empty)

This gives you 2x vertical pixel resolution per character cell.

## Development

```bash
bun src/index.ts          # run directly
bun src/index.ts --demo   # preview expressions
npm run build             # compile for distribution
```

## Uninstall

```bash
claude-cat uninstall
npm uninstall -g claude-cat
```

## License

MIT
