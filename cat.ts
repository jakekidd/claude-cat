#!/usr/bin/env bun
// claude-cat -- a 1-bit companion cat that reacts to Claude Code
//
// bun cat.ts          watch mode (run in a side terminal)
// bun cat.ts --hook   hook mode (called by Claude Code via stdin)
// bun cat.ts --demo   cycle through all expressions

import { watch, existsSync, readFileSync, writeFileSync } from "node:fs";

const STATE_FILE = "/tmp/claude-cat.json";

// ── Hook mode ───────────────────────────────────────────────────────
if (process.argv.includes("--hook")) {
  try {
    const text = await Bun.stdin.text();
    const input = JSON.parse(text);
    writeFileSync(
      STATE_FILE,
      JSON.stringify({
        event: input.hook_event_name ?? "unknown",
        tool: input.tool_name ?? "",
        ts: Date.now(),
      })
    );
  } catch {}
  process.exit(0);
}

// ── Types ───────────────────────────────────────────────────────────
type Mood =
  | "idle"
  | "blink"
  | "working"
  | "happy"
  | "error"
  | "sleeping"
  | "surprised";

// ── Sprites ─────────────────────────────────────────────────────────
// Pixel bitmaps: '#' = filled, '.' = empty
// Row count must be even. Each pair -> one char row via half-blocks.
// ▀ = top filled  ▄ = bottom filled  █ = both  ' ' = neither
const sprites: Record<Mood, string[]> = {
  idle: [
    "..##.....##..",
    ".####...####.",
    "#############",
    "#############",
    "###..###..###",
    "###..###..###",
    "#############",
    "######.######",
    "#############",
    "#####...#####",
    "#############",
    "#############",
    ".###########.",
    "..#########..",
  ],
  blink: [
    "..##.....##..",
    ".####...####.",
    "#############",
    "#############",
    "#############",
    "#############",
    "#############",
    "######.######",
    "#############",
    "#####...#####",
    "#############",
    "#############",
    ".###########.",
    "..#########..",
  ],
  working: [
    "..##.....##..",
    ".####...####.",
    "#############",
    "#############",
    "##...###...##",
    "##...###...##",
    "#############",
    "######.######",
    "#############",
    "####.....####",
    "#############",
    "#############",
    ".###########.",
    "..#########..",
  ],
  happy: [
    "..##.....##..",
    ".####...####.",
    "#############",
    "#############",
    "#############",
    "###..###..###",
    "#############",
    "######.######",
    "#############",
    "####.....####",
    "#####...#####",
    "#############",
    ".###########.",
    "..#########..",
  ],
  error: [
    "..##.....##..",
    ".####...####.",
    "#############",
    "#############",
    "###.#####.###",
    "###.#####.###",
    "#############",
    "######.######",
    "#############",
    "####.....####",
    "#############",
    "#############",
    ".###########.",
    "..#########..",
  ],
  sleeping: [
    "..##.....##..",
    ".####...####.",
    "#############",
    "#############",
    "#############",
    "###..###..###",
    "#############",
    "######.######",
    "#############",
    "#############",
    "#############",
    "#############",
    ".###########.",
    "..#########..",
  ],
  surprised: [
    "..##.....##..",
    ".####...####.",
    "#############",
    "##...###...##",
    "##...###...##",
    "##...###...##",
    "#############",
    "######.######",
    "#############",
    "####.....####",
    "####.....####",
    "#############",
    ".###########.",
    "..#########..",
  ],
};

// ── Bitmap -> half-blocks ───────────────────────────────────────────
function toBlocks(rows: string[]): string[] {
  const out: string[] = [];
  for (let i = 0; i < rows.length; i += 2) {
    let line = "";
    for (let j = 0; j < rows[i].length; j++) {
      const t = rows[i][j] === "#";
      const b = (rows[i + 1]?.[j] ?? ".") === "#";
      line += t ? (b ? "\u2588" : "\u2580") : b ? "\u2584" : " ";
    }
    out.push(line);
  }
  return out;
}

// ── Tool labels ─────────────────────────────────────────────────────
const LABELS: Record<string, string> = {
  Read: "reading",
  Edit: "editing",
  Write: "writing",
  Bash: "hacking",
  Grep: "searching",
  Glob: "looking",
  Agent: "thinking",
  WebFetch: "fetching",
  WebSearch: "googling",
  Skill: "casting",
};

// ── State ───────────────────────────────────────────────────────────
let mood: Mood = "idle";
let bubble = "";
let blinking = false;
let lastStateRaw = "";
let bubbleTimer: Timer | null = null;
let sleepTimer: Timer | null = null;

// ── Render ──────────────────────────────────────────────────────────
function render() {
  const m: Mood = blinking && mood !== "sleeping" ? "blink" : mood;
  const cat = toBlocks(sprites[m]);
  const catW = cat[0]?.length ?? 13;

  let out = "\x1b[H\x1b[?25l"; // cursor home + hide cursor

  if (bubble) {
    const inner = ` ${bubble} `;
    const boxW = inner.length + 2;
    const pad = " ".repeat(Math.max(0, Math.floor((catW - boxW) / 2)));
    out += `${pad}\x1b[2m\u256d${"\u2500".repeat(inner.length)}\u256e\x1b[0m\x1b[K\n`;
    out += `${pad}\x1b[2m\u2502\x1b[0m${inner}\x1b[2m\u2502\x1b[0m\x1b[K\n`;
    out += `${pad}\x1b[2m\u2570${"\u2500".repeat(inner.length)}\u256f\x1b[0m\x1b[K\n`;
  } else {
    out += "\x1b[K\n\x1b[K\n\x1b[K\n";
  }

  for (const line of cat) out += `\x1b[1m${line}\x1b[0m\x1b[K\n`;

  out += `\x1b[K\n\x1b[2m${mood}\x1b[0m\x1b[K\n\x1b[J`;
  process.stdout.write(out);
}

// ── Event handling ──────────────────────────────────────────────────
function handleEvent(data: { event?: string; tool?: string }) {
  // Wake up from sleep
  if (mood === "sleeping") {
    mood = "surprised";
    bubble = "!";
    render();
    setTimeout(() => {
      mood = "idle";
      applyEvent(data);
    }, 500);
    resetSleep();
    return;
  }
  applyEvent(data);
  resetSleep();
}

function applyEvent(data: { event?: string; tool?: string }) {
  const ev = data.event ?? "";
  const tool = data.tool ?? "";

  if (ev === "Stop" || ev === "SubagentStop") {
    mood = "happy";
    bubble = ev === "Stop" ? "done!" : "returned";
  } else if (ev === "PostToolUseFailure") {
    mood = "error";
    bubble = "oops";
  } else if (ev === "PostToolUse" || ev === "PreToolUse") {
    mood = "working";
    bubble = LABELS[tool] || tool.toLowerCase() || "working";
  } else if (ev === "SubagentStart") {
    mood = "working";
    bubble = "spawning";
  } else {
    mood = "idle";
    bubble = ev.toLowerCase() || "";
  }

  render();

  if (bubbleTimer) clearTimeout(bubbleTimer);
  bubbleTimer = setTimeout(() => {
    bubble = "";
    if (mood !== "sleeping") mood = "idle";
    render();
  }, 4000);
}

// ── Sleep ───────────────────────────────────────────────────────────
function resetSleep() {
  if (sleepTimer) clearTimeout(sleepTimer);
  sleepTimer = setTimeout(() => {
    mood = "sleeping";
    bubble = "zzz";
    render();
    setTimeout(() => {
      bubble = "";
      render();
    }, 3000);
  }, 120_000);
}

// ── Blink ───────────────────────────────────────────────────────────
function scheduleBlink() {
  setTimeout(
    () => {
      if (mood !== "sleeping" && mood !== "surprised") {
        blinking = true;
        render();
        setTimeout(() => {
          blinking = false;
          render();
          scheduleBlink();
        }, 150);
      } else {
        scheduleBlink();
      }
    },
    2000 + Math.random() * 5000
  );
}

// ── Demo mode ───────────────────────────────────────────────────────
if (process.argv.includes("--demo")) {
  process.stdout.write("\x1b[2J");
  const moods: Mood[] = [
    "idle",
    "blink",
    "working",
    "happy",
    "error",
    "sleeping",
    "surprised",
  ];
  let i = 0;
  const next = () => {
    if (i >= moods.length) {
      process.stdout.write("\x1b[?25h");
      process.exit(0);
    }
    mood = moods[i];
    bubble = mood;
    render();
    i++;
    setTimeout(next, 1500);
  };
  next();
}

// ── Watch mode ──────────────────────────────────────────────────────
else {
  process.stdout.write("\x1b[2J");
  if (!existsSync(STATE_FILE)) writeFileSync(STATE_FILE, "{}");

  watch(STATE_FILE, () => {
    try {
      const raw = readFileSync(STATE_FILE, "utf-8");
      if (raw === lastStateRaw) return;
      lastStateRaw = raw;
      handleEvent(JSON.parse(raw));
    } catch {}
  });

  scheduleBlink();
  resetSleep();
  render();
}

// ── Cleanup ─────────────────────────────────────────────────────────
const cleanup = () => {
  process.stdout.write("\x1b[?25h\n");
  process.exit(0);
};
process.on("SIGINT", cleanup);
process.on("SIGTERM", cleanup);
