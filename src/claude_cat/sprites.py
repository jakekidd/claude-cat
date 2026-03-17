"""Sprite loading for claude-cat.

Sprites use an extended hex format: one character per terminal cell.
Not true hexadecimal -- 17 symbols (base-17, "heptadecimal" if you
want to be fancy). 0-F map to the 16 quadrant block characters,
plus I for inverse video fill.

  0 = empty       8 = ▘
  1 = ▗           9 = ▚
  2 = ▖           A = ▌
  3 = ▄           B = ▙
  4 = ▝           C = ▀
  5 = ▐           D = ▜
  6 = ▞           E = ▛
  7 = ▟           F = █ (foreground block)
  I = inverse video (gap-free fill)

JSON format:
{
  "name": "my-cat",
  "format": "hex",
  "moods": {
    "idle": ["00III00", ...],
    ...
  },
  "eyes": {
    "idle": {
      "slots": [[3,3], [3,4], [3,8], [3,9]],
      "frames": ["1212", "2I2I", "I1I1"],
      "ms": 2000
    }
  }
}
"""

import json
import os
from pathlib import Path

REQUIRED_MOODS = ["idle", "blink", "working", "happy", "error", "sleeping", "surprised"]

VALID_CHARS = set("0123456789ABCDEFabcdefI")

# Block character lookup: index 0-15 maps to Unicode quadrant block
BLOCKS = " \u2597\u2596\u2584\u259d\u2590\u259e\u259f\u2598\u259a\u258c\u2599\u2580\u259c\u259b\u2588"


def _sprites_dir():
    return Path(__file__).resolve().parent / "sprites"


def _convert_legacy(rows):
    """Convert old subpixel (#/.) format to hex format."""
    hexchars = "0123456789ABCDEF"
    out = []
    for y in range(0, len(rows), 2):
        top = rows[y]
        bot = rows[y + 1] if y + 1 < len(rows) else "." * len(top)
        line = ""
        for x in range(0, len(top), 2):
            tl = 1 if top[x] == "#" else 0
            tr = 1 if x + 1 < len(top) and top[x + 1] == "#" else 0
            bl = 1 if bot[x] == "#" else 0
            br = 1 if x + 1 < len(bot) and bot[x + 1] == "#" else 0
            idx = tl * 8 + tr * 4 + bl * 2 + br
            line += "I" if idx == 15 else hexchars[idx]
        out.append(line)
    return out


def load(name=None):
    """Load sprites by name or path."""
    if name is None:
        default_json = _sprites_dir() / "default.json"
        if default_json.exists():
            return _load_file(default_json)
        return {"moods": {}, "eyes": {}}

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

    moods = data.get("moods", data)
    if not isinstance(moods, dict):
        print("Invalid sprite file: expected 'moods' dict")
        raise SystemExit(1)

    # Auto-detect and convert legacy format
    is_legacy = data.get("format") != "hex"
    if is_legacy:
        sample = list(moods.values())[0][0]
        if set(sample) <= {"#", "."}:
            converted = {}
            for mood, rows in moods.items():
                converted[mood] = _convert_legacy(rows)
            moods = converted

    missing = [m for m in REQUIRED_MOODS if m not in moods]
    if missing:
        print("Sprite missing moods: %s" % ", ".join(missing))
        raise SystemExit(1)

    for mood, rows in moods.items():
        widths = set(len(r) for r in rows)
        if len(widths) > 1:
            print("Sprite '%s' has inconsistent row widths" % mood)
            raise SystemExit(1)

    # Load eyes config (optional)
    eyes = data.get("eyes", {})
    for mood_name, cfg in eyes.items():
        if mood_name not in moods:
            print("Eyes config references unknown mood: %s" % mood_name)
            raise SystemExit(1)
        slots = cfg.get("slots", [])
        frames = cfg.get("frames", [])
        for frame in frames:
            if len(frame) != len(slots):
                print("Eyes '%s': frame length %d != slot count %d" % (mood_name, len(frame), len(slots)))
                raise SystemExit(1)

    return {"moods": moods, "eyes": eyes}


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
