"""Sprite loading for claude-cat.

Sprites are pixel bitmaps: '#' = filled, '.' = empty.
Row count and width must be even.

Each 2x2 pixel block maps to one quadrant block character,
giving 2x resolution in both axes.

Sprites can be loaded from:
  1. A JSON file (--sprite path/to/file.json)
  2. A named sprite in the sprites/ directory (--sprite name)
  3. The built-in default (no flag)

JSON format:
{
  "name": "my-cat",
  "author": "your-name",
  "description": "A cool cat",
  "width": 24,
  "height": 16,
  "moods": {
    "idle": ["..##..", ...],
    "blink": [...],
    "working": [...],
    "happy": [...],
    "error": [...],
    "sleeping": [...],
    "surprised": [...]
  }
}
"""

import json
import os
from pathlib import Path

REQUIRED_MOODS = ["idle", "blink", "working", "happy", "error", "sleeping", "surprised"]

# Built-in fallback (same as sprites/default.json)
BUILTIN = {
    "idle": [
        ".....##..........##.....",
        "....####........####....",
        "...######......######...",
        "..####################..",
        "..####################..",
        "..####....####....####..",
        "..####....####....####..",
        "..####################..",
        "..#########..#########..",
        "..####################..",
        "..########....########..",
        "..####################..",
        "..####################..",
        "..####################..",
        "...##################...",
        ".....##############.....",
    ],
    "blink": [
        ".....##..........##.....",
        "....####........####....",
        "...######......######...",
        "..####################..",
        "..####################..",
        "..####################..",
        "..####################..",
        "..####################..",
        "..#########..#########..",
        "..####################..",
        "..########....########..",
        "..####################..",
        "..####################..",
        "..####################..",
        "...##################...",
        ".....##############.....",
    ],
    "working": [
        ".....##..........##.....",
        "....####........####....",
        "...######......######...",
        "..####################..",
        "..####################..",
        "..###.....####.....###..",
        "..###.....####.....###..",
        "..####################..",
        "..#########..#########..",
        "..####################..",
        "..#######......#######..",
        "..####################..",
        "..####################..",
        "..####################..",
        "...##################...",
        ".....##############.....",
    ],
    "happy": [
        ".....##..........##.....",
        "....####........####....",
        "...######......######...",
        "..####################..",
        "..####################..",
        "..####################..",
        "..####....####....####..",
        "..####################..",
        "..#########..#########..",
        "..####################..",
        "..#####..........#####..",
        "..########....########..",
        "..####################..",
        "..####################..",
        "...##################...",
        ".....##############.....",
    ],
    "error": [
        ".....##..........##.....",
        "....####........####....",
        "...######......######...",
        "..####################..",
        "..####################..",
        "..#####..######..#####..",
        "..####.##.####.##.####..",
        "..####################..",
        "..#########..#########..",
        "..####################..",
        "..#########..#########..",
        "..#######......#######..",
        "..####################..",
        "..####################..",
        "...##################...",
        ".....##############.....",
    ],
    "sleeping": [
        ".....##..........##.....",
        "....####........####....",
        "...######......######...",
        "..####################..",
        "..####################..",
        "..####################..",
        "..####....####....####..",
        "..####################..",
        "..#########..#########..",
        "..####################..",
        "..####################..",
        "..####################..",
        "..####################..",
        "..####################..",
        "...##################...",
        ".....##############.....",
    ],
    "surprised": [
        ".....##..........##.....",
        "....####........####....",
        "...######......######...",
        "..####################..",
        "..##......####......##..",
        "..##......####......##..",
        "..##......####......##..",
        "..####################..",
        "..#########..#########..",
        "..####################..",
        "..#####..........#####..",
        "..#####..........#####..",
        "..####################..",
        "..####################..",
        "...##################...",
        ".....##############.....",
    ],
}


def _sprites_dir():
    """Find the sprites/ directory (inside the package)."""
    return Path(__file__).resolve().parent / "sprites"


def load(name=None):
    """Load sprites by name or path.

    - None -> load default.json from sprites dir, fall back to BUILTIN
    - "somefile.json" or "/path/to/file.json" -> load that file
    - "name" -> look for sprites/name.json
    """
    if name is None:
        default_json = _sprites_dir() / "default.json"
        if default_json.exists():
            return _load_file(default_json)
        return dict(BUILTIN)

    # Direct file path
    path = Path(name)
    if path.suffix == ".json" and path.exists():
        return _load_file(path)

    # Named sprite in sprites/ directory
    sprites_dir = _sprites_dir()
    if sprites_dir:
        candidate = sprites_dir / (name + ".json")
        if candidate.exists():
            return _load_file(candidate)

    # Try as direct path without .json suffix
    if path.with_suffix(".json").exists():
        return _load_file(path.with_suffix(".json"))

    print("Sprite not found: %s" % name)
    print("Available sprites:")
    list_sprites()
    raise SystemExit(1)


def _load_file(path):
    """Load and validate a sprite JSON file."""
    with open(path) as f:
        data = json.load(f)

    moods = data.get("moods", data)
    if not isinstance(moods, dict):
        print("Invalid sprite file: expected 'moods' dict")
        raise SystemExit(1)

    missing = [m for m in REQUIRED_MOODS if m not in moods]
    if missing:
        print("Sprite missing moods: %s" % ", ".join(missing))
        raise SystemExit(1)

    # Validate dimensions
    for mood, rows in moods.items():
        if len(rows) % 2 != 0:
            print("Sprite '%s' has odd row count (%d), must be even" % (mood, len(rows)))
            raise SystemExit(1)
        widths = set(len(r) for r in rows)
        if len(widths) > 1:
            print("Sprite '%s' has inconsistent row widths" % mood)
            raise SystemExit(1)

    return moods


def list_sprites():
    """List available sprite files."""
    sprites_dir = _sprites_dir()
    if not sprites_dir:
        print("  (no sprites directory found)")
        return
    found = sorted(sprites_dir.glob("*.json"))
    if not found:
        print("  (none)")
        return
    for f in found:
        try:
            data = json.loads(f.read_text())
            desc = data.get("description", "")
            author = data.get("author", "")
            label = f.stem
            meta = []
            if author:
                meta.append("by " + author)
            if desc:
                meta.append(desc)
            print("  %s  %s" % (label, " -- ".join(meta) if meta else ""))
        except Exception:
            print("  %s" % f.stem)


# Default export for backward compat
SPRITES = BUILTIN
