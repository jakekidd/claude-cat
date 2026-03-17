"""Sprite loading for claude-cat.

Sprites use the "states" format: animated states + static reactions.

States have multiple frames with animation modes:
  shuffle — random frame each tick (idle, cooking, thinking)
  loop    — cycle frames linearly (reading, hunting, hacking)

Reactions are single frames with hold durations.

Each frame is a list of hex-format rows (one char per terminal cell).
Extended hex: 0-F quadrant blocks + I for inverse video.

JSON format:
{
  "format": "states",
  "states": {
    "idle": {
      "frames": [["00III00", ...], ...],
      "blink": ["00III00", ...],
      "mode": "shuffle",
      "ms": 2000
    }
  },
  "reactions": {
    "happy": {
      "frame": ["00III00", ...],
      "hold": 4.0
    }
  }
}
"""

import json
from pathlib import Path

BLOCKS = " \u2597\u2596\u2584\u259d\u2590\u259e\u259f\u2598\u259a\u258c\u2599\u2580\u259c\u259b\u2588"


def _sprites_dir():
    return Path(__file__).resolve().parent / "sprites"


def load(name=None):
    """Load sprite data by name or path."""
    if name is None:
        default_json = _sprites_dir() / "default.json"
        if default_json.exists():
            return _load_file(default_json)
        return {"states": {}, "reactions": {}}

    path = Path(name)
    if path.suffix == ".json" and path.exists():
        return _load_file(path)

    sprites_dir = _sprites_dir()
    if sprites_dir:
        candidate = sprites_dir / (name + ".json")
        if candidate.exists():
            return _load_file(candidate)

    if path.with_suffix(".json").exists():
        return _load_file(path.with_suffix(".json"))

    print("Sprite not found: %s" % name)
    print("Available sprites:")
    list_sprites()
    raise SystemExit(1)


def _load_file(path):
    with open(path) as f:
        data = json.load(f)

    fmt = data.get("format", "")

    if fmt == "states":
        states = data.get("states", {})
        reactions = data.get("reactions", {})
        return {"states": states, "reactions": reactions}

    # Legacy format: convert moods to states (basic, no animation)
    moods = data.get("moods", data)
    if not isinstance(moods, dict):
        return {"states": {}, "reactions": {}}

    states = {}
    for mood, rows in moods.items():
        states[mood] = {
            "frames": [rows],
            "blink": rows,
            "mode": "hold",
            "ms": 1000,
        }
    return {"states": states, "reactions": {}}


def list_sprites():
    sprites_dir = _sprites_dir()
    if not sprites_dir or not sprites_dir.exists():
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
