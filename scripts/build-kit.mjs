#!/usr/bin/env node
// Assemble dist/pixel-preset-kit from this repo.
//
//   node scripts/build-kit.mjs          # build only
//   node scripts/build-kit.mjs --zip    # build + dist/pixel-preset-kit.zip
//
// Requires node >= 18. MCP runtime dependencies are installed with `npm ci --omit=dev`
// into mcp/node_modules (gitignored) and then copied into the release payload.
import { readFileSync, writeFileSync, mkdirSync, rmSync, cpSync, existsSync, readdirSync, statSync } from "node:fs";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { join, dirname, relative } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, "..");
const DIST = join(REPO, "dist", "pixel-preset-kit");
const MCP = join(REPO, "mcp");
const VERSION = JSON.parse(readFileSync(join(REPO, "package.json"), "utf8")).version;
const WITH_ZIP = process.argv.includes("--zip");

const problems = [];
const step = (s) => console.log("==> " + s);

// --------------------------------------------------------------- dependencies --
function ensureMcpDeps() {
  const sdk = join(MCP, "node_modules", "@modelcontextprotocol", "sdk");
  const devOnly = join(MCP, "node_modules", "typescript");
  if (existsSync(sdk) && !existsSync(devOnly)) {
    console.log("mcp/node_modules present (production tree), skipping npm ci");
    return;
  }
  step("installing MCP runtime dependencies (npm ci --omit=dev)");
  // `npm ci` wipes node_modules first, so a dev tree left over from `npm install`
  // (needed to compile src/) is replaced by the production tree that we ship.
  const r = spawnSync("npm.cmd", ["ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"], {
    cwd: MCP,
    stdio: "inherit",
    shell: true,
  });
  if (r.status !== 0) {
    problems.push(`npm ci failed in mcp/ (exit ${r.status}); run it manually to see the error`);
  }
}

// ------------------------------------------------------------------- copying --
const copy = (from, to) => {
  if (!existsSync(from)) {
    problems.push(`missing source: ${relative(REPO, from)}`);
    return;
  }
  mkdirSync(dirname(to), { recursive: true });
  cpSync(from, to, { recursive: true });
};
const copyByExt = (dir, to, ext) => {
  for (const f of readdirSync(dir)) if (f.endsWith(ext)) copy(join(dir, f), join(to, f));
};

ensureMcpDeps();
if (problems.length) {
  console.error(problems.join("\n"));
  process.exit(1);
}

step("assembling " + relative(REPO, DIST));
rmSync(DIST, { recursive: true, force: true });
mkdirSync(DIST, { recursive: true });

const PAYLOAD = join(DIST, "payload");
copy(join(REPO, "skills", "aseprite-pixel-art"), join(PAYLOAD, "skills", "aseprite-pixel-art"));
copyByExt(join(REPO, "vision"), join(PAYLOAD, "vision"), ".py");
copyByExt(join(REPO, "tools"), join(PAYLOAD, "tools"), ".py");

// MCP server: prebuilt bundle + runtime deps + upstream licence/metadata
copy(join(MCP, "build", "index.js"), join(PAYLOAD, "mcp", "build", "index.js"));
copy(join(MCP, "package.json"), join(PAYLOAD, "mcp", "package.json"));
copy(join(MCP, "LICENSE"), join(PAYLOAD, "mcp", "LICENSE"));
copy(join(MCP, "node_modules"), join(PAYLOAD, "mcp", "node_modules"));

// installer assets + the preset template
copy(join(REPO, "preset", "preset-pixel.patch.yml.tpl"), join(DIST, "preset-pixel.patch.yml.tpl"));
copy(join(REPO, "scripts", "install.ps1"), join(DIST, "install.ps1"));
copy(join(REPO, "scripts", "uninstall.ps1"), join(DIST, "uninstall.ps1"));
copy(join(REPO, "README.zh-CN.md"), join(DIST, "README.md"));

// Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI (GBK on a zh-CN box) and the Chinese
// messages come out as mojibake. Ship both installers as UTF-8 WITH BOM.
for (const ps of ["install.ps1", "uninstall.ps1"]) {
  const p = join(DIST, ps);
  const text = readFileSync(p, "utf8").replace(/^\uFEFF/, "");
  writeFileSync(p, "\uFEFF" + text, "utf8");
}

// ------------------------------------------------------------------ manifest --
function walk(dir, base = dir, acc = []) {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) walk(p, base, acc);
    else acc.push({ path: relative(base, p).replace(/\\/g, "/"), bytes: statSync(p).size });
  }
  return acc;
}
const files = walk(DIST)
  .filter((f) => f.path !== "manifest.json")
  .map((f) => ({ ...f, sha256: createHash("sha256").update(readFileSync(join(DIST, f.path))).digest("hex") }))
  .sort((a, b) => a.path.localeCompare(b.path));
writeFileSync(
  join(DIST, "manifest.json"),
  JSON.stringify({ kit: "pixel-preset-kit", version: VERSION, files, totalBytes: files.reduce((s, f) => s + f.bytes, 0) }, null, 2) + "\n",
  "utf8",
);

// -------------------------------------------------------------- self-checks --
const must = [
  "README.md",
  "install.ps1",
  "uninstall.ps1",
  "preset-pixel.patch.yml.tpl",
  "payload/skills/aseprite-pixel-art/SKILL.md",
  "payload/skills/aseprite-pixel-art/scripts/cel-ascii.mjs",
  "payload/vision/image-diff.py",
  "payload/tools/atlas-pack.py",
  "payload/mcp/build/index.js",
  "payload/mcp/node_modules/@modelcontextprotocol/sdk/package.json",
];
for (const m of must) if (!existsSync(join(DIST, m))) problems.push(`release is missing ${m}`);

const installers = ["install.ps1", "uninstall.ps1"].map((f) => readFileSync(join(DIST, f), "utf8"));
for (let i = 0; i < installers.length; i++) {
  const f = ["install.ps1", "uninstall.ps1"][i];
  if (!installers[i].startsWith("\uFEFF")) problems.push(`${f} lost its BOM`);
  if (!installers[i].includes("pixel-toolkit")) problems.push(`${f} does not mention the toolkit dir`);
}

const tpl = readFileSync(join(DIST, "preset-pixel.patch.yml.tpl"), "utf8");
for (const token of ["__PIXEL_TOOLKIT__", "__ASEPRITE_PATH_LINE__", "- id: preset-pixel", "customSkillDirs", "mcp-aseprite"]) {
  if (!tpl.includes(token)) problems.push(`preset template lost "${token}"`);
}
// The preset launches the server at <toolkit>\mcp\build\index.js; if the template ever says
// <toolkit>\mcp\index.js the install looks fine and every drawing call fails to start.
if (!tpl.includes("mcp\\build\\index.js")) problems.push("preset template does not launch mcp\\build\\index.js");

// A collapsed block scalar (paragraphs joined into one huge line) keeps the file parseable by
// eye but breaks YAML: "mapping values are not allowed here". Catch the shape, not the symptom:
// the plan-mode body must still be several paragraphs, and no line may look like two joined.
const tplLines = tpl.split("\n");
const secIdx = tplLines.indexOf("                  section: |");
if (secIdx < 0) {
  problems.push("preset template lost the plan-mode `section: |` key");
} else {
  const body = [];
  for (let i = secIdx + 1; i < tplLines.length; i++) {
    const l = tplLines[i];
    if (l.trim() !== "" && !l.startsWith(" ".repeat(20))) break;
    body.push(l);
  }
  const blanks = body.filter((l) => l.trim() === "").length;
  const longest = Math.max(0, ...body.map((l) => l.length));
  if (!body[0] || !body[0].startsWith(" ".repeat(20))) problems.push("plan-mode section body is not indented 20 spaces under its key");
  if (blanks < 3) problems.push(`plan-mode section has only ${blanks} paragraph break(s) — block scalar likely collapsed`);
  if (longest > 800) problems.push(`plan-mode section has a ${longest}-char line — paragraphs likely joined`);
}

// Nothing authored here may point back at the author's machine. Vendored trees
// (mcp/node_modules, the compiled MCP bundle) are skipped: they legitimately contain
// numbers and paths of their own, and scanning them only produces false positives.
const leaks = ["C:\\Users\\20566", "D:\\Workplace", "preset-skills", "/home/"];
const authored = (p) =>
  p === "README.md" ||
  p.endsWith(".ps1") ||
  p.endsWith(".tpl") ||
  p.startsWith("payload/skills/") ||
  p.startsWith("payload/vision/") ||
  p.startsWith("payload/tools/");
for (const f of files) {
  if (!authored(f.path)) continue;
  const text = readFileSync(join(DIST, f.path), "utf8");
  for (const leak of leaks) {
    if (text.includes(leak)) problems.push(`release file ${f.path} leaks "${leak}"`);
  }
}

const depCount = readdirSync(join(PAYLOAD, "mcp", "node_modules")).length;
if (depCount < 20) problems.push(`mcp/node_modules has only ${depCount} entries - dependencies missing?`);

if (problems.length) {
  console.error("BUILD FAILED:\n" + [...new Set(problems)].map((p) => "  x " + p).join("\n"));
  process.exit(1);
}

const totalBytes = files.reduce((s, f) => s + f.bytes, 0);
console.log(`release -> ${DIST}`);
console.log(`files ${files.length}, ${(totalBytes / 1024 / 1024).toFixed(1)} MB, top-level mcp deps ${depCount}`);

if (WITH_ZIP) {
  const zip = join(REPO, "dist", "pixel-preset-kit.zip");
  rmSync(zip, { force: true });
  step("zipping (this takes ~1 min)");
  const r = spawnSync(
    "powershell",
    ["-NoProfile", "-Command", `Compress-Archive -Path '${DIST}' -DestinationPath '${zip}' -CompressionLevel Optimal`],
    { stdio: "inherit" },
  );
  if (r.status !== 0) {
    console.error("zip failed; run Compress-Archive manually");
    process.exit(1);
  }
  const hash = createHash("sha256").update(readFileSync(zip)).digest("hex");
  console.log(`zip -> ${zip}`);
  console.log(`sha256 ${hash}`);
}
