// Shared helper for this skill's scripts: run a Lua snippet through the local Aseprite
// in batch (headless) mode and get a structured result back.
//
// Why a file-based result channel instead of stdout: a confined host (the DSH file
// sandbox included) refuses ANY child spawn with piped stdio — `spawn(..., {stdio:'pipe'})`
// and `execFile` both fail with EPERM. Spawning with stdio "ignore" works, so the Lua
// side buffers io.write and flushes it to a file that we read afterwards.
//
// The snippet must report by calling: io.write("__RESULT__" .. json.encode({ ... }))
//
// ── Failure retention (2026-10-15) ──────────────────────────────────────────────
// The upstream MCP server erases the temp script in a `finally` block
// (vendor/aseprite-mcp-master/src/index.ts: `await unlink(tmpFile).catch(() => {})`),
// so a failed `run_script` leaves NO evidence on disk. In the 2026-10-15 farm02
// session two Lua failures could only be located by reasoning backwards from the
// reported line number, because the script was already gone.
// This helper deliberately does the opposite: a failed snippet is COPIED to
// <scratchDir>/_fail/ (override with ASEPRITE_FAIL_DIR), together with the raw result
// channel and a note, before anything is deleted. Set ASEPRITE_KEEP_LUA=1 to keep
// successful runs too.
import { spawn } from "node:child_process";
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";

/**
 * 定位 Aseprite：ASEPRITE_PATH → 常见安装位置（与 MCP 服务端同一套候选）。
 * 找不到时返回空串，由 runLua 给出可执行的报错（导期不抛，--selftest 等纯解析路径仍可用）。
 */
function detectAseprite() {
  const env = process.env.ASEPRITE_PATH;
  if (env && existsSync(env)) return env;
  const candidates =
    process.platform === "win32"
      ? [
          "C:\\Program Files\\Aseprite\\Aseprite.exe",
          "C:\\Program Files (x86)\\Aseprite\\Aseprite.exe",
          "C:\\Program Files (x86)\\Steam\\steamapps\\common\\Aseprite\\Aseprite.exe",
          "C:\\Program Files\\Steam\\steamapps\\common\\Aseprite\\Aseprite.exe",
          join(process.env.LOCALAPPDATA ?? "", "Programs", "Aseprite", "Aseprite.exe"),
        ]
      : process.platform === "darwin"
        ? ["/Applications/Aseprite.app/Contents/MacOS/aseprite"]
        : ["/usr/bin/aseprite", "/usr/local/bin/aseprite", "/snap/bin/aseprite"];
  for (const p of candidates) if (p && existsSync(p)) return p;
  return "";
}

export const ASEPRITE = detectAseprite();

const STAMP = () => new Date().toISOString().replace(/[:.]/g, "-");

/**
 * Copy a failed run's evidence somewhere durable and say where it went.
 * Never throws: losing the echo must not mask the original error.
 */
function retain(scratchDir, id, luaPath, outPath, bodyPath, note) {
  try {
    const dir = process.env.ASEPRITE_FAIL_DIR || join(scratchDir, "_fail");
    mkdirSync(dir, { recursive: true });
    const base = `fail-${STAMP()}-${id}`;
    copyFileSync(luaPath, join(dir, `${base}.lua`));
    if (existsSync(bodyPath)) copyFileSync(bodyPath, join(dir, `${base}.body.lua`));
    if (existsSync(outPath)) copyFileSync(outPath, join(dir, `${base}.out.txt`));
    writeFileSync(
      join(dir, `${base}.note.txt`),
      [
        `when      ${new Date().toISOString()}`,
        `aseprite  ${ASEPRITE}`,
        `scratch   ${scratchDir}`,
        `note      ${note}`,
        "",
        "fail-<stamp>-<id>.lua       exactly what Aseprite ran (error line numbers refer to THIS file)",
        "fail-<stamp>-<id>.body.lua  the caller's snippet, unwrapped",
        "fail-<stamp>-<id>.out.txt   raw result channel (may hold partial io.write output)",
        "",
      ].join("\n"),
      "utf-8",
    );
    console.error(`[aseprite-lua] FAILED — evidence kept at ${join(dir, `${base}.lua`)}`);
  } catch (e) {
    console.error(`[aseprite-lua] could not retain failure evidence: ${e.message}`);
  }
}

/** Run a Lua snippet in Aseprite batch mode. Resolves to the parsed __RESULT__ payload. */
export async function runLua(luaBody, { scratchDir, timeoutMs = 180000 } = {}) {
  // Never default to this file's own directory: under a confined host that lives outside
  // the writable workspace. Callers normally point scratchDir at the artifact's directory;
  // the fallback is the temp dir (override with ASEPRITE_MCP_TMP).
  const dir = scratchDir || process.env.ASEPRITE_MCP_TMP || tmpdir();
  mkdirSync(dir, { recursive: true });
  const id = randomUUID();
  const luaPath = join(dir, `.aseprite-lua-${id}.lua`);
  const bodyPath = join(dir, `.aseprite-lua-${id}.body.lua`);
  const outPath = join(dir, `.aseprite-lua-${id}.json`);
  const outLua = outPath.replace(/\\/g, "/");

  const wrapped = `
local __out = "${outLua}"
local __buf = {}
local __orig = io.write
io.write = function(...)
  for i = 1, select("#", ...) do __buf[#__buf + 1] = tostring((select(i, ...))) end
  return io
end
local __ok, __err = pcall(function()
${luaBody}
end)
if not __ok then
  __buf[#__buf + 1] = "__RESULT__" .. json.encode({ success = false, error = tostring(__err) })
end
io.write = __orig
local __f = io.open(__out, "w")
if __f then __f:write(table.concat(__buf)) __f:close() end
`;
  writeFileSync(luaPath, wrapped, "utf-8");
  writeFileSync(bodyPath, luaBody, "utf-8");
  if (existsSync(outPath)) rmSync(outPath, { force: true });

  if (!ASEPRITE) {
    throw new Error(
      "未找到 Aseprite：设置环境变量 ASEPRITE_PATH 指向 Aseprite.exe，" +
        "或把 Aseprite 装到默认位置（C:\\Program Files\\Aseprite）。",
    );
  }

  let exitNote = null;
  await new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn(ASEPRITE, ["-b", "--script", luaPath], { timeout: timeoutMs, stdio: "ignore" });
    } catch (err) {
      return reject(err);
    }
    child.on("error", reject);
    child.on("close", (code, signal) => {
      if (code !== 0) exitNote = `aseprite exit ${code}${signal ? ` signal ${signal}` : ""}`;
      resolve();
    });
  });

  let raw = "";
  try {
    raw = readFileSync(outPath, "utf-8");
  } catch {
    raw = "";
  }

  const keepAll = process.env.ASEPRITE_KEEP_LUA === "1";
  const idx = raw.indexOf("__RESULT__");

  if (idx === -1) {
    const why = `Aseprite produced no result${exitNote ? ` (${exitNote})` : ""}`
      + ` — no __RESULT__ marker, so the script died before/outside its report`;
    retain(dir, id, luaPath, outPath, bodyPath, why);
    rmSync(luaPath, { force: true });
    rmSync(bodyPath, { force: true });
    rmSync(outPath, { force: true });
    throw new Error(`${why}. Check ASEPRITE_PATH=${ASEPRITE}`);
  }

  let parsed;
  try {
    parsed = JSON.parse(raw.slice(idx + "__RESULT__".length).trim());
  } catch (e) {
    const why = `could not parse the result JSON: ${e.message}`;
    retain(dir, id, luaPath, outPath, bodyPath, why);
    rmSync(luaPath, { force: true });
    rmSync(bodyPath, { force: true });
    rmSync(outPath, { force: true });
    throw new Error(`Aseprite reported an unparseable result (${why})`);
  }

  // A snippet can fail three ways: the pcall wrapper caught it (success=false), Aseprite
  // itself exited non-zero, or it reported success and lied. Only the first two are
  // "failed runs" for retention purposes.
  const ok = parsed?.success !== false && !exitNote;
  if (!ok) {
    const why = parsed?.error ?? exitNote ?? "unknown failure";
    retain(dir, id, luaPath, outPath, bodyPath, String(why));
  } else if (keepAll) {
    const dir2 = process.env.ASEPRITE_FAIL_DIR || join(dir, "_fail");
    mkdirSync(dir2, { recursive: true });
    copyFileSync(luaPath, join(dir2, `ok-${STAMP()}-${id}.lua`));
  }

  rmSync(luaPath, { force: true });
  rmSync(bodyPath, { force: true });
  rmSync(outPath, { force: true });

  return parsed;
}
