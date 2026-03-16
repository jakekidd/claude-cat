import { execSync } from "node:child_process";
import { readFileSync, writeFileSync, chmodSync, mkdirSync } from "node:fs";

mkdirSync("dist", { recursive: true });
execSync("npx tsc", { stdio: "inherit" });

// Prepend shebang
const code = readFileSync("dist/index.js", "utf-8");
if (!code.startsWith("#!")) {
  writeFileSync("dist/index.js", `#!/usr/bin/env node\n${code}`);
}
chmodSync("dist/index.js", 0o755);

console.log("Built dist/index.js");
