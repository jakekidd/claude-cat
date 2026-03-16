#!/usr/bin/env node
// claude-cat -- a 1-bit companion cat for Claude Code

import {
  existsSync,
  readFileSync,
  writeFileSync,
  statSync,
  mkdirSync,
} from "node:fs";
import { join } from "node:path";
import { homedir, tmpdir } from "node:os";

const VERSION = "0.1.0";
const STATE_FILE = join(tmpdir(), "claude-cat.json");
const HOOK_EVENTS = [
  "PostToolUse",
  "PostToolUseFailure",
  "Stop",
  "SubagentStart",
  "SubagentStop",
];

// ── CLI ─────────────────────────────────────────────────────────────
const args = process.argv.slice(2);
const cmd = args[0] ?? "";

if (cmd === "--hook" || cmd === "hook") hookMode();
else if (cmd === "--demo" || cmd === "demo") demoMode();
else if (cmd === "install") installCmd();
else if (cmd === "uninstall") uninstallCmd();
else if (cmd === "--help" || cmd === "-h" || cmd === "help") helpCmd();
else if (cmd === "--version" || cmd === "-v") console.log(VERSION);
else if (cmd === "" || cmd === "--watch") watchMode();
else {
  console.error(`Unknown command: ${cmd}`);
  helpCmd();
  process.exit(1);
}

// ── Help ────────────────────────────────────────────────────────────
function helpCmd() {
  console.log(`claude-cat v${VERSION}
A 1-bit companion cat for Claude Code

Usage:
  claude-cat              Start the cat (run in a side terminal)
  claude-cat install      Set up Claude Code hooks
  claude-cat uninstall    Remove Claude Code hooks
  claude-cat --demo       Preview all expressions
  claude-cat --version    Show version`);
}

// ── Hook mode ───────────────────────────────────────────────────────
function hookMode() {
  let data = "";
  process.stdin.setEncoding("utf-8");
  process.stdin.on("data", (chunk: string) => {
    data += chunk;
  });
  process.stdin.on("end", () => {
    try {
      const input = JSON.parse(data);
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
  });
}

// ── Install ─────────────────────────────────────────────────────────
function installCmd() {
  const claudeDir = join(homedir(), ".claude");
  const settingsPath = join(claudeDir, "settings.json");

  if (!existsSync(claudeDir)) mkdirSync(claudeDir, { recursive: true });

  let settings: Record<string, any> = {};
  if (existsSync(settingsPath)) {
    try {
      settings = JSON.parse(readFileSync(settingsPath, "utf-8"));
    } catch {}
  }

  if (!settings.hooks) settings.hooks = {};

  let added = 0;
  for (const event of HOOK_EVENTS) {
    if (!settings.hooks[event]) settings.hooks[event] = [];
    const exists = settings.hooks[event].some((rule: any) =>
      rule.hooks?.some((h: any) => h.command?.includes("claude-cat"))
    );
    if (!exists) {
      settings.hooks[event].push({
        matcher: "",
        hooks: [
          {
            type: "command",
            command: "claude-cat --hook",
            async: true,
            timeout: 5,
          },
        ],
      });
      added++;
    }
  }

  writeFileSync(settingsPath, JSON.stringify(settings, null, 2) + "\n");

  if (added > 0) {
    console.log(`Installed ${added} hook(s) in ${settingsPath}`);
    console.log("Run claude-cat in a side terminal to see your cat.");
  } else {
    console.log("Hooks already installed.");
  }
}

// ── Uninstall ───────────────────────────────────────────────────────
function uninstallCmd() {
  const settingsPath = join(homedir(), ".claude", "settings.json");

  if (!existsSync(settingsPath)) {
    console.log("No settings found.");
    return;
  }

  let settings: Record<string, any> = {};
  try {
    settings = JSON.parse(readFileSync(settingsPath, "utf-8"));
  } catch {
    return;
  }

  if (!settings.hooks) {
    console.log("No hooks found.");
    return;
  }

  let removed = 0;
  for (const event of HOOK_EVENTS) {
    if (!settings.hooks[event]) continue;
    const before = settings.hooks[event].length;
    settings.hooks[event] = settings.hooks[event].filter(
      (rule: any) =>
        !rule.hooks?.some((h: any) => h.command?.includes("claude-cat"))
    );
    removed += before - settings.hooks[event].length;
    if (settings.hooks[event].length === 0) delete settings.hooks[event];
  }

  if (Object.keys(settings.hooks).length === 0) delete settings.hooks;

  writeFileSync(settingsPath, JSON.stringify(settings, null, 2) + "\n");
  console.log(`Removed ${removed} hook(s) from ${settingsPath}`);
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
// Row count must be even. Pairs of rows -> one character row.
// Encoding: top+bot -> (1,1)=█  (1,0)=▀  (0,1)=▄  (0,0)=' '
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
let lastMtime = 0;
let bubbleTimer: ReturnType<typeof setTimeout> | null = null;
let sleepTimer: ReturnType<typeof setTimeout> | null = null;

// ── Render ──────────────────────────────────────────────────────────
function render() {
  const m: Mood = blinking && mood !== "sleeping" ? "blink" : mood;
  const cat = toBlocks(sprites[m]);
  const catW = cat[0]?.length ?? 13;

  let out = "\x1b[H\x1b[?25l";

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

// ── Events ──────────────────────────────────────────────────────────
function handleEvent(data: { event?: string; tool?: string }) {
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
function demoMode() {
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
  setupCleanup();
}

// ── Watch mode ──────────────────────────────────────────────────────
function watchMode() {
  process.stdout.write("\x1b[2J");
  if (!existsSync(STATE_FILE)) writeFileSync(STATE_FILE, "{}");

  // Poll state file for changes (portable across platforms)
  setInterval(() => {
    try {
      const stat = statSync(STATE_FILE);
      if (stat.mtimeMs <= lastMtime) return;
      lastMtime = stat.mtimeMs;
      const raw = readFileSync(STATE_FILE, "utf-8");
      if (raw === lastStateRaw) return;
      lastStateRaw = raw;
      handleEvent(JSON.parse(raw));
    } catch {}
  }, 500);

  scheduleBlink();
  resetSleep();
  render();
  setupCleanup();
}

// ── Cleanup ─────────────────────────────────────────────────────────
function setupCleanup() {
  const cleanup = () => {
    process.stdout.write("\x1b[?25h\n");
    process.exit(0);
  };
  process.on("SIGINT", cleanup);
  process.on("SIGTERM", cleanup);
}
