#!/usr/bin/env node
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema, } from "@modelcontextprotocol/sdk/types.js";
import { execFile, spawn } from "node:child_process";
import { existsSync, openSync, closeSync, readFileSync } from "node:fs";
import { writeFile, unlink, readFile, stat, mkdir, appendFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
// ---------------------------------------------------------------------------
// Debug helper — always to stderr so we don't corrupt JSON-RPC on stdout
// ---------------------------------------------------------------------------
const DEBUG = process.env.DEBUG === "true";
function logDebug(msg) {
    if (DEBUG)
        console.error(`[DEBUG] ${msg}`);
}
// Scratch directory for the generated .lua and its result file. Overridable so the
// server can be pointed at a writable location when the host confines temp writes.
const TMP_DIR = process.env.ASEPRITE_MCP_TMP || tmpdir();
/** RGB -> HSV (h in degrees, s/v in 0..1), same formula as portrait/color-compare.py. */
function rgb2hsv(r, g, b) {
    const rr = r / 255;
    const gg = g / 255;
    const bb = b / 255;
    const mx = Math.max(rr, gg, bb);
    const mn = Math.min(rr, gg, bb);
    const d = mx - mn;
    let h = 0;
    if (d !== 0) {
        if (mx === rr)
            h = (60 * ((gg - bb) / d)) % 360;
        else if (mx === gg)
            h = 60 * ((bb - rr) / d) + 120;
        else
            h = 60 * ((rr - gg) / d) + 240;
    }
    if (h < 0)
        h += 360;
    return [h, mx === 0 ? 0 : d / mx, mx];
}
/** CIE L* (D65), same constants as the project's vision tools. */
function lstarCie(r, g, b) {
    const lin = [r, g, b].map((v) => {
        const c = v / 255;
        return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    const y = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
    return y > 0.008856 ? 116 * y ** (1 / 3) - 16 : 903.3 * y;
}
/** Circular mean of hue angles (degrees) - a ramp of similar hues averages without wraparound surprises. */
function circularMeanHue(hues) {
    if (!hues.length)
        return 0;
    let sx = 0;
    let sy = 0;
    for (const h of hues) {
        const rad = (h * Math.PI) / 180;
        sx += Math.cos(rad);
        sy += Math.sin(rad);
    }
    const mean = (Math.atan2(sy / hues.length, sx / hues.length) * 180) / Math.PI;
    return mean < 0 ? mean + 360 : mean;
}
/** Shortest angular distance between two hues, 0..180. */
function hueDistance(a, b) {
    const d = Math.abs(a - b) % 360;
    return d > 180 ? 360 - d : d;
}
// Escape a string for safe embedding inside Lua double-quoted strings
export function luaEscape(s) {
    return s
        .replace(/\\/g, "\\\\")
        .replace(/"/g, '\\"')
        .replace(/\n/g, "\\n")
        .replace(/\r/g, "\\r");
}
// Normalize file path for Lua (forward slashes, escaped)
export function luaPath(p) {
    return luaEscape(p.replace(/\\/g, "/"));
}
// ---------------------------------------------------------------------------
// Aseprite MCP Server
// ---------------------------------------------------------------------------
export class AsepriteMcpServer {
    server;
    asepritePath = null;
    toolHandlers = new Map();
    constructor() {
        this.server = new Server({ name: "aseprite-mcp", version: "1.1.0" }, { capabilities: { tools: {} } });
        this.setupHandlers();
        process.on("SIGINT", async () => {
            await this.server.close();
            process.exit(0);
        });
    }
    // ---- Path detection -----------------------------------------------------
    async detectAsepritePath() {
        if (this.asepritePath)
            return this.asepritePath;
        const envPath = process.env.ASEPRITE_PATH;
        if (envPath && existsSync(envPath)) {
            this.asepritePath = envPath;
            return envPath;
        }
        const candidates = process.platform === "win32"
            ? [
                "C:\\Program Files\\Aseprite\\Aseprite.exe",
                "C:\\Program Files (x86)\\Aseprite\\Aseprite.exe",
                "C:\\Program Files (x86)\\Steam\\steamapps\\common\\Aseprite\\Aseprite.exe",
                "C:\\Program Files\\Steam\\steamapps\\common\\Aseprite\\Aseprite.exe",
                join(process.env.LOCALAPPDATA ?? "", "Programs", "Aseprite", "Aseprite.exe"),
            ]
            : process.platform === "darwin"
                ? [
                    "/Applications/Aseprite.app/Contents/MacOS/aseprite",
                    join(process.env.HOME ?? "", "Applications", "Aseprite.app", "Contents", "MacOS", "aseprite"),
                ]
                : [
                    "/usr/bin/aseprite",
                    "/usr/local/bin/aseprite",
                    "/snap/bin/aseprite",
                    join(process.env.HOME ?? "", ".local", "bin", "aseprite"),
                ];
        for (const p of candidates) {
            if (p && existsSync(p)) {
                this.asepritePath = p;
                logDebug(`Detected Aseprite at ${p}`);
                return p;
            }
        }
        // Try PATH. Some confined hosts refuse piped-stdio spawns (EPERM); that must fall
        // through to the actionable ASEPRITE_PATH message instead of masking it.
        const cmd = process.platform === "win32" ? "where" : "which";
        const found = await new Promise((resolve) => {
            try {
                execFile(cmd, ["aseprite"], (err, stdout) => {
                    if (err)
                        return resolve(null);
                    const line = (stdout ?? "").trim().split("\n")[0]?.trim();
                    resolve(line || null);
                });
            }
            catch {
                resolve(null);
            }
        });
        if (found) {
            this.asepritePath = found;
            logDebug(`Found Aseprite on PATH: ${found}`);
            return found;
        }
        throw new Error("Aseprite executable not found. Set ASEPRITE_PATH environment variable.");
    }
    // ---- Lua script runner --------------------------------------------------
    /**
     * The pcall + file-result-channel wrapper every Lua run goes through.
     *
     * `preludeLuaLines` is how many complete lines are prepended to the caller's snippet, so a Lua
     * error line number (which refers to the WRAPPED file Aseprite actually ran) can be mapped back
     * to the caller's own numbering: snippetLine = wrappedLine - preludeLuaLines.
     */
    buildLuaWrapper(snippet, resultFile) {
        const prelude = `
local __resultPath = "${luaPath(resultFile)}"
local __chunks = {}
local __origWrite = io.write
io.write = function(...)
  for __i = 1, select("#", ...) do
    __chunks[#__chunks + 1] = tostring((select(__i, ...)))
  end
  return io
end
local __ok, __err = pcall(function()
`;
        const epilogue = `
end)
if not __ok then
  __chunks[#__chunks + 1] = "__RESULT__" .. json.encode({ success = false, error = tostring(__err) })
end
io.write = __origWrite
local __f = io.open(__resultPath, "w")
if __f then
  __f:write(table.concat(__chunks))
  __f:close()
end
`;
        return {
            text: prelude + snippet + epilogue,
            preludeLuaLines: prelude.split("\n").length - 1,
        };
    }
    /**
     * farm02 round 2 / F.7: a bare line number cannot locate a failure once the script is gone.
     * Aseprite reports "<file>:<line>: <message>"; emit that line's source text with two lines of
     * context, plus the same line in the caller's own numbering.
     */
    annotateLuaError(rawError, wrappedScript, preludeLuaLines) {
        const m = /:(\d+):/.exec(rawError);
        if (!m)
            return rawError;
        const n = Number(m[1]);
        const lines = wrappedScript.split("\n");
        if (!(n >= 1 && n <= lines.length))
            return rawError;
        const snippetLine = n - preludeLuaLines;
        const from = Math.max(1, n - 2);
        const to = Math.min(lines.length, n + 2);
        const excerpt = [];
        for (let i = from; i <= to; i += 1) {
            excerpt.push(`${i === n ? ">>" : "  "} ${String(i).padStart(5)} | ${lines[i - 1] ?? ""}`);
        }
        const where = snippetLine >= 1
            ? `failing line: snippet line ${snippetLine} (wrapped line ${n})`
            : `failing line: wrapped line ${n}`;
        return `${rawError}\n${where}\n${excerpt.join("\n")}`;
    }
    /**
     * farm02 round 2 / F.6: never erase a failure. Upstream unconditionally unlinked the temp script
     * in `finally`, so the only copy of the code that failed was destroyed and two farm02 failures
     * could only be traced by reasoning backwards from a line number.
     *
     * A failed run is copied to <ASEPRITE_FAIL_DIR or TMP_DIR/_fail>/fail-<stamp>-<id>.{lua,
     * body.lua,out.txt,note.txt} - the same layout as this preset's scripts/aseprite-lua.mjs, so
     * either path leaves evidence a later session can read. ASEPRITE_KEEP_LUA=1 also keeps
     * successful runs. Never throws: losing the echo must not mask the original error.
     */
    async retainFailureEvidence(opts) {
        const keepOk = process.env.ASEPRITE_KEEP_LUA === "1";
        if (!opts.reason && !keepOk)
            return null;
        try {
            const dir = process.env.ASEPRITE_FAIL_DIR || join(TMP_DIR, "_fail");
            await mkdir(dir, { recursive: true });
            const stamp = new Date().toISOString().replace(/[:.]/g, "-");
            const base = `${opts.reason ? "fail" : "ok"}-${stamp}-${opts.id}`;
            const wrappedAt = join(dir, `${base}.lua`);
            await writeFile(wrappedAt, opts.wrappedScript, "utf-8");
            await writeFile(join(dir, `${base}.body.lua`), opts.snippet, "utf-8");
            await writeFile(join(dir, `${base}.out.txt`), opts.rawChannel ?? "", "utf-8");
            await writeFile(join(dir, `${base}.note.txt`), [
                `when      ${new Date().toISOString()}`,
                `reason    ${opts.reason ?? "success (ASEPRITE_KEEP_LUA=1)"}`,
                "",
                `${base}.lua       exactly what Aseprite ran (error line numbers refer to THIS file)`,
                `${base}.body.lua  the caller's snippet, unwrapped (the error maps back to this file)`,
                `${base}.out.txt   raw result channel (may hold partial io.write output)`,
                "",
            ].join("\n"), "utf-8");
            return wrappedAt;
        }
        catch (err) {
            logDebug(`could not retain failure evidence: ${err.message}`);
            return null;
        }
    }
    async runLuaScript(script, inputFile) {
        const aseprite = await this.detectAsepritePath();
        const id = randomUUID();
        const tmpFile = join(TMP_DIR, `aseprite-mcp-${id}.lua`);
        const resultFile = join(TMP_DIR, `aseprite-mcp-${id}.json`);
        // Result channel. The generated scripts report through io.write("__RESULT__" .. ...),
        // which upstream reaches the caller over an stdout pipe. Confined hosts (the DSH
        // sandbox on this machine included) refuse piped-stdio spawns with EPERM, so io.write
        // is buffered inside Lua and flushed to a file instead; the child runs with
        // stdio "ignore" and this process reads the file back. Same payload, no pipe.
        const wrapper = this.buildLuaWrapper(script, resultFile);
        const wrappedScript = wrapper.text;
        // F.6/F.7 bookkeeping: `failure` is set at every failure site and drives retention; `outcome`
        // is the object we return, mutated by the `finally` block to append the evidence path
        // (mutating the same reference is visible to the caller because `finally` runs before the
        // return value is delivered).
        let failure = null;
        let rawChannel = "";
        let outcome = null;
        try {
            await mkdir(TMP_DIR, { recursive: true }).catch(() => { });
            await writeFile(tmpFile, wrappedScript, "utf-8");
            await unlink(resultFile).catch(() => { });
            const args = ["-b"];
            if (inputFile)
                args.push(inputFile);
            args.push("--script", tmpFile);
            logDebug(`Running: ${aseprite} ${args.join(" ")}`);
            let exitError = null;
            await new Promise((resolve, reject) => {
                let child;
                try {
                    child = spawn(aseprite, args, { timeout: 120000, stdio: "ignore" });
                }
                catch (err) {
                    return reject(err);
                }
                child.on("error", (err) => {
                    if (err.code === "ENOENT")
                        return reject(err);
                    exitError = err;
                    resolve();
                });
                child.on("close", (code, signal) => {
                    // A non-zero exit is not fatal on its own: script-level Lua errors are
                    // reported through the result file, and Aseprite exits 1 for them.
                    if (code !== 0) {
                        exitError =
                            exitError ??
                                new Error(`Aseprite exited with code ${code}${signal ? ` (signal ${signal})` : ""}`);
                    }
                    resolve();
                });
            });
            let stdout = "";
            try {
                stdout = await readFile(resultFile, "utf-8");
            }
            catch {
                stdout = "";
            }
            rawChannel = stdout;
            const marker = "__RESULT__";
            const idx = stdout.indexOf(marker);
            if (idx === -1) {
                // No report at all: either the script legitimately printed nothing, or Aseprite
                // died before the flush. Only the latter is an error.
                if (exitError) {
                    const message = `Aseprite failed: ${exitError.message}`;
                    failure = { message, retain: true };
                    const res = { success: false, error: message, success_source: "no_result_channel" };
                    outcome = res;
                    return res;
                }
                const res = {
                    success: true,
                    data: { stdout: stdout.trim() },
                    success_source: "no_result_channel_but_clean_exit",
                };
                outcome = res;
                return res;
            }
            const jsonStr = stdout.slice(idx + marker.length).trim();
            try {
                const parsed = JSON.parse(jsonStr);
                if (parsed?.success === false) {
                    const raw = parsed.error ?? "script reported failure";
                    // A Lua error carries ":<line>:" and gets the source excerpt (F.7); a domain refusal
                    // ("No cel at frame 3") has no line number and is not a script crash.
                    const annotated = this.annotateLuaError(raw, wrappedScript, wrapper.preludeLuaLines);
                    failure = { message: annotated, retain: /:\d+:/.test(raw) };
                    const res = { ...parsed, success: false, error: annotated, success_source: "self_reported" };
                    outcome = res;
                    return res;
                }
                // B3 (2026-10-16): do not infer FAILURE from a missing `success` key. A snippet that reports
                // `{ok="true", ...}` (the farm02 probes do exactly this) ran to completion and wrote its
                // result channel, so it succeeded - the old `parsed.success === true` wrapped it as
                // success:false, which reads as "failed" to every caller. Absent key now means "inferred
                // success, the run completed", and success_source says which it was, so nobody has to guess.
                const selfReported = parsed.success === true;
                const res = {
                    ...parsed,
                    success: true,
                    success_source: selfReported ? "self_reported" : "inferred_completed_run",
                };
                outcome = res;
                return res;
            }
            catch {
                const message = `Failed to parse result JSON: ${jsonStr.slice(0, 200)}`;
                failure = { message, retain: true };
                const res = { success: false, error: message, success_source: "unparseable_result" };
                outcome = res;
                return res;
            }
        }
        catch (err) {
            const message = err instanceof Error ? err.message : String(err);
            failure = failure ?? { message, retain: true };
            outcome = outcome ?? { success: false, error: message };
            throw err;
        }
        finally {
            const kept = await this.retainFailureEvidence({
                id,
                wrappedScript,
                snippet: script,
                rawChannel,
                reason: failure?.retain ? failure.message.split("\n")[0] : null,
            });
            if (kept && outcome && outcome.success === false) {
                outcome.error = `${outcome.error ?? "failed"}\nevidence: ${kept} (wrapped .lua + unwrapped .body.lua + raw .out.txt)`;
            }
            await unlink(tmpFile).catch(() => { });
            await unlink(resultFile).catch(() => { });
        }
    }
    async runCliCommand(cliArgs) {
        const aseprite = await this.detectAsepritePath();
        return new Promise((resolve, reject) => {
            // These CLI forms write their results to files, so nothing is lost by running
            // them with stdio "ignore" (see runLuaScript for why a pipe is not available).
            let child;
            try {
                child = spawn(aseprite, cliArgs, { timeout: 300000, stdio: "ignore" });
            }
            catch (err) {
                return reject(err);
            }
            child.on("error", (err) => reject(err));
            child.on("close", (code, signal) => {
                if (code !== 0) {
                    return reject(new Error(`Aseprite exited with code ${code}${signal ? ` (signal ${signal})` : ""}`));
                }
                resolve({ stdout: "", stderr: "" });
            });
        });
    }
    /** Size of a file a tool claims to have written, or null when it is missing. */
    async fileSize(path) {
        try {
            return (await stat(path)).size;
        }
        catch {
            return null;
        }
    }
    // ---- Response helpers ---------------------------------------------------
    ok(data) {
        return {
            content: [
                { type: "text", text: typeof data === "string" ? data : JSON.stringify(data, null, 2) },
            ],
        };
    }
    error(message, solutions = []) {
        const parts = [
            { type: "text", text: message },
        ];
        if (solutions.length > 0) {
            parts.push({
                type: "text",
                text: "Possible solutions:\n- " + solutions.join("\n- "),
            });
        }
        return { content: parts, isError: true };
    }
    requireParam(args, name) {
        const val = args[name] ?? args[this.toSnakeCase(name)] ?? args[this.toCamelCase(name)];
        if (val === undefined || val === null || val === "") {
            throw new Error(`Missing required parameter: ${name}`);
        }
        return String(val);
    }
    optParam(args, name) {
        const val = args[name] ?? args[this.toSnakeCase(name)] ?? args[this.toCamelCase(name)];
        return val !== undefined && val !== null ? String(val) : undefined;
    }
    numParam(args, name, def) {
        const val = args[name] ?? args[this.toSnakeCase(name)] ?? args[this.toCamelCase(name)];
        if (val === undefined || val === null)
            return def;
        return Number(val);
    }
    /**
     * Validate a tool's own `inputSchema.required` before its handler interpolates values into
     * Lua. Without this guard a mistyped required key becomes the literal `undefined` inside the
     * generated script, and the failure surfaces far from its cause: either a confusing Lua error
     * (`attempt to perform arithmetic on a nil value (upvalue 'cx')`) or, worse, a success reply
     * carrying empty values (`center: []`). Alias handling mirrors requireParam.
     */
    missingRequired(tool, args) {
        const def = this.toolDefinitions().find((t) => t.name === tool);
        const schema = def?.inputSchema;
        const required = schema?.required ?? [];
        const present = (n) => [n, this.toSnakeCase(n), this.toCamelCase(n)].some((k) => {
            const v = args[k];
            return v !== undefined && v !== null && v !== "";
        });
        const missing = required.filter((n) => !present(n));
        if (!missing.length)
            return null;
        const accepted = Object.keys(schema?.properties ?? {});
        return (`Missing required parameter(s) for "${tool}": ${missing.join(", ")}.` +
            (accepted.length ? ` Accepted parameters: ${accepted.join(", ")}.` : ""));
    }
    boolParam(args, name, def) {
        const val = args[name] ?? args[this.toSnakeCase(name)] ?? args[this.toCamelCase(name)];
        if (val === undefined || val === null)
            return def;
        if (typeof val === "boolean")
            return val;
        return val === "true" || val === "1";
    }
    toSnakeCase(s) {
        return s.replace(/([A-Z])/g, "_$1").toLowerCase();
    }
    toCamelCase(s) {
        return s.replace(/_([a-z])/g, (_, c) => c.toUpperCase());
    }
    luaColor(color) {
        if (typeof color === "number")
            return `Color(${color})`;
        if (typeof color === "string") {
            if (color.startsWith("#")) {
                const hex = color.slice(1);
                const r = parseInt(hex.slice(0, 2), 16);
                const g = parseInt(hex.slice(2, 4), 16);
                const b = parseInt(hex.slice(4, 6), 16);
                const a = hex.length >= 8 ? parseInt(hex.slice(6, 8), 16) : 255;
                return `Color{ r=${r}, g=${g}, b=${b}, a=${a} }`;
            }
            return `Color(${color})`;
        }
        if (typeof color === "object" && color !== null) {
            const c = color;
            return `Color{ r=${c.r ?? 0}, g=${c.g ?? 0}, b=${c.b ?? 0}, a=${c.a ?? 255} }`;
        }
        return "Color{ r=0, g=0, b=0, a=255 }";
    }
    /**
     * F.1 support: resolve a color into something a Lua snapshot can be COMPARED against.
     * `luaColor` alone cannot do this - a palette index and an RGB triple are matched differently,
     * and an INDEXED cel stores indices only, so an RGB outline has to be located in the palette
     * first (or the "is this pixel already outline-coloured" test silently answers false for ever).
     * r/g/b = -1 means "not statically resolvable" (e.g. the string "app.fgColor").
     */
    luaColorRef(color) {
        const lua = this.luaColor(color);
        if (typeof color === "number") {
            return { lua, kind: "index", index: color, r: -1, g: -1, b: -1 };
        }
        if (typeof color === "string" && color.startsWith("#")) {
            const hex = color.slice(1);
            return {
                lua,
                kind: "rgb",
                index: -1,
                r: parseInt(hex.slice(0, 2), 16),
                g: parseInt(hex.slice(2, 4), 16),
                b: parseInt(hex.slice(4, 6), 16),
            };
        }
        if (typeof color === "object" && color !== null) {
            const c = color;
            return { lua, kind: "rgb", index: -1, r: c.r ?? 0, g: c.g ?? 0, b: c.b ?? 0 };
        }
        return { lua, kind: "rgb", index: -1, r: -1, g: -1, b: -1 };
    }
    // ---- Tool definitions ---------------------------------------------------
    toolDefinitions() {
        return [
            // ---- Sprite Management ----
            {
                name: "create_sprite",
                description: "Create a new Aseprite sprite file",
                inputSchema: {
                    type: "object",
                    properties: {
                        width: { type: "number", description: "Width in pixels" },
                        height: { type: "number", description: "Height in pixels" },
                        colorMode: { type: "string", enum: ["rgb", "grayscale", "indexed"], description: "Color mode (default: rgb)" },
                        savePath: { type: "string", description: "Path to save the new sprite file" },
                    },
                    required: ["width", "height", "savePath"],
                },
            },
            {
                name: "open_sprite",
                description: "Open a sprite file and return its metadata",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Path to the sprite file (.aseprite, .ase, .png, etc.)" },
                    },
                    required: ["filePath"],
                },
            },
            {
                name: "save_sprite",
                description: "Save/convert a sprite to a different format or path",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Source sprite file" },
                        savePath: { type: "string", description: "Destination path (format inferred from extension)" },
                    },
                    required: ["filePath", "savePath"],
                },
            },
            {
                name: "get_sprite_info",
                description: "Get detailed sprite metadata: dimensions, layers, frames, tags, palette, slices",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Path to the sprite file" },
                    },
                    required: ["filePath"],
                },
            },
            {
                name: "resize_sprite",
                description: "Resize a sprite to new dimensions",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Source sprite file" },
                        width: { type: "number", description: "New width in pixels" },
                        height: { type: "number", description: "New height in pixels" },
                        savePath: { type: "string", description: "Path to save (defaults to overwrite source)" },
                    },
                    required: ["filePath", "width", "height"],
                },
            },
            {
                name: "crop_sprite",
                description: "Crop a sprite to given bounds",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Source sprite file" },
                        x: { type: "number", description: "Left edge" },
                        y: { type: "number", description: "Top edge" },
                        width: { type: "number", description: "Crop width" },
                        height: { type: "number", description: "Crop height" },
                        savePath: { type: "string", description: "Path to save (defaults to overwrite source)" },
                    },
                    required: ["filePath", "x", "y", "width", "height"],
                },
            },
            // ---- Layer Management ----
            {
                name: "add_layer",
                description: "Add a new layer to a sprite",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        layerName: { type: "string", description: "Name for the new layer" },
                        isGroup: { type: "boolean", description: "Create a group layer instead of image layer" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "layerName"],
                },
            },
            {
                name: "remove_layer",
                description: "Remove a layer from a sprite by name",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        layerName: { type: "string", description: "Name of the layer to remove" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "layerName"],
                },
            },
            {
                name: "list_layers",
                description: "List all layers in a sprite with their properties",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                    },
                    required: ["filePath"],
                },
            },
            {
                name: "set_layer_properties",
                description: "Set properties on a layer (visibility, opacity, blend mode, name)",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        layerName: { type: "string", description: "Layer to modify" },
                        newName: { type: "string", description: "Rename the layer" },
                        visible: { type: "boolean", description: "Set visibility" },
                        opacity: { type: "number", description: "Set opacity (0-255)" },
                        blendMode: { type: "string", description: "Set blend mode (normal, multiply, screen, overlay, darken, lighten, color_dodge, color_burn, hard_light, soft_light, difference, exclusion, hue, saturation, color, luminosity)" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "layerName"],
                },
            },
            // ---- Frame & Animation ----
            {
                name: "add_frame",
                description: "Add new frame(s) to a sprite",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        count: { type: "number", description: "Number of frames to add (default: 1)" },
                        afterFrame: { type: "number", description: "Insert after this frame number (1-indexed). If omitted, appends at end." },
                        empty: { type: "boolean", description: "Create empty frames (default: false, copies previous)" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath"],
                },
            },
            {
                name: "remove_frame",
                description: "Remove a frame from a sprite",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        frameNumber: { type: "number", description: "Frame number to remove (1-indexed)" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "frameNumber"],
                },
            },
            {
                name: "set_frame_duration",
                description: "Set the duration of a frame in milliseconds",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        frameNumber: { type: "number", description: "Frame number (1-indexed)" },
                        durationMs: { type: "number", description: "Duration in milliseconds" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "frameNumber", "durationMs"],
                },
            },
            {
                name: "list_frames",
                description: "List all frames in a sprite with durations",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                    },
                    required: ["filePath"],
                },
            },
            // ---- Animation Tags ----
            {
                name: "create_tag",
                description: "Create an animation tag on a range of frames",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        tagName: { type: "string", description: "Name for the tag" },
                        fromFrame: { type: "number", description: "Start frame (1-indexed)" },
                        toFrame: { type: "number", description: "End frame (1-indexed)" },
                        aniDir: { type: "string", enum: ["forward", "reverse", "pingpong", "pingpong_reverse"], description: "Animation direction (default: forward)" },
                        color: { type: "string", description: "Tag color as hex (#RRGGBB)" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "tagName", "fromFrame", "toFrame"],
                },
            },
            {
                name: "remove_tag",
                description: "Remove an animation tag by name",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        tagName: { type: "string", description: "Name of the tag to remove" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "tagName"],
                },
            },
            {
                name: "list_tags",
                description: "List all animation tags in a sprite",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                    },
                    required: ["filePath"],
                },
            },
            // ---- Drawing ----
            {
                name: "draw_pixels",
                description: "Draw individual pixels on a specific layer and frame",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        layerName: { type: "string", description: "Target layer name" },
                        frameNumber: { type: "number", description: "Frame number (1-indexed, default: 1)" },
                        pixels: {
                            type: "array",
                            items: {
                                type: "object",
                                properties: {
                                    x: { type: "number" },
                                    y: { type: "number" },
                                    color: { description: "Color as hex string (#RRGGBB), palette index, or {r,g,b,a} object" },
                                },
                                required: ["x", "y", "color"],
                            },
                            description: "Array of pixels to draw",
                        },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "pixels"],
                },
            },
            {
                name: "draw_rect",
                description: "Draw a rectangle on a specific layer and frame",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        layerName: { type: "string", description: "Target layer name" },
                        frameNumber: { type: "number", description: "Frame number (1-indexed, default: 1)" },
                        x: { type: "number", description: "Left edge" },
                        y: { type: "number", description: "Top edge" },
                        width: { type: "number", description: "Rectangle width" },
                        height: { type: "number", description: "Rectangle height" },
                        color: { description: "Color as hex string (#RRGGBB), palette index, or {r,g,b,a}" },
                        filled: { type: "boolean", description: "Fill the rectangle (default: true)" },
                        thickness: { type: "number", description: "Stroke thickness in pixels (default: 1). Uses native Aseprite brush when > 1." },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "x", "y", "width", "height", "color"],
                },
            },
            {
                name: "draw_circle",
                description: "Draw a circle on a specific layer and frame",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        layerName: { type: "string", description: "Target layer name" },
                        frameNumber: { type: "number", description: "Frame number (1-indexed, default: 1)" },
                        centerX: { type: "number", description: "Center X coordinate" },
                        centerY: { type: "number", description: "Center Y coordinate" },
                        radius: { type: "number", description: "Circle radius" },
                        color: { description: "Color as hex string (#RRGGBB), palette index, or {r,g,b,a}" },
                        filled: { type: "boolean", description: "Fill the circle (default: true)" },
                        thickness: { type: "number", description: "Stroke thickness in pixels (default: 1). Uses native Aseprite brush when > 1." },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "centerX", "centerY", "radius", "color"],
                },
            },
            {
                name: "draw_line",
                description: "Draw a line between two points on a specific layer and frame. " +
                    "For thickness=1 the walk is Bresenham, which is 8-CONNECTED on diagonal steps (the corner pixel is missing); " +
                    "a 4-neighbour silhouette-connectivity gate reads that as 2-4 separate components - the farm02 handle failure. " +
                    "connect4=true adds the L corner between diagonal steps so the 1px line is 4-connected; it only ADDS corners " +
                    "(thickness=1 stays a 1px line), and it is OPT-IN because it changes the pixels a Bresenham caller gets. " +
                    "The result always reports the line's own component count (components4), the corners added and pointsPlotted, " +
                    "and a `warning` when a 1px line came out 8-connected - so pass connect4=true whenever a silhouette gate " +
                    "counts 4-neighbours. thickness>1 uses the native brush and ignores the flag.",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        layerName: { type: "string", description: "Target layer name" },
                        frameNumber: { type: "number", description: "Frame number (1-indexed, default: 1)" },
                        x1: { type: "number", description: "Start X" },
                        y1: { type: "number", description: "Start Y" },
                        x2: { type: "number", description: "End X" },
                        y2: { type: "number", description: "End Y" },
                        color: { description: "Color as hex string (#RRGGBB), palette index, or {r,g,b,a}" },
                        thickness: { type: "number", description: "Line thickness in pixels (default: 1). Uses native Aseprite brush when > 1." },
                        connect4: {
                            type: "boolean",
                            description: "Add L corners on diagonal steps so a 1px line is 4-connected (default: false = exact Bresenham).",
                        },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "x1", "y1", "x2", "y2", "color"],
                },
            },
            {
                name: "draw_ellipse",
                description: "Draw an ellipse on a specific layer and frame using Aseprite's native ellipse tool",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        layerName: { type: "string", description: "Target layer name" },
                        frameNumber: { type: "number", description: "Frame number (1-indexed, default: 1)" },
                        x: { type: "number", description: "Left edge of bounding box" },
                        y: { type: "number", description: "Top edge of bounding box" },
                        width: { type: "number", description: "Ellipse bounding box width" },
                        height: { type: "number", description: "Ellipse bounding box height" },
                        color: { description: "Color as hex string (#RRGGBB), palette index, or {r,g,b,a}" },
                        filled: { type: "boolean", description: "Fill the ellipse (default: true)" },
                        thickness: { type: "number", description: "Stroke thickness in pixels (default: 1)" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "x", "y", "width", "height", "color"],
                },
            },
            {
                name: "fill_area",
                description: "Flood fill an area starting from a point using Aseprite's native paint bucket tool",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        layerName: { type: "string", description: "Target layer name" },
                        frameNumber: { type: "number", description: "Frame number (1-indexed, default: 1)" },
                        x: { type: "number", description: "X coordinate of the fill starting point" },
                        y: { type: "number", description: "Y coordinate of the fill starting point" },
                        color: { description: "Fill color as hex string (#RRGGBB), palette index, or {r,g,b,a}" },
                        tolerance: { type: "number", description: "Color tolerance for flood fill (0-255, default: 0)" },
                        contiguous: { type: "boolean", description: "Only fill contiguous pixels (default: true). If false, fills all matching pixels." },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "x", "y", "color"],
                },
            },
            {
                name: "replace_color",
                description: "Replace all occurrences of one color with another in a sprite",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        fromColor: { description: "Source color to replace (hex, palette index, or {r,g,b,a})" },
                        toColor: { description: "Replacement color (hex, palette index, or {r,g,b,a})" },
                        layerName: { type: "string", description: "Only replace on this layer (optional, all layers if omitted)" },
                        frameNumber: { type: "number", description: "Only replace on this frame (optional, all frames if omitted)" },
                        tolerance: { type: "number", description: "Color matching tolerance (0-255, default: 0)" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "fromColor", "toColor"],
                },
            },
            {
                name: "flip_sprite",
                description: "Flip the entire sprite canvas horizontally or vertically",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        direction: { type: "string", enum: ["horizontal", "vertical"], description: "Flip direction" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "direction"],
                },
            },
            {
                name: "rotate_sprite",
                description: "Rotate the sprite canvas by 90, 180, or 270 degrees",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        angle: { type: "number", enum: [90, 180, 270], description: "Rotation angle in degrees (clockwise)" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "angle"],
                },
            },
            {
                name: "flatten_layers",
                description: "Flatten all layers in a sprite into a single layer",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath"],
                },
            },
            {
                name: "merge_down",
                description: "Merge a layer with the one below it",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        layerName: { type: "string", description: "Layer to merge down (will merge with layer below)" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "layerName"],
                },
            },
            {
                name: "outline",
                description: "Add an outline around non-transparent pixels on a layer. " +
                    "noStack (default TRUE) skips a candidate pixel whose BELOW or LEFT/RIGHT neighbour already carries the " +
                    "outline color in the pre-draw snapshot - the 2x2 all-black blocks ('outline got thicker') that automated " +
                    "outlining produced twice in one farm02 night. Judged on the snapshot, never on this run's writes, so the " +
                    "result does not depend on iteration order; pass noStack=false for the old unconditional behaviour. " +
                    "Returns stackedSkipped plus a self-report: existingOutlinePixels counts the snapshot pixels that already " +
                    "carry the color, and guardNote says so when that count is 0 - then stackedSkipped=0 means 'the guard was " +
                    "inert', NOT 'the layout was clean'. Transparency is mode-aware (INDEXED compares spr.transparentColor, " +
                    "RGB/GRAY compares alpha), so it works on an indexed character sheet.",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        layerName: { type: "string", description: "Target layer name" },
                        frameNumber: { type: "number", description: "Frame number (1-indexed, default: 1)" },
                        color: { description: "Outline color as hex string (#RRGGBB), palette index, or {r,g,b,a}" },
                        noStack: {
                            type: "boolean",
                            description: "Skip candidates stacked under/beside an existing outline pixel (default: true). false restores the old behaviour.",
                        },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "color"],
                },
            },
            // ---- Palette ----
            {
                name: "get_palette",
                description: "Get all colors in a sprite's palette",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                    },
                    required: ["filePath"],
                },
            },
            {
                name: "set_palette_colors",
                description: "Set colors in a sprite's palette",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        colors: {
                            type: "array",
                            items: {
                                type: "object",
                                properties: {
                                    index: { type: "number", description: "Palette index" },
                                    r: { type: "number" },
                                    g: { type: "number" },
                                    b: { type: "number" },
                                    a: { type: "number", description: "Alpha (default: 255)" },
                                },
                                required: ["index", "r", "g", "b"],
                            },
                            description: "Array of palette entries to set",
                        },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "colors"],
                },
            },
            {
                name: "load_palette",
                description: "Load a palette from a file into a sprite",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        palettePath: { type: "string", description: "Path to palette file (.gpl, .pal, .aseprite, .png, etc.)" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "palettePath"],
                },
            },
            {
                name: "resize_palette",
                description: "Resize a sprite's palette to a new number of entries. Returns entries (the capacity AFTER the " +
                    "resize) and entriesBefore, because a Palette has NO size/length/count/getSize/numColors accessor - " +
                    "the Lua # operator (#pal) is the only way to read the capacity, and it is not visible from a script " +
                    "you are about to write. Use this return value instead of guessing, then set_palette_colors.",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        newSize: { type: "number", description: "New palette size" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "newSize"],
                },
            },
            // ---- Cel Operations ----
            {
                name: "move_cel",
                description: "Move a cel to a new position",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        layerName: { type: "string", description: "Layer name" },
                        frameNumber: { type: "number", description: "Frame number (1-indexed, default: 1)" },
                        x: { type: "number", description: "New X position" },
                        y: { type: "number", description: "New Y position" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "layerName", "x", "y"],
                },
            },
            {
                name: "set_cel_opacity",
                description: "Set the opacity of a cel",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        layerName: { type: "string", description: "Layer name" },
                        frameNumber: { type: "number", description: "Frame number (1-indexed, default: 1)" },
                        opacity: { type: "number", description: "Opacity (0-255)" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "layerName", "opacity"],
                },
            },
            {
                name: "clear_cel",
                description: "Clear (delete) a cel at a specific layer and frame",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        layerName: { type: "string", description: "Layer name" },
                        frameNumber: { type: "number", description: "Frame number (1-indexed, default: 1)" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "layerName"],
                },
            },
            // ---- Export ----
            {
                name: "export_sprite_sheet",
                description: "Export a sprite as a sprite sheet image with optional JSON data. " +
                    "BATCH (2026-10-16): pass outputs=[{filePath, outputImage, ...}] to export many sheets in ONE call - per-item " +
                    "keys override the top-level defaults (sheetType/columns/padding/trim/...), so shared options are written once. " +
                    "The reply reports count/ok_count/failed_count/all_ok and one entry per item, and a batch where every item failed " +
                    "returns an error rather than a success-looking payload.",
                inputSchema: {
                    type: "object",
                    properties: {
                        outputs: {
                            type: "array",
                            description: "Batch mode: one entry per sheet. Each needs filePath + outputImage; other keys override the top-level ones.",
                            items: {
                                type: "object",
                                properties: {
                                    filePath: { type: "string" },
                                    outputImage: { type: "string" },
                                    dataFile: { type: "string" },
                                    sheetType: { type: "string", enum: ["horizontal", "vertical", "rows", "columns", "packed"] },
                                    columns: { type: "number" },
                                    rows: { type: "number" },
                                    borderPadding: { type: "number" },
                                    shapePadding: { type: "number" },
                                    innerPadding: { type: "number" },
                                    trim: { type: "boolean" },
                                    mergeDuplicates: { type: "boolean" },
                                    layer: { type: "string" },
                                    tag: { type: "string" },
                                    splitLayers: { type: "boolean" },
                                },
                                required: ["filePath", "outputImage"],
                            },
                        },
                        filePath: { type: "string", description: "Source sprite file" },
                        outputImage: { type: "string", description: "Output sprite sheet image path (.png)" },
                        dataFile: { type: "string", description: "Output JSON data file path (optional)" },
                        sheetType: { type: "string", enum: ["horizontal", "vertical", "rows", "columns", "packed"], description: "Sheet layout type (default: rows)" },
                        columns: { type: "number", description: "Fixed number of columns" },
                        rows: { type: "number", description: "Fixed number of rows" },
                        borderPadding: { type: "number", description: "Padding on texture borders" },
                        shapePadding: { type: "number", description: "Padding between frames" },
                        innerPadding: { type: "number", description: "Padding inside each frame" },
                        trim: { type: "boolean", description: "Trim transparent edges" },
                        mergeDuplicates: { type: "boolean", description: "Merge duplicate frames" },
                        layer: { type: "string", description: "Export only this layer" },
                        tag: { type: "string", description: "Export only this tag" },
                        splitLayers: { type: "boolean", description: "Split each layer into separate images" },
                    },
                    // Not required: batch mode passes outputs[] instead (validated in the handler).
                    required: [],
                },
            },
            {
                name: "export_frame",
                description: "Export a single frame as an image file",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Source sprite file" },
                        frameNumber: { type: "number", description: "Frame to export (1-indexed, default: 1)" },
                        outputPath: { type: "string", description: "Output image path" },
                        layerName: { type: "string", description: "Export only this layer (optional)" },
                    },
                    required: ["filePath", "outputPath"],
                },
            },
            {
                name: "export_layers",
                description: "Export each layer as a separate image file",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Source sprite file" },
                        outputPattern: { type: "string", description: "Output path pattern with {layer} placeholder (e.g. 'output-{layer}.png')" },
                        frameNumber: { type: "number", description: "Export this frame only (1-indexed)" },
                    },
                    required: ["filePath", "outputPattern"],
                },
            },
            // ---- Slices ----
            {
                name: "create_slice",
                description: "Create a named slice region in a sprite",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        sliceName: { type: "string", description: "Name for the slice" },
                        x: { type: "number", description: "Left edge" },
                        y: { type: "number", description: "Top edge" },
                        width: { type: "number", description: "Slice width" },
                        height: { type: "number", description: "Slice height" },
                        pivotX: { type: "number", description: "Pivot X (optional)" },
                        pivotY: { type: "number", description: "Pivot Y (optional)" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "sliceName", "x", "y", "width", "height"],
                },
            },
            {
                name: "remove_slice",
                description: "Remove a slice by name",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        sliceName: { type: "string", description: "Name of the slice to remove" },
                        savePath: { type: "string", description: "Path to save" },
                    },
                    required: ["filePath", "sliceName"],
                },
            },
            // ---- Utility ----
            {
                name: "run_script",
                description: "Execute an arbitrary Lua script in Aseprite (for advanced operations). " +
                    "A failed run is retained (see the error's evidence path) and the error carries the offending line's source. " +
                    "withMeter=true injects the MCP meters' own Lua core (the same source string build/index.js uses) ahead of your " +
                    "snippet, so you can call __px(spr, v, pal) / __scan_cel(spr, cel) / __scan_all / __alpha_fp / __composite / " +
                    "__bbox / __lstar / __hsv directly instead of re-implementing them - in particular __px is the mode-aware " +
                    "decoder that INDEXED sheets require. Snippets that report only their own keys (no `success`) are still " +
                    "successful runs: the reply says success_source = inferred_completed_run.",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file to open before running (optional)" },
                        script: { type: "string", description: "Lua script code to execute" },
                        withMeter: {
                            type: "boolean",
                            description: "Inject the meters' Lua core (__px/__scan_cel/__composite/...) ahead of the snippet (default: false)",
                        },
                    },
                    required: ["script"],
                },
            },
            {
                name: "get_aseprite_version",
                description: "Get Aseprite version information",
                inputSchema: {
                    type: "object",
                    properties: {},
                },
            },
            // ---- Pixel-art primitives -------------------------------------------
            // Added 2026-10-14 to kill the "export -> Python -> import" round-trip that every
            // mask/layer/palette operation used to need. Each one is grounded in a live-app
            // probe (see aseprite/_probe/PRIMITIVES.md): app.command.ChangePixelFormat,
            // app.command.InvertMask/DeselectMask, LoadPalette/SavePalette/ColorQuantization/
            // ReplaceColor exist; ApplyMask/NewMask/SetPaletteEntry/ChangeColorMode do NOT.
            {
                name: "indexify",
                description: "Convert a sprite to INDEXED color mode natively (app.command.ChangePixelFormat), no external PNG round-trip.",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file to convert" },
                        savePath: { type: "string", description: "Where to save (defaults to source file)" },
                        colorCount: { type: "number", description: "Target color count (omit to keep the sprite palette)" },
                        dithering: {
                            type: "string",
                            enum: ["none", "ordered", "error-diffusion"],
                            description: "Dithering (default: none)",
                        },
                    },
                    required: ["filePath"],
                },
            },
            {
                name: "get_layer_image",
                description: "Export one layer's cel image to a PNG file (layer image read-out).",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        layerName: { type: "string", description: "Layer name (default: first layer)" },
                        frameNumber: { type: "number", description: "Frame number, 1-based (default: 1)" },
                        savePath: { type: "string", description: "PNG output path" },
                    },
                    required: ["filePath", "savePath"],
                },
            },
            {
                name: "set_layer_image",
                description: "Replace one layer's cel image from a PNG file (layer image write-in).",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        layerName: { type: "string", description: "Layer name (default: first layer)" },
                        frameNumber: { type: "number", description: "Frame number, 1-based (default: 1)" },
                        imagePath: { type: "string", description: "PNG to load as the new cel image" },
                        savePath: { type: "string", description: "Where to save (defaults to source file)" },
                    },
                    required: ["filePath", "imagePath"],
                },
            },
            {
                name: "snap_to_palette",
                description: "Snap every pixel of an RGB sprite to the nearest palette color (skips the transparent slot). Unique-color cached.",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file (must not be INDEXED yet)" },
                        savePath: { type: "string", description: "Where to save (defaults to source file)" },
                    },
                    required: ["filePath"],
                },
            },
            {
                name: "remap_palette",
                description: "Load a palette file into the sprite (app.command.LoadPalette) and optionally remap all pixels to it.",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        palettePath: { type: "string", description: "Palette file (.gpl, .aseprite, .png)" },
                        remapPixels: { type: "boolean", description: "Also snap pixels to the new palette (default: false)" },
                        savePath: { type: "string", description: "Where to save (defaults to source file)" },
                    },
                    required: ["filePath", "palettePath"],
                },
            },
            {
                name: "apply_mask",
                description: "Edit one layer only inside a mask PNG (white + opaque = inside). No ApplyMask command exists in this Aseprite, so it is done pixel-wise.",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file" },
                        maskPath: { type: "string", description: "Mask PNG (white+opaque = inside)" },
                        layerName: { type: "string", description: "Layer name (default: first layer)" },
                        frameNumber: { type: "number", description: "Frame number, 1-based (default: 1)" },
                        mode: {
                            type: "string",
                            enum: ["fill", "clear", "keep"],
                            description: "fill = paint color inside mask; clear = erase inside; keep = erase OUTSIDE (default: fill)",
                        },
                        color: { type: "string", description: "Fill color as #rrggbb (used by mode=fill)" },
                        savePath: { type: "string", description: "Where to save (defaults to source file)" },
                    },
                    required: ["filePath", "maskPath"],
                },
            },
            {
                name: "verify_artifact",
                description: "Run the machine-checkable gates from spec/pixel-spec.json on an artifact and write a <artifact>.verify.json sidecar. " +
                    "Validates the checker itself (--selftest) FIRST, so a broken meter can never certify anything. " +
                    "Every gate DECLARES THE FEATURE IT MEASURES and is CHECKER_INVALID (never PASS) when it cannot see that feature: " +
                    "a fully transparent artifact makes every ratio vacuously 0, an empty palette cannot judge 'outside palette', and a " +
                    "declared probes[] color that hits 0 pixels means the gate is blind. That is the farm02 failure where a hardcoded " +
                    "outline color (#131313 vs the sheet's real #171310) reported '0 thick outline blocks' in five spec documents. " +
                    "probes[] declares the colors a gate requires: { color: \"#rrggbb\", minPixels?, id? }; each one reports its actual " +
                    "hit count in feature_hits and the gate list, and 0 hits refuses the whole verdict. " +
                    "Gates it cannot check are listed explicitly as gates_not_machine_checkable (no silent 'unverified = fine').",
                inputSchema: {
                    type: "object",
                    properties: {
                        artifactPath: { type: "string", description: "PNG artifact to verify" },
                        palettePath: { type: "string", description: "Optional .gpl; adds the out-of-palette gate" },
                        probes: {
                            type: "array",
                            description: "Features this run's gates require to exist in the artifact. 0 hits => CHECKER_INVALID (the gate is blind), not PASS.",
                            items: {
                                type: "object",
                                properties: {
                                    color: { type: "string", description: "#rrggbb the gate looks for" },
                                    minPixels: { type: "number", description: "Require at least this many hits (default 1)" },
                                    id: { type: "string", description: "Which gate this feature belongs to, e.g. outline.not_thicker" },
                                },
                                required: ["color"],
                            },
                        },
                        sidecar: { type: "boolean", description: "Write <artifact>.verify.json (default: true)" },
                    },
                    required: ["artifactPath"],
                },
            },
            // ---- Batch-edit + verification family (farm02 实战派生, see docs/MCP-TOOLS-ADD-2026-10-15.md) ----
            {
                name: "crop_cels",
                description: "TRUE crop: rebuild every layer's cel image + sprite canvas together, so content outside the rect is DESTROYED. " +
                    "crop_sprite / Lua spr:crop only change the canvas and keep the cel data (shrinking then re-expanding is a no-op: " +
                    "measured 27210 -> 27210 px), so any crop that must actually discard pixels needs this tool. " +
                    "Rect semantics: omit rect to crop to the content bbox; rect may extend beyond the current canvas (transparent padding); " +
                    "anchor='content' expands the given rect to include all content; padToTile rounds the canvas up to a multiple " +
                    "(extra space split around content with anchor='content', appended right/bottom with 'top-left'). " +
                    "Applies to every layer x every frame. Reads old cels through cel.bounds (never assumes cel.image origin == sprite origin). " +
                    "lossless = pixel_after == pixel_inside_rect_before, and it is a DATA-INTEGRITY gate only: it proves the rebuild neither " +
                    "dropped pixels inside the rect nor kept any outside. It is a pixel COUNT, so it is blind to a purely spatial shift, and it " +
                    "does NOT judge whether that rect should have been cropped at all - read pixel_before and discarded_pixels to see how much " +
                    "content the rect throws away, and use diff_sprites against an expected artifact as the positional gate.",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Source sprite file (.aseprite/.ase/.png...)" },
                        rect: {
                            type: "object",
                            description: "Crop rect {x,y,w,h} in sprite coords. Omit = content bbox.",
                            properties: {
                                x: { type: "number" },
                                y: { type: "number" },
                                w: { type: "number" },
                                h: { type: "number" },
                            },
                            required: ["x", "y", "w", "h"],
                        },
                        anchor: {
                            type: "string",
                            enum: ["top-left", "content"],
                            description: "top-left (default): keep rect origin. content: expand rect to cover all content and centre padToTile slack.",
                        },
                        padToTile: {
                            type: "number",
                            description: "Round the resulting canvas width/height up to a multiple of this (e.g. 32). No scaling, only padding.",
                        },
                        savePath: { type: "string", description: "Path to save (defaults to overwrite source)" },
                    },
                    required: ["filePath"],
                },
            },
            {
                name: "measure_sprite",
                description: "Batch measurement that get_sprite_info/list_layers do not provide: content bbox, per-layer pixel counts + bboxes, " +
                    "exact color histogram, out-of-palette count, and CIE L*/HSV tone statistics. " +
                    "Color/tone stats are computed on the COMPOSITE of one frame (engine flatten when the sprite has a single frame, " +
                    "otherwise the topmost opaque pixel per position - reported as stats_basis; both count each position once). " +
                    "L* = CIE L* (D65, Y from 0.2126/0.7152/0.0722 linear sRGB) and hue/sat = HSV, matching tools/ref-colors.py, " +
                    "so numbers are comparable with the project's existing analyses. Region restricts every measurement.",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file to measure (read-only; never saved)" },
                        region: {
                            type: "object",
                            description: "Restrict all measurements to {x,y,w,h} in sprite coords",
                            properties: {
                                x: { type: "number" },
                                y: { type: "number" },
                                w: { type: "number" },
                                h: { type: "number" },
                            },
                            required: ["x", "y", "w", "h"],
                        },
                        topColors: { type: "number", description: "How many of the most frequent colors to detail (default 12)" },
                        frameNumber: { type: "number", description: "Frame to measure (default 1)" },
                    },
                    required: ["filePath"],
                },
            },
            {
                name: "measure_layers",
                description: "farm02 round 2 / F.4: one call for the whole gate kit that six Lua scripts each re-implemented by hand " +
                    "(build-action-water.lua / -west / -front / -back / -hoe / mirror-front-to-right-hand.lua). " +
                    "Per measured frame it returns: content bbox, lowest row, opaque pixel count, 4-connected component count with a " +
                    "per-component detail list (size + bbox, the 'n=16(12,20..13,21)' form those scripts printed), solid 2x2 block count, " +
                    "the outline-coloured 2x2 block count (outlineColor, the 'thick2x2' gate'), an alpha fingerprint (opaque/alpha_sum/" +
                    "weighted), the change count against the previous measured frame, and per-layer pixel counts. " +
                    "Layer selection names must exist (an unknown name is refused, not silently skipped); frames default to every frame. " +
                    "Read-only: the sprite is never saved. Metrics are computed on the COMPOSITE of each frame; per-layer counts come " +
                    "from that frame's own cels, so a hidden-under-another-layer pixel still counts in layer_pixels (use opaque_px for " +
                    "what is visible). diffCycle (default true) compares the first frame against the last, the cyclic convention those " +
                    "scripts used; pass false to leave the first frame without a predecessor. A pixel counts as changed when its coverage " +
                    "differs or, when opaque on both sides, when its RGB differs - the same rule as diff_sprites.",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Sprite file to measure (read-only; never saved)" },
                        frames: {
                            type: "array",
                            items: { type: "number" },
                            description: "1-indexed frames to measure (default: all frames)",
                        },
                        layers: {
                            type: "array",
                            items: { type: "string" },
                            description: "Layer names to count pixels for (default: all non-group layers). Unknown names are refused.",
                        },
                        outlineColor: {
                            description: "Optional outline color (hex, palette index, or {r,g,b,a}); adds thick2x2 = 2x2 blocks fully covered by it.",
                        },
                        diffCycle: {
                            type: "boolean",
                            description: "Compare the first measured frame against the last (default true); false leaves it without a predecessor.",
                        },
                    },
                    required: ["filePath"],
                },
            },
            {
                name: "derive_mirror",
                description: "farm02 round 2 / F.5: the two-step 'mirror + replace some rows' derivation that needed a brand-new script " +
                    "three times in one round (east->west, front->back, left hand->right hand). The whole sprite is copied to dst, " +
                    "then each selected layer x frame is mirrored in SPRITE space (axis horizontal by default), then the optional " +
                    "patch[] rects are pasted back from another sprite. Coordinates are INCLUSIVE: { x0, y0, x1, y1 } in sprite " +
                    "coordinates, so 'body rows y14..23' is written as y0=14, y1=23. A patch applies to its own layer when given, " +
                    "otherwise to every selected layer; the rect is read from fromSprite's frame (default 1) and applies to every " +
                    "selected dst frame (call once per frame when they differ). " +
                    "Cel geometry is rebuilt in sprite space: mirroring pixel (x,y) means (W-1-x,y) on the CANVAS, not inside the " +
                    "cel's own image, which is the trap a naive in-place flip falls into whenever cel.position is not symmetric " +
                    "about the sprite centre. Every rebuilt cel is re-tightened to its content bbox. " +
                    "Two self-checks run on the file that was just written, against an expectation recomputed independently from a " +
                    "pristine pre-edit snapshot: silhouette_match (opaque masks match the mirror outside the patch rects) and " +
                    "patched_only_change (pixels outside the patch rects are bit-identical to the mirror). Both report their mismatch " +
                    "count plus sample coordinates, and patched_pixels reports whether the patches wrote anything at all - a patch " +
                    "that matched no pixel is reported instead of silently passing.",
                inputSchema: {
                    type: "object",
                    properties: {
                        src: { type: "string", description: "Source sprite file (read-only)" },
                        dst: { type: "string", description: "Destination sprite file (written)" },
                        axis: {
                            type: "string",
                            enum: ["horizontal", "vertical"],
                            description: "Mirror axis (default: horizontal = left/right flip, the east<->west and front<->back case)",
                        },
                        frames: {
                            type: "array",
                            items: { type: "number" },
                            description: "1-indexed frames to mirror (default: every frame). Unknown frames are refused.",
                        },
                        layers: {
                            type: "array",
                            items: { type: "string" },
                            description: "Layer names to mirror (default: every non-group layer). Unknown names are refused.",
                        },
                        patch: {
                            type: "array",
                            description: "Rects (inclusive, sprite coords) pasted over the mirrored result, in order",
                            items: {
                                type: "object",
                                properties: {
                                    x0: { type: "number" },
                                    y0: { type: "number" },
                                    x1: { type: "number" },
                                    y1: { type: "number" },
                                    fromSprite: { type: "string", description: "Sprite file to read the rect from" },
                                    frame: { type: "number", description: "Frame of fromSprite to read (default 1)" },
                                    layer: { type: "string", description: "Only this dst layer (default: every selected layer)" },
                                },
                                required: ["x0", "y0", "x1", "y1", "fromSprite"],
                            },
                        },
                    },
                    required: ["src", "dst"],
                },
            },
            {
                name: "palette_spec_check",
                description: "live §6.3 R3: compare a spec table against an artifact's actual palette, and against that artifact's own palette " +
                    "history. spec is {\"<palette index>\": \"#rrggbb\", ...} (or specPath to such a JSON). Per entry it reports expected " +
                    "vs actual, the hue and CIE L* difference and the saturation drop (the desaturation signature), and the verdict is " +
                    "FAIL as soon as one entry does not match. When <file>.palette-history.jsonl exists it is replayed: if the spec " +
                    "matches the palette state recorded BEFORE one of the changes, the reply says so - i.e. \"the spec is not wrong, the " +
                    "board was re-coloured afterwards, here is when and by which tool\" - which is exactly the skin/hat desaturation " +
                    "mystery (spec #a06a45 vs actual #8e6f5a) that was otherwise found by eye. hueTolerance / lightnessTolerance let a " +
                    "deliberate small drift pass; without them any difference is a mismatch.",
                inputSchema: {
                    type: "object",
                    properties: {
                        file: { type: "string", description: "Artifact to check: a sprite (.aseprite/...) or a .gpl palette" },
                        spec: {
                            type: "object",
                            description: 'Palette index -> expected #rrggbb, e.g. {"2":"#a06a45","5":"#96703c"}',
                            additionalProperties: { type: "string" },
                        },
                        specPath: { type: "string", description: "JSON file holding the same mapping (or {colors:{...}})" },
                        hueTolerance: { type: "number", description: "Hue difference (deg) up to which a mismatch is tolerated" },
                        lightnessTolerance: { type: "number", description: "L* difference up to which a mismatch is tolerated" },
                        checkHistory: { type: "boolean", description: "Replay <file>.palette-history.jsonl to explain the mismatch (default true)" },
                    },
                    required: ["file"],
                },
            },
            {
                name: "family_palette_consistency",
                description: "live §6.3 R2: turn 'the portrait is blue but the animation frames are brown' into numbers. For each requested " +
                    "family it reports the palette colours at those indices on BOTH sides with their hue and CIE L*, the circular-mean " +
                    "anchor hue, the anchor hue difference (default FAIL threshold 25deg) and the mean L* difference, plus how much of " +
                    "each artifact the family actually covers (pixel share of opaque pixels - sprite inputs only; a .gpl has no pixels). " +
                    "Sides may be a .gpl palette file or a sprite/image; a sprite contributes its palette AND its pixel share. " +
                    "Comparison is ALWAYS on the palette colours, never on family names: the two sides legitimately call the same " +
                    "material different things (overalls vs denim), so a name-only check would pass a 162deg mismatch. " +
                    "Near-neutral families (mean saturation <= 0.05) are reported as hue_comparable=false instead of producing a " +
                    "meaningless hue FAIL.",
                inputSchema: {
                    type: "object",
                    properties: {
                        a: { type: "string", description: "First side: .gpl palette file, or a sprite/image (.aseprite/.png/...)" },
                        b: { type: "string", description: "Second side: .gpl palette file, or a sprite/image" },
                        families: {
                            type: "array",
                            description: "Families to compare",
                            items: {
                                type: "object",
                                properties: {
                                    name: { type: "string" },
                                    indices: { type: "array", items: { type: "number" }, description: "Palette indices of this family on BOTH sides" },
                                },
                                required: ["name", "indices"],
                            },
                        },
                        indices: { type: "array", items: { type: "number" }, description: "Convenience form: one family, indices only" },
                        probeIndex: { type: "number", description: "Convenience form: the family's representative index" },
                        family: { type: "string", description: "Convenience form: family label" },
                        hueThreshold: { type: "number", description: "Anchor hue difference that counts as FAIL (default 25 degrees)" },
                        lightnessThreshold: { type: "number", description: "Optional mean L* difference that counts as FAIL" },
                    },
                    required: ["a", "b"],
                },
            },
            {
                name: "swap_family_palette",
                description: "live §6.3 R1: batch-swap ONE material family's palette entries across many INDEXED sheets with ZERO pixel " +
                    "change (a palette swap re-colours everything while every pixel keeps its index - the mechanism ACT §621 used " +
                    "for NPC variants). Entries are set on EACH FILE'S OWN palette, never by loading one external board: the 24 v3 " +
                    "character sheets carry palettes of 20 / 26 / 28 entries, so an external board does not line up (live §6.4). " +
                    "Family = indices[] (explicit, e.g. [11,12,13] for overalls) or family+familyTable ({families:{name:[indices]}}). " +
                    "ramp[] must hold one #rrggbb per index, in index order; expectRamp[] asserts the CURRENT values first, so " +
                    "re-colouring the wrong material is refused instead of silently applied. " +
                    "Writes only where you say: outDir (sources stay untouched - the assets_anim_blue/ pattern) or inPlace=true. " +
                    "Preconditions are checked for EVERY file before anything is written (all-or-nothing), and afterwards each " +
                    "output is re-opened from disk and verified: index fingerprint identical, palette diff exactly == the family " +
                    "indices, palette size unchanged, and decoded-RGB changes confined to pixels whose index is in the family " +
                    "(rgb_changed_outside_family must be 0). Non-INDEXED files and palettes too short for the indices are refused. " +
                    "Every changed entry is appended to <out>.palette-history.jsonl (R3 trace).",
                inputSchema: {
                    type: "object",
                    properties: {
                        files: { type: "array", items: { type: "string" }, description: "INDEXED sheets to convert (source files stay as they are)" },
                        indices: { type: "array", items: { type: "number" }, description: "Family palette indices, e.g. [11,12,13]" },
                        family: { type: "string", description: "Family name, resolved through familyTable" },
                        familyTable: { type: "string", description: "JSON file: {families:{name:[indices]}} (A6 writes this)" },
                        ramp: { type: "array", items: { type: "string" }, description: "New colours, one #rrggbb per index, in index order" },
                        expectRamp: { type: "array", items: { type: "string" }, description: "Assert these CURRENT values first (refuses if they differ)" },
                        outDir: { type: "string", description: "Write <outDir>/<basename>; source files are never written" },
                        inPlace: { type: "boolean", description: "Overwrite the sources (only if you really mean it)" },
                        recordHistory: { type: "boolean", description: "Append the change trace (default true)" },
                    },
                    required: ["files", "ramp"],
                },
            },
            {
                name: "export_meter_module",
                description: "farm02 round 3 / C1: write the MCP meters' own Lua core to a .lua file, so an in-artifact script " +
                    "(tools/_action4.lua, build-action-*.lua) calls ONE implementation instead of re-copying the meter " +
                    "(the farm02 project had reached its 7th hand-written copy). The file is generated from the SAME source " +
                    "string the built-in tools embed, so the two can never drift. Load it with " +
                    "`local M = dofile(\"<path>\")` and use M.px / M.scan_cel / M.scan_frame / M.scan_all / M.alpha_fp / " +
                    "M.composite / M.bbox / M.round / M.lin / M.lstar / M.hsv / M.hex / M.pct. " +
                    "M.px(spr, pixelValue, palette) is the mode-aware decoder (INDEXED: index ~= transparentColor), which is " +
                    "the single rule whose absence made every meter read 'transparent' on indexed sheets. " +
                    "For a one-off snippet, run_script's withMeter=true injects the same helpers inline (no file needed).",
                inputSchema: {
                    type: "object",
                    properties: {
                        outputPath: { type: "string", description: "Where to write the .lua module (parent dirs are created)" },
                    },
                    required: ["outputPath"],
                },
            },
            {
                name: "remap_colors",
                description: "Table-driven color remap (replace_color only handles one color, whole image, and rewrites alpha). " +
                    "map is {\"#rrggbb\":\"#rrggbb\", ...}; matching is on the decoded color and each pixel's ALPHA IS PRESERVED, so the " +
                    "silhouette cannot change - use it as the zero-shape-risk way to re-color. Optional region and layer filter. " +
                    "WORKS ON INDEXED SHEETS (2026-10-16 fix): an INDEXED cel stores palette indices, so the target color is resolved " +
                    "to a palette INDEX and written as that index; if a map target is NOT in the palette it is reported in unmappable " +
                    "with unmappable_pixels and self_report instead of being skipped silently (the old code returned changed=0 and let " +
                    "verifyAlpha certify 'nothing changed'). perKey[].target_index is the resolved index. " +
                    "Reports per-key hit counts, and when verifyAlpha (default true) re-opens the SAVED file and compares an alpha " +
                    "fingerprint (opaque count + alpha sum + position-weighted sum) against the original, so alpha_unchanged is measured " +
                    "on disk rather than assumed. changed counts EDITED pixels - a pixel hidden under an opaque layer still counts; for the " +
                    "VISIBLE change use diff_sprites (on a layered asset the two numbers legitimately differ).",
                inputSchema: {
                    type: "object",
                    properties: {
                        filePath: { type: "string", description: "Source sprite file" },
                        map: {
                            type: "object",
                            description: 'Color table, e.g. {"#3b4a5a":"#546474"} - keys/values are #rrggbb (case-insensitive)',
                            additionalProperties: { type: "string" },
                        },
                        region: {
                            type: "object",
                            description: "Only remap inside {x,y,w,h}",
                            properties: {
                                x: { type: "number" },
                                y: { type: "number" },
                                w: { type: "number" },
                                h: { type: "number" },
                            },
                            required: ["x", "y", "w", "h"],
                        },
                        layers: {
                            type: "array",
                            items: { type: "string" },
                            description: "Only these layer names (default: all non-group layers)",
                        },
                        frameNumber: { type: "number", description: "Only this frame (default: all frames)" },
                        savePath: { type: "string", description: "Path to save (defaults to overwrite source)" },
                        verifyAlpha: { type: "boolean", description: "Re-open the saved file to measure alpha (default true)" },
                    },
                    required: ["filePath", "map"],
                },
            },
            {
                name: "diff_sprites",
                description: "Pixel diff of two sprites with region decomposition - the atomic acceptance gate for 'only what should have changed, " +
                    "changed'. Compares the COMPOSITE of one frame of each file (stats basis reported). " +
                    "A pixel counts as changed when its coverage differs (opaque vs transparent on either side) or, when both sides are " +
                    "opaque, when RGB differs (plus alpha when includeAlpha, default true). RGB under fully transparent pixels is ignored, " +
                    "so stray color in discarded areas does not fake a diff. Also reports the changed bbox, the alpha-changed count, and can " +
                    "write a mask PNG (red = changed) for visual review.",
                inputSchema: {
                    type: "object",
                    properties: {
                        a: { type: "string", description: "First sprite file (base)" },
                        b: { type: "string", description: "Second sprite file (candidate)" },
                        region: {
                            type: "object",
                            description: "Split the diff into inside/outside this {x,y,w,h}",
                            properties: {
                                x: { type: "number" },
                                y: { type: "number" },
                                w: { type: "number" },
                                h: { type: "number" },
                            },
                            required: ["x", "y", "w", "h"],
                        },
                        includeAlpha: { type: "boolean", description: "Count alpha-only differences as changed (default true)" },
                        maskPng: { type: "string", description: "Optional path for a diff mask PNG (red = changed)" },
                        frameNumber: { type: "number", description: "Frame to compare (default 1)" },
                    },
                    required: ["a", "b"],
                },
            },
        ];
    }
    // ---- Native tool helpers ------------------------------------------------
    // Generate Lua to set up app.useTool() context (active sprite + layer + frame)
    useToolSetup(layerName, frameNumber) {
        const layerPart = layerName
            ? `${this.findLayerLua("activeLayer", layerName)}
app.activeLayer = activeLayer`
            : `app.activeLayer = spr.layers[1]`;
        return `
app.activeSprite = spr
${layerPart}
app.activeFrame = spr.frames[${frameNumber}]
`;
    }
    // ---- Tool handler implementations --------------------------------------
    // -- Sprite Management --
    async handleCreateSprite(args) {
        const width = this.numParam(args, "width");
        const height = this.numParam(args, "height");
        const colorMode = this.optParam(args, "colorMode") ?? "rgb";
        const savePath = this.requireParam(args, "savePath");
        const modeMap = { rgb: "ColorMode.RGB", grayscale: "ColorMode.GRAYSCALE", indexed: "ColorMode.INDEXED" };
        const luaMode = modeMap[colorMode] ?? "ColorMode.RGB";
        const script = `
local spr = Sprite(${width}, ${height}, ${luaMode})
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({
  success = true,
  data = {
    width = spr.width,
    height = spr.height,
    colorMode = "${colorMode}",
    path = "${luaPath(savePath)}"
  }
}))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to create sprite");
        return this.ok(result.data ?? result);
    }
    async handleOpenSprite(args) {
        const filePath = this.requireParam(args, "filePath");
        return this.handleGetSpriteInfo(args);
    }
    async handleSaveSprite(args) {
        const filePath = this.requireParam(args, "filePath");
        const savePath = this.requireParam(args, "savePath");
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to save sprite");
        return this.ok(result.data ?? result);
    }
    async handleGetSpriteInfo(args) {
        const filePath = this.requireParam(args, "filePath");
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end

local function collectLayers(layers, depth)
  local result = {}
  for i, layer in ipairs(layers) do
    local info = {
      name = layer.name,
      isGroup = layer.isGroup,
      isVisible = layer.isVisible,
      isEditable = layer.isEditable,
      opacity = layer.opacity,
      blendMode = tostring(layer.blendMode),
      depth = depth
    }
    if layer.isGroup and layer.layers then
      info.children = collectLayers(layer.layers, depth + 1)
    end
    table.insert(result, info)
  end
  return result
end

local frames = {}
for i, frame in ipairs(spr.frames) do
  table.insert(frames, { frameNumber = frame.frameNumber, duration = math.floor(frame.duration * 1000) })
end

local tags = {}
for i, tag in ipairs(spr.tags) do
  table.insert(tags, {
    name = tag.name,
    fromFrame = tag.fromFrame.frameNumber,
    toFrame = tag.toFrame.frameNumber,
    aniDir = tostring(tag.aniDir),
    repeats = tag.repeats
  })
end

local slices = {}
for i, slice in ipairs(spr.slices) do
  table.insert(slices, {
    name = slice.name,
    bounds = { x = slice.bounds.x, y = slice.bounds.y, width = slice.bounds.width, height = slice.bounds.height }
  })
end

local pal = spr.palettes[1]
local palSize = #pal

io.write("__RESULT__" .. json.encode({
  success = true,
  data = {
    filename = spr.filename,
    width = spr.width,
    height = spr.height,
    colorMode = tostring(spr.colorMode),
    -- C3 (2026-10-16): 明文色模式。tostring(colorMode) 是 "2"，而"是不是 INDEXED"直接决定
    -- 所有量尺可不可信、以及改色该写索引还是写 RGB —— 不该由调用方去记 2=INDEXED。
    colorModeName = ({ "rgb", "grayscale", "indexed" })[spr.colorMode + 1] or tostring(spr.colorMode),
    indexed = (spr.colorMode == ColorMode.INDEXED),
    frameCount = #spr.frames,
    layerCount = #spr.layers,
    layers = collectLayers(spr.layers, 0),
    frames = frames,
    tags = tags,
    slices = slices,
    paletteSize = palSize
  }
}))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to get sprite info");
        return this.ok(result.data ?? result);
    }
    async handleResizeSprite(args) {
        const filePath = this.requireParam(args, "filePath");
        const width = this.numParam(args, "width");
        const height = this.numParam(args, "height");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
spr:resize(${width}, ${height})
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { width = spr.width, height = spr.height, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to resize sprite");
        return this.ok(result.data ?? result);
    }
    async handleCropSprite(args) {
        const filePath = this.requireParam(args, "filePath");
        const x = this.numParam(args, "x");
        const y = this.numParam(args, "y");
        const width = this.numParam(args, "width");
        const height = this.numParam(args, "height");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
spr:crop(${x}, ${y}, ${width}, ${height})
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { width = spr.width, height = spr.height, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to crop sprite");
        return this.ok(result.data ?? result);
    }
    // -- Layer Management --
    async handleAddLayer(args) {
        const filePath = this.requireParam(args, "filePath");
        const layerName = this.requireParam(args, "layerName");
        const isGroup = this.boolParam(args, "isGroup", false);
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const createFn = isGroup ? "spr:newGroup()" : "spr:newLayer()";
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local layer = ${createFn}
layer.name = "${luaEscape(layerName)}"
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { layerName = layer.name, isGroup = layer.isGroup, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to add layer");
        return this.ok(result.data ?? result);
    }
    findLayerLua(varName, layerName) {
        return `
local function findLayer(layers, name)
  for i, layer in ipairs(layers) do
    if layer.name == name then return layer end
    if layer.isGroup and layer.layers then
      local found = findLayer(layer.layers, name)
      if found then return found end
    end
  end
  return nil
end
local ${varName} = findLayer(spr.layers, "${luaEscape(layerName)}")
if not ${varName} then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Layer not found: ${luaEscape(layerName)}" }))
  return
end
`;
    }
    async handleRemoveLayer(args) {
        const filePath = this.requireParam(args, "filePath");
        const layerName = this.requireParam(args, "layerName");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
${this.findLayerLua("target", layerName)}
spr:deleteLayer(target)
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { removed = "${luaEscape(layerName)}", savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to remove layer");
        return this.ok(result.data ?? result);
    }
    async handleListLayers(args) {
        return this.handleGetSpriteInfo(args);
    }
    async handleSetLayerProperties(args) {
        const filePath = this.requireParam(args, "filePath");
        const layerName = this.requireParam(args, "layerName");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const newName = this.optParam(args, "newName");
        const visible = this.boolParam(args, "visible");
        const opacity = this.numParam(args, "opacity");
        const blendMode = this.optParam(args, "blendMode");
        const blendModeMap = {
            normal: "BlendMode.NORMAL", multiply: "BlendMode.MULTIPLY", screen: "BlendMode.SCREEN",
            overlay: "BlendMode.OVERLAY", darken: "BlendMode.DARKEN", lighten: "BlendMode.LIGHTEN",
            color_dodge: "BlendMode.COLOR_DODGE", color_burn: "BlendMode.COLOR_BURN",
            hard_light: "BlendMode.HARD_LIGHT", soft_light: "BlendMode.SOFT_LIGHT",
            difference: "BlendMode.DIFFERENCE", exclusion: "BlendMode.EXCLUSION",
            hue: "BlendMode.HSL_HUE", saturation: "BlendMode.HSL_SATURATION",
            color: "BlendMode.HSL_COLOR", luminosity: "BlendMode.HSL_LUMINOSITY",
        };
        let setParts = "";
        if (newName !== undefined)
            setParts += `  target.name = "${luaEscape(newName)}"\n`;
        if (visible !== undefined)
            setParts += `  target.isVisible = ${visible}\n`;
        if (opacity !== undefined)
            setParts += `  target.opacity = ${opacity}\n`;
        if (blendMode !== undefined) {
            const luaBm = blendModeMap[blendMode] ?? "BlendMode.NORMAL";
            setParts += `  target.blendMode = ${luaBm}\n`;
        }
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
${this.findLayerLua("target", layerName)}
app.transaction(function()
${setParts}end)
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = {
  name = target.name,
  isVisible = target.isVisible,
  opacity = target.opacity,
  blendMode = tostring(target.blendMode),
  savedTo = "${luaPath(savePath)}"
} }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to set layer properties");
        return this.ok(result.data ?? result);
    }
    // -- Frame & Animation --
    async handleAddFrame(args) {
        const filePath = this.requireParam(args, "filePath");
        const count = this.numParam(args, "count", 1);
        const afterFrame = this.numParam(args, "afterFrame");
        const empty = this.boolParam(args, "empty", false);
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const fn = empty ? "spr:newEmptyFrame" : "spr:newFrame";
        const insertAt = afterFrame !== undefined ? `${afterFrame} + 1` : "#spr.frames + 1";
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
for i = 1, ${count} do
  ${fn}(${insertAt})
end
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { totalFrames = #spr.frames, added = ${count}, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to add frame");
        return this.ok(result.data ?? result);
    }
    async handleRemoveFrame(args) {
        const filePath = this.requireParam(args, "filePath");
        const frameNumber = this.numParam(args, "frameNumber");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
if ${frameNumber} < 1 or ${frameNumber} > #spr.frames then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Frame number out of range" }))
  return
end
spr:deleteFrame(${frameNumber})
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { totalFrames = #spr.frames, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to remove frame");
        return this.ok(result.data ?? result);
    }
    async handleSetFrameDuration(args) {
        const filePath = this.requireParam(args, "filePath");
        const frameNumber = this.numParam(args, "frameNumber");
        const durationMs = this.numParam(args, "durationMs");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
if ${frameNumber} < 1 or ${frameNumber} > #spr.frames then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Frame number out of range" }))
  return
end
spr.frames[${frameNumber}].duration = ${durationMs} / 1000.0
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { frameNumber = ${frameNumber}, durationMs = ${durationMs}, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to set frame duration");
        return this.ok(result.data ?? result);
    }
    async handleListFrames(args) {
        const filePath = this.requireParam(args, "filePath");
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local frames = {}
for i, frame in ipairs(spr.frames) do
  table.insert(frames, { frameNumber = frame.frameNumber, durationMs = math.floor(frame.duration * 1000) })
end
io.write("__RESULT__" .. json.encode({ success = true, data = { totalFrames = #spr.frames, frames = frames } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to list frames");
        return this.ok(result.data ?? result);
    }
    // -- Animation Tags --
    async handleCreateTag(args) {
        const filePath = this.requireParam(args, "filePath");
        const tagName = this.requireParam(args, "tagName");
        const fromFrame = this.numParam(args, "fromFrame");
        const toFrame = this.numParam(args, "toFrame");
        const aniDir = this.optParam(args, "aniDir") ?? "forward";
        const color = this.optParam(args, "color");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const dirMap = {
            forward: "AniDir.FORWARD",
            reverse: "AniDir.REVERSE",
            pingpong: "AniDir.PING_PONG",
            pingpong_reverse: "AniDir.PING_PONG_REVERSE",
        };
        const luaDir = dirMap[aniDir] ?? "AniDir.FORWARD";
        let colorLine = "";
        if (color)
            colorLine = `tag.color = ${this.luaColor(color)}`;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local tag = spr:newTag(${fromFrame}, ${toFrame})
tag.name = "${luaEscape(tagName)}"
tag.aniDir = ${luaDir}
${colorLine}
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = {
  name = tag.name,
  fromFrame = tag.fromFrame.frameNumber,
  toFrame = tag.toFrame.frameNumber,
  aniDir = tostring(tag.aniDir),
  savedTo = "${luaPath(savePath)}"
} }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to create tag");
        return this.ok(result.data ?? result);
    }
    async handleRemoveTag(args) {
        const filePath = this.requireParam(args, "filePath");
        const tagName = this.requireParam(args, "tagName");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local found = nil
for i, tag in ipairs(spr.tags) do
  if tag.name == "${luaEscape(tagName)}" then found = tag; break end
end
if not found then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Tag not found: ${luaEscape(tagName)}" }))
  return
end
spr:deleteTag(found)
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { removed = "${luaEscape(tagName)}", savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to remove tag");
        return this.ok(result.data ?? result);
    }
    async handleListTags(args) {
        const filePath = this.requireParam(args, "filePath");
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local tags = {}
for i, tag in ipairs(spr.tags) do
  table.insert(tags, {
    name = tag.name,
    fromFrame = tag.fromFrame.frameNumber,
    toFrame = tag.toFrame.frameNumber,
    aniDir = tostring(tag.aniDir),
    repeats = tag.repeats
  })
end
io.write("__RESULT__" .. json.encode({ success = true, data = { totalTags = #spr.tags, tags = tags } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to list tags");
        return this.ok(result.data ?? result);
    }
    // -- Drawing & Pixel Operations --
    getCelScript(layerName, frameNumber) {
        if (!layerName) {
            return `
local layer = spr.layers[1]
local frameNum = ${frameNumber}
`;
        }
        return `
${this.findLayerLua("layer", layerName)}
local frameNum = ${frameNumber}
`;
    }
    getOrCreateCelScript() {
        return `
local cel = layer:cel(frameNum)
if not cel then
  cel = spr:newCel(layer, frameNum)
end
local img = cel.image:clone()
local pos = cel.position
`;
    }
    saveCelScript(savePath) {
        return `
cel.image = img
spr:saveCopyAs("${luaPath(savePath)}")
`;
    }
    async handleDrawPixels(args) {
        const filePath = this.requireParam(args, "filePath");
        const layerName = this.optParam(args, "layerName");
        const frameNumber = this.numParam(args, "frameNumber", 1);
        const pixels = args.pixels;
        const savePath = this.optParam(args, "savePath") ?? filePath;
        if (!pixels || !Array.isArray(pixels) || pixels.length === 0) {
            return this.error("pixels array is required and must not be empty");
        }
        const pixelLines = pixels
            .map((p) => `  img:drawPixel(${p.x} - pos.x, ${p.y} - pos.y, ${this.luaColor(p.color)})`)
            .join("\n");
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
${this.getCelScript(layerName, frameNumber)}
${this.getOrCreateCelScript()}
app.transaction(function()
${pixelLines}
end)
${this.saveCelScript(savePath)}
io.write("__RESULT__" .. json.encode({ success = true, data = { pixelsDrawn = ${pixels.length}, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to draw pixels");
        return this.ok(result.data ?? result);
    }
    async handleDrawRect(args) {
        const filePath = this.requireParam(args, "filePath");
        const layerName = this.optParam(args, "layerName");
        const frameNumber = this.numParam(args, "frameNumber", 1);
        const x = this.numParam(args, "x");
        const y = this.numParam(args, "y");
        const width = this.numParam(args, "width");
        const height = this.numParam(args, "height");
        const color = args.color;
        const filled = this.boolParam(args, "filled", true);
        const thickness = this.numParam(args, "thickness", 1);
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const luaCol = this.luaColor(color);
        // Use app.useTool() for thick strokes, manual pixels for thickness=1
        let drawScript;
        if (thickness > 1 && !filled) {
            drawScript = `
${this.useToolSetup(layerName, frameNumber)}
app.fgColor = ${luaCol}
app.useTool{
  tool = "rectangle",
  color = ${luaCol},
  brush = Brush{ size = ${thickness} },
  points = { Point(${x}, ${y}), Point(${x + width - 1}, ${y + height - 1}) }
}
spr:saveCopyAs("${luaPath(savePath)}")
`;
        }
        else {
            drawScript = `
${this.getCelScript(layerName, frameNumber)}
${this.getOrCreateCelScript()}
local c = ${luaCol}
app.transaction(function()
${filled
                ? `  for dy = 0, ${height - 1} do
    for dx = 0, ${width - 1} do
      img:drawPixel(${x} + dx - pos.x, ${y} + dy - pos.y, c)
    end
  end`
                : `  for dx = 0, ${width - 1} do
    img:drawPixel(${x} + dx - pos.x, ${y} - pos.y, c)
    img:drawPixel(${x} + dx - pos.x, ${y} + ${height - 1} - pos.y, c)
  end
  for dy = 0, ${height - 1} do
    img:drawPixel(${x} - pos.x, ${y} + dy - pos.y, c)
    img:drawPixel(${x} + ${width - 1} - pos.x, ${y} + dy - pos.y, c)
  end`}
end)
${this.saveCelScript(savePath)}
`;
        }
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
${drawScript}
io.write("__RESULT__" .. json.encode({ success = true, data = { rect = {x=${x}, y=${y}, width=${width}, height=${height}}, filled = ${filled}, thickness = ${thickness}, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to draw rectangle");
        return this.ok(result.data ?? result);
    }
    async handleDrawCircle(args) {
        const filePath = this.requireParam(args, "filePath");
        const layerName = this.optParam(args, "layerName");
        const frameNumber = this.numParam(args, "frameNumber", 1);
        const cx = this.numParam(args, "centerX");
        const cy = this.numParam(args, "centerY");
        const radius = this.numParam(args, "radius");
        const color = args.color;
        const filled = this.boolParam(args, "filled", true);
        const thickness = this.numParam(args, "thickness", 1);
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const luaCol = this.luaColor(color);
        // Use app.useTool() for thick strokes
        let drawScript;
        if (thickness > 1 && !filled) {
            const x1 = cx - radius;
            const y1 = cy - radius;
            const x2 = cx + radius;
            const y2 = cy + radius;
            drawScript = `
${this.useToolSetup(layerName, frameNumber)}
app.fgColor = ${luaCol}
app.useTool{
  tool = "ellipse",
  color = ${luaCol},
  brush = Brush{ size = ${thickness} },
  points = { Point(${x1}, ${y1}), Point(${x2}, ${y2}) }
}
spr:saveCopyAs("${luaPath(savePath)}")
`;
        }
        else {
            drawScript = `
${this.getCelScript(layerName, frameNumber)}
${this.getOrCreateCelScript()}
local c = ${luaCol}
local cx, cy, r = ${cx}, ${cy}, ${radius}
local filled = ${filled}

local function drawPixelSafe(ix, iy)
  local lx = ix - pos.x
  local ly = iy - pos.y
  if lx >= 0 and lx < img.width and ly >= 0 and ly < img.height then
    img:drawPixel(lx, ly, c)
  end
end

app.transaction(function()
  if filled then
    for dy = -r, r do
      for dx = -r, r do
        if dx*dx + dy*dy <= r*r then
          drawPixelSafe(cx + dx, cy + dy)
        end
      end
    end
  else
    local x = r
    local y = 0
    local d = 1 - r
    while x >= y do
      drawPixelSafe(cx + x, cy + y)
      drawPixelSafe(cx - x, cy + y)
      drawPixelSafe(cx + x, cy - y)
      drawPixelSafe(cx - x, cy - y)
      drawPixelSafe(cx + y, cy + x)
      drawPixelSafe(cx - y, cy + x)
      drawPixelSafe(cx + y, cy - x)
      drawPixelSafe(cx - y, cy - x)
      y = y + 1
      if d < 0 then
        d = d + 2 * y + 1
      else
        x = x - 1
        d = d + 2 * (y - x) + 1
      end
    end
  end
end)
${this.saveCelScript(savePath)}
`;
        }
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
${drawScript}
io.write("__RESULT__" .. json.encode({ success = true, data = { center = {x=${cx}, y=${cy}}, radius = ${radius}, filled = ${filled}, thickness = ${thickness}, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to draw circle");
        return this.ok(result.data ?? result);
    }
    async handleDrawLine(args) {
        const filePath = this.requireParam(args, "filePath");
        const layerName = this.optParam(args, "layerName");
        const frameNumber = this.numParam(args, "frameNumber", 1);
        const x1 = this.numParam(args, "x1");
        const y1 = this.numParam(args, "y1");
        const x2 = this.numParam(args, "x2");
        const y2 = this.numParam(args, "y2");
        const color = args.color;
        const thickness = this.numParam(args, "thickness", 1);
        const savePath = this.optParam(args, "savePath") ?? filePath;
        // farm02 round 2 / F.2: a 1px Bresenham diagonal step is 8-connected (corner pixels missing)
        // while the silhouette gate counts 4-neighbours, so one thin diagonal was reported as 2-4
        // connected components (n=16 blade + stray n=1/n=2 handle pixels). connect4 inserts the L
        // corner between two diagonal steps - the same fix the project already uses in
        // tools/build-action-hoe.lua:linePoints4. It ADDS corners; it never thickens the line, so
        // `thickness=1` stays the only thin-line primitive.
        //
        // OPT-IN (default false), deliberately: turning it on by default changes the pixels an
        // existing caller gets, and this repo's own selftest-verify.mjs asserts "exactly one pixel
        // per x column" for a Bresenham diagonal. Instead the result always reports components4 and a
        // `warning` when a 1px line came out 8-connected, so the trap is visible without being
        // silently applied.
        const connect4 = this.boolParam(args, "connect4", false) === true;
        const luaCol = this.luaColor(color);
        // Use app.useTool() for thick lines, Bresenham for thickness=1
        let drawScript;
        if (thickness > 1) {
            drawScript = `
${this.useToolSetup(layerName, frameNumber)}
app.fgColor = ${luaCol}
app.useTool{
  tool = "line",
  color = ${luaCol},
  brush = Brush{ size = ${thickness} },
  points = { Point(${x1}, ${y1}), Point(${x2}, ${y2}) }
}
spr:saveCopyAs("${luaPath(savePath)}")
`;
        }
        else {
            drawScript = `
${this.getCelScript(layerName, frameNumber)}
${this.getOrCreateCelScript()}
local c = ${luaCol}
local CONNECT4 = ${connect4 ? "true" : "false"}
local corners = 0
local pts = {}

app.transaction(function()
  local x0, y0 = ${x1}, ${y1}
  local tx, ty = ${x2}, ${y2}
  local dx = math.abs(tx - x0)
  local dy = -math.abs(ty - y0)
  local sx = x0 < tx and 1 or -1
  local sy = y0 < ty and 1 or -1
  local err = dx + dy
  local prevX, prevY = nil, nil
  local function plot(ax, ay)
    local lx = ax - pos.x
    local ly = ay - pos.y
    if lx >= 0 and lx < img.width and ly >= 0 and ly < img.height then
      img:drawPixel(lx, ly, c)
    end
    pts[#pts + 1] = { ax, ay }
  end
  while true do
    -- Diagonal step (both axes move): insert the L corner that shares the NEW x with the OLD y.
    if CONNECT4 and prevX and prevX ~= x0 and prevY ~= y0 then
      plot(x0, prevY)
      corners = corners + 1
    end
    plot(x0, y0)
    prevX, prevY = x0, y0
    if x0 == tx and y0 == ty then break end
    local e2 = 2 * err
    if e2 >= dy then err = err + dy; x0 = x0 + sx end
    if e2 <= dx then err = err + dx; y0 = y0 + sy end
  end
end)
-- 自证：把"4 连通"从宣称变成量出的数。沿像素序列走一遍，凡相邻两像素不是 4 邻接
-- （|dx|+|dy| ~= 1）就断开 ⇒ components4 就是这条线自己的连通块数（1 = 真的 4 连通）。
local components4 = 0
for i = 1, #pts do
  if i == 1 then
    components4 = 1
  else
    local ddx = math.abs(pts[i][1] - pts[i - 1][1])
    local ddy = math.abs(pts[i][2] - pts[i - 1][2])
    if ddx + ddy ~= 1 then components4 = components4 + 1 end
  end
end
-- 8 连通不是错误，但它是"4 邻连通闸门会误判"的那一类输入 ⇒ 明说，别让调用方自己撞。
local warning = nil
if not CONNECT4 and components4 > 1 then
  warning = "this 1px line is 8-connected (" .. components4 .. " four-neighbour fragments); pass connect4=true when a silhouette gate counts 4-neighbours"
end
${this.saveCelScript(savePath)}
`;
        }
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
${drawScript}
io.write("__RESULT__" .. json.encode({ success = true, data = { from = {x=${x1}, y=${y1}}, to = {x=${x2}, y=${y2}}, thickness = ${thickness}, connect4_requested = ${connect4}, ${thickness > 1 ? `connect4_applied = false, connect4_note = "thickness>1 uses the native brush; the 4-connectivity rule applies to 1px lines only", cornerPixelsAdded = 0, pointsPlotted = 0, components4 = 0, warning = nil,` : `connect4_applied = CONNECT4, cornerPixelsAdded = corners, pointsPlotted = #pts, components4 = components4, warning = warning,`} savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to draw line");
        return this.ok(result.data ?? result);
    }
    async handleDrawEllipse(args) {
        const filePath = this.requireParam(args, "filePath");
        const layerName = this.optParam(args, "layerName");
        const frameNumber = this.numParam(args, "frameNumber", 1);
        const x = this.numParam(args, "x");
        const y = this.numParam(args, "y");
        const width = this.numParam(args, "width");
        const height = this.numParam(args, "height");
        const color = args.color;
        const filled = this.boolParam(args, "filled", true);
        const thickness = this.numParam(args, "thickness", 1);
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const luaCol = this.luaColor(color);
        const toolName = filled ? "filled_ellipse" : "ellipse";
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
${this.useToolSetup(layerName, frameNumber)}
app.fgColor = ${luaCol}
app.useTool{
  tool = "${toolName}",
  color = ${luaCol},
  brush = Brush{ size = ${thickness} },
  points = { Point(${x}, ${y}), Point(${x + width - 1}, ${y + height - 1}) }
}
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { bounds = {x=${x}, y=${y}, width=${width}, height=${height}}, filled = ${filled}, thickness = ${thickness}, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to draw ellipse");
        return this.ok(result.data ?? result);
    }
    async handleFillArea(args) {
        const filePath = this.requireParam(args, "filePath");
        const layerName = this.optParam(args, "layerName");
        const frameNumber = this.numParam(args, "frameNumber", 1);
        const x = this.numParam(args, "x");
        const y = this.numParam(args, "y");
        const color = args.color;
        const tolerance = this.numParam(args, "tolerance", 0);
        const contiguous = this.boolParam(args, "contiguous", true);
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const luaCol = this.luaColor(color);
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
${this.useToolSetup(layerName, frameNumber)}
app.fgColor = ${luaCol}
app.preferences.tool("paint_bucket").tolerance = ${tolerance}
app.preferences.tool("paint_bucket").contiguous = ${contiguous}
app.useTool{
  tool = "paint_bucket",
  color = ${luaCol},
  point = Point(${x}, ${y})
}
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { point = {x=${x}, y=${y}}, tolerance = ${tolerance}, contiguous = ${contiguous}, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to fill area");
        return this.ok(result.data ?? result);
    }
    async handleReplaceColor(args) {
        const filePath = this.requireParam(args, "filePath");
        const fromColor = args.from_color ?? args.fromColor;
        const toColor = args.to_color ?? args.toColor;
        const layerName = this.optParam(args, "layerName");
        const frameNumber = this.numParam(args, "frameNumber");
        const tolerance = this.numParam(args, "tolerance", 0);
        const savePath = this.optParam(args, "savePath") ?? filePath;
        if (!fromColor || !toColor) {
            return this.error("Both fromColor and toColor are required");
        }
        const luaFrom = this.luaColor(fromColor);
        const luaTo = this.luaColor(toColor);
        const layerFilter = layerName
            ? `${this.findLayerLua("targetLayer", layerName)}
local layersToProcess = { targetLayer }`
            : `local layersToProcess = spr.layers`;
        const frameFilter = frameNumber !== undefined
            ? `local framesToProcess = { spr.frames[${frameNumber}] }`
            : `local framesToProcess = spr.frames`;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
${layerFilter}
${frameFilter}

local fromCol = ${luaFrom}
local toCol = ${luaTo}
local tolerance = ${tolerance}
local replaced = 0

local function colorMatch(a, b, tol)
  if tol == 0 then
    return a.red == b.red and a.green == b.green and a.blue == b.blue and a.alpha == b.alpha
  end
  return math.abs(a.red - b.red) <= tol and
         math.abs(a.green - b.green) <= tol and
         math.abs(a.blue - b.blue) <= tol and
         math.abs(a.alpha - b.alpha) <= tol
end

app.transaction(function()
  for _, layer in ipairs(layersToProcess) do
    if not layer.isGroup then
      for _, frame in ipairs(framesToProcess) do
        local cel = layer:cel(frame.frameNumber)
        if cel then
          local img = cel.image:clone()
          for y = 0, img.height - 1 do
            for x = 0, img.width - 1 do
              local px = img:getPixel(x, y)
              local pxColor = Color(px)
              if colorMatch(pxColor, fromCol, tolerance) then
                img:drawPixel(x, y, toCol)
                replaced = replaced + 1
              end
            end
          end
          cel.image = img
        end
      end
    end
  end
end)
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { pixelsReplaced = replaced, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to replace color");
        return this.ok(result.data ?? result);
    }
    async handleFlipSprite(args) {
        const filePath = this.requireParam(args, "filePath");
        const direction = this.requireParam(args, "direction");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const orientation = direction === "horizontal" ? "FlipHorizontal" : "FlipVertical";
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
app.activeSprite = spr
app.command.Flip{ target="canvas", orientation="${orientation}" }
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { direction = "${luaEscape(direction)}", savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to flip sprite");
        return this.ok(result.data ?? result);
    }
    async handleRotateSprite(args) {
        const filePath = this.requireParam(args, "filePath");
        const angle = this.numParam(args, "angle");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        if (![90, 180, 270].includes(angle)) {
            return this.error("Angle must be 90, 180, or 270 degrees");
        }
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
app.activeSprite = spr
app.command.Rotate{ angle=${angle}, ui=false }
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { angle = ${angle}, width = spr.width, height = spr.height, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to rotate sprite");
        return this.ok(result.data ?? result);
    }
    async handleFlattenLayers(args) {
        const filePath = this.requireParam(args, "filePath");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
spr:flatten()
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { layerCount = #spr.layers, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to flatten layers");
        return this.ok(result.data ?? result);
    }
    async handleMergeDown(args) {
        const filePath = this.requireParam(args, "filePath");
        const layerName = this.requireParam(args, "layerName");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
${this.findLayerLua("target", layerName)}
app.activeSprite = spr
app.activeLayer = target
app.command.MergeDownLayer()
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { merged = "${luaEscape(layerName)}", layerCount = #spr.layers, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to merge layer down");
        return this.ok(result.data ?? result);
    }
    async handleOutline(args) {
        const filePath = this.requireParam(args, "filePath");
        const layerName = this.optParam(args, "layerName");
        const frameNumber = this.numParam(args, "frameNumber", 1);
        const color = args.color;
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const luaCol = this.luaColor(color);
        const ref = this.luaColorRef(color);
        // farm02 round 2 / F.1: automated outlining wrote 2x2 all-black blocks (a "thicker outline")
        // twice in one night, where the new outline pixel landed directly below / beside an outline
        // pixel the art already had. noStack skips exactly those candidates, judged against the
        // snapshot taken BEFORE this run writes anything (so write order cannot change the result).
        const noStack = this.boolParam(args, "noStack", true) === true;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
${this.getCelScript(layerName, frameNumber)}
${this.getOrCreateCelScript()}
local outlineColor = ${luaCol}
local indexed = (spr.colorMode == ColorMode.INDEXED)
local pal = spr.palettes[1]

-- Transparency differs by color mode: an INDEXED cel stores palette indices, where the RGBA
-- accessors read the index as a packed value and lie. Only the transparent index is transparent.
local function isTransparent(v)
  if indexed then return v == spr.transparentColor end
  return app.pixelColor.rgbaA(v) == 0
end

-- F.1 snapshot identity: which pixels ALREADY carry the outline color.
local MATCH = "${ref.kind}"
local matchIndex = ${ref.index}
local matchR, matchG, matchB = ${ref.r}, ${ref.g}, ${ref.b}
local identifiable = false
if MATCH == "index" then
  if indexed then identifiable = true end
elseif matchR >= 0 then
  if indexed then
    for i = 0, #pal - 1 do
      local c = pal:getColor(i)
      if c.red == matchR and c.green == matchG and c.blue == matchB then
        matchIndex = i
        identifiable = true
        break
      end
    end
  else
    identifiable = true
  end
end

local function isOutline(x, y)
  if not identifiable then return false end
  if x < 0 or y < 0 or x >= img.width or y >= img.height then return false end
  local v = img:getPixel(x, y)
  if indexed then return v == matchIndex end
  if app.pixelColor.rgbaA(v) == 0 then return false end
  return app.pixelColor.rgbaR(v) == matchR
     and app.pixelColor.rgbaG(v) == matchG
     and app.pixelColor.rgbaB(v) == matchB
end

-- 自证（F.3 的同一条纪律）：这条守卫必须先证明"目标真的存在"。
-- existingOutlinePixels == 0 ⇒ 守卫本轮不可能触发，此时 stackedSkipped=0 不代表版面干净。
local existingOutline = 0
if identifiable then
  for py = 0, img.height - 1 do
    for px = 0, img.width - 1 do
      if isOutline(px, py) then existingOutline = existingOutline + 1 end
    end
  end
end

local NO_STACK = ${noStack ? "true" : "false"}
local outlinePixels = {}
local stackedSkipped = 0
for py = 0, img.height - 1 do
  for px = 0, img.width - 1 do
    if isTransparent(img:getPixel(px, py)) then
      local edge = false
      if px > 0 and not isTransparent(img:getPixel(px - 1, py)) then edge = true end
      if not edge and px < img.width - 1 and not isTransparent(img:getPixel(px + 1, py)) then edge = true end
      if not edge and py > 0 and not isTransparent(img:getPixel(px, py - 1)) then edge = true end
      if not edge and py < img.height - 1 and not isTransparent(img:getPixel(px, py + 1)) then edge = true end
      if edge then
        -- Judged on the pre-draw snapshot: isOutline reads img, which this run does not write
        -- until the transaction below, so the answer is independent of iteration order.
        local stacked = NO_STACK and (isOutline(px, py + 1) or isOutline(px - 1, py) or isOutline(px + 1, py))
        if stacked then
          stackedSkipped = stackedSkipped + 1
        else
          outlinePixels[#outlinePixels + 1] = { px, py }
        end
      end
    end
  end
end

app.transaction(function()
  for _, p in ipairs(outlinePixels) do
    img:drawPixel(p[1], p[2], outlineColor)
  end
end)
${this.saveCelScript(savePath)}
local guardNote = nil
if not identifiable then
  guardNote = NO_STACK and "noStack could not identify the outline color in this sprite (non-#rrggbb color?) - the guard was inert"
    or nil
elseif existingOutline == 0 then
  guardNote = "no pixel of the snapshot already carried the outline color - the guard was inert"
end
io.write("__RESULT__" .. json.encode({ success = true, data = {
  outlinePixels = #outlinePixels,
  stackedSkipped = stackedSkipped,
  existingOutlinePixels = existingOutline,
  noStack = NO_STACK,
  guardIdentifiable = identifiable,
  colorMode = ({ "rgb", "grayscale", "indexed" })[spr.colorMode + 1] or tostring(spr.colorMode),
  guardNote = guardNote,
  savedTo = "${luaPath(savePath)}"
}}))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to add outline");
        return this.ok(result.data ?? result);
    }
    // -- Palette --
    async handleGetPalette(args) {
        const filePath = this.requireParam(args, "filePath");
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local pal = spr.palettes[1]
local colors = {}
for i = 0, #pal - 1 do
  local c = pal:getColor(i)
  table.insert(colors, { index = i, r = c.red, g = c.green, b = c.blue, a = c.alpha })
end
io.write("__RESULT__" .. json.encode({ success = true, data = { paletteSize = #pal, colors = colors } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to get palette");
        return this.ok(result.data ?? result);
    }
    async handleSetPaletteColors(args) {
        const filePath = this.requireParam(args, "filePath");
        const colors = args.colors;
        const savePath = this.optParam(args, "savePath") ?? filePath;
        if (!colors || !Array.isArray(colors)) {
            return this.error("colors array is required");
        }
        const setLines = colors
            .map((c) => `  pal:setColor(${c.index}, Color{ r=${c.r}, g=${c.g}, b=${c.b}, a=${c.a ?? 255} })`)
            .join("\n");
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local pal = spr.palettes[1]
local __before = __pal_snapshot(pal)
app.transaction(function()
${setLines}
end)
spr:saveCopyAs("${luaPath(savePath)}")
local __changes = __pal_changes(__before, __pal_snapshot(pal))
io.write("__RESULT__" .. json.encode({ success = true, data = { colorsSet = ${colors.length}, entries = #pal, palette_changes = __changes, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(`${this.p0CoreLua()}\n${script}`);
        if (!result.success)
            return this.error(result.error ?? "Failed to set palette colors");
        const data = (result.data ?? {});
        const history = await this.recordPaletteHistory({
            file: savePath,
            tool: "set_palette_colors",
            changes: (data.palette_changes ?? []),
        });
        return this.ok({ ...data, palette_history: history });
    }
    async handleLoadPalette(args) {
        const filePath = this.requireParam(args, "filePath");
        const palettePath = this.requireParam(args, "palettePath");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local __before = __pal_snapshot(spr.palettes[1])
spr:loadPalette("${luaPath(palettePath)}")
spr:saveCopyAs("${luaPath(savePath)}")
local __after = __pal_snapshot(spr.palettes[1])
io.write("__RESULT__" .. json.encode({ success = true, data = {
  paletteLoaded = "${luaPath(palettePath)}",
  entriesBefore = #__before,
  entriesAfter = #__after,
  palette_changes = __pal_changes(__before, __after),
  savedTo = "${luaPath(savePath)}"
} }))
`;
        const result = await this.runLuaScript(`${this.p0CoreLua()}\n${script}`);
        if (!result.success)
            return this.error(result.error ?? "Failed to load palette");
        const data = (result.data ?? {});
        const history = await this.recordPaletteHistory({
            file: savePath,
            tool: "load_palette",
            changes: (data.palette_changes ?? []),
            note: `loaded ${palettePath}`,
        });
        return this.ok({ ...data, palette_history: history });
    }
    async handleResizePalette(args) {
        const filePath = this.requireParam(args, "filePath");
        const newSize = this.numParam(args, "newSize");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local pal = spr.palettes[1]
local entriesBefore = #pal
pal:resize(${newSize})
local entriesAfter = #pal
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { newSize = ${newSize}, entries = entriesAfter, entriesBefore = entriesBefore, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to resize palette");
        return this.ok(result.data ?? result);
    }
    // -- Cel Operations --
    async handleMoveCel(args) {
        const filePath = this.requireParam(args, "filePath");
        const layerName = this.requireParam(args, "layerName");
        const frameNumber = this.numParam(args, "frameNumber", 1);
        const x = this.numParam(args, "x");
        const y = this.numParam(args, "y");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
${this.findLayerLua("target", layerName)}
local cel = target:cel(${frameNumber})
if not cel then
  io.write("__RESULT__" .. json.encode({ success = false, error = "No cel at frame ${frameNumber} on layer ${luaEscape(layerName)}" }))
  return
end
cel.position = Point(${x}, ${y})
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { position = {x=${x}, y=${y}}, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to move cel");
        return this.ok(result.data ?? result);
    }
    async handleSetCelOpacity(args) {
        const filePath = this.requireParam(args, "filePath");
        const layerName = this.requireParam(args, "layerName");
        const frameNumber = this.numParam(args, "frameNumber", 1);
        const opacity = this.numParam(args, "opacity");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
${this.findLayerLua("target", layerName)}
local cel = target:cel(${frameNumber})
if not cel then
  io.write("__RESULT__" .. json.encode({ success = false, error = "No cel at frame ${frameNumber} on layer ${luaEscape(layerName)}" }))
  return
end
cel.opacity = ${opacity}
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { opacity = ${opacity}, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to set cel opacity");
        return this.ok(result.data ?? result);
    }
    async handleClearCel(args) {
        const filePath = this.requireParam(args, "filePath");
        const layerName = this.requireParam(args, "layerName");
        const frameNumber = this.numParam(args, "frameNumber", 1);
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
${this.findLayerLua("target", layerName)}
local cel = target:cel(${frameNumber})
if cel then
  spr:deleteCel(cel)
end
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { cleared = true, savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to clear cel");
        return this.ok(result.data ?? result);
    }
    // -- Export --
    async handleExportSpriteSheet(args) {
        // C2 (2026-10-16): batch support. One round of farm02 exported 8 sheets with 8 separate calls
        // driven from a pwsh loop (through _call-tool.mjs) - each one a process spawn plus a hand-built
        // arg file. `outputs: [{filePath, outputImage, ...per-item overrides}]` runs them in one call.
        // Per-item values override the top-level defaults, so shared options are written once.
        const rawOutputs = args.outputs ?? args.sheets;
        if (rawOutputs !== undefined && rawOutputs !== null) {
            if (!Array.isArray(rawOutputs) || rawOutputs.length === 0) {
                return this.error("export_sprite_sheet: outputs must be a non-empty array of {filePath, outputImage, ...}");
            }
            const results = [];
            let okCount = 0;
            for (const [i, raw] of rawOutputs.entries()) {
                const item = { ...args, ...(raw ?? {}) };
                delete item.outputs;
                delete item.sheets;
                const filePath = typeof item.filePath === "string" ? item.filePath : "";
                const outputImage = typeof item.outputImage === "string" ? item.outputImage : "";
                if (!filePath || !outputImage) {
                    results.push({ index: i, filePath: filePath || null, outputImage: outputImage || null, error: "filePath and outputImage are required for every entry" });
                    continue;
                }
                const one = await this.exportOneSheet(item);
                if (one.ok) {
                    okCount += 1;
                    results.push({ index: i, ...one.data });
                }
                else {
                    results.push({ index: i, filePath, outputImage, error: one.error });
                }
            }
            const payload = {
                count: rawOutputs.length,
                ok_count: okCount,
                failed_count: rawOutputs.length - okCount,
                all_ok: okCount === rawOutputs.length,
                results,
            };
            // A batch that partially failed must not look like a success.
            if (okCount === 0)
                return this.error(`Sprite sheet batch export failed for all ${rawOutputs.length} entries: ${JSON.stringify(results.slice(0, 2))}`);
            return this.ok(payload);
        }
        const filePath = this.requireParam(args, "filePath");
        const outputImage = this.requireParam(args, "outputImage");
        const one = await this.exportOneSheet(args);
        if (!one.ok)
            return this.error(`Sprite sheet export failed: ${one.error}`);
        return this.ok(one.data);
    }
    /** One CLI sheet export; shared by the single call and the batch path. */
    async exportOneSheet(args) {
        const filePath = this.requireParam(args, "filePath");
        const outputImage = this.requireParam(args, "outputImage");
        const dataFile = this.optParam(args, "dataFile");
        const sheetType = this.optParam(args, "sheetType") ?? "rows";
        const columns = this.numParam(args, "columns");
        const rows = this.numParam(args, "rows");
        const borderPadding = this.numParam(args, "borderPadding");
        const shapePadding = this.numParam(args, "shapePadding");
        const innerPadding = this.numParam(args, "innerPadding");
        const trim = this.boolParam(args, "trim", false);
        const mergeDuplicates = this.boolParam(args, "mergeDuplicates", false);
        const layer = this.optParam(args, "layer");
        const tag = this.optParam(args, "tag");
        const splitLayers = this.boolParam(args, "splitLayers", false);
        const cliArgs = ["-b", filePath];
        if (layer)
            cliArgs.push("--layer", layer);
        if (tag)
            cliArgs.push("--tag", tag);
        if (splitLayers)
            cliArgs.push("--split-layers");
        if (trim)
            cliArgs.push("--trim");
        if (mergeDuplicates)
            cliArgs.push("--merge-duplicates");
        cliArgs.push("--sheet", outputImage);
        cliArgs.push("--sheet-type", sheetType);
        if (columns !== undefined)
            cliArgs.push("--sheet-columns", String(columns));
        if (rows !== undefined)
            cliArgs.push("--sheet-rows", String(rows));
        if (borderPadding !== undefined)
            cliArgs.push("--border-padding", String(borderPadding));
        if (shapePadding !== undefined)
            cliArgs.push("--shape-padding", String(shapePadding));
        if (innerPadding !== undefined)
            cliArgs.push("--inner-padding", String(innerPadding));
        if (dataFile) {
            cliArgs.push("--data", dataFile);
            cliArgs.push("--format", "json-array");
        }
        try {
            await this.runCliCommand(cliArgs);
            // C2 附带发现（2026-10-16）：Aseprite CLI 对**缺失的输入**也返回退出码 0，于是"导出成功"
            // 可以在什么都没写的情况下成立（实测 mixed 批次 ok_count=2 而 PNG 不存在）。
            // 产物存在性必须自证：写完再看盘。
            const imageBytes = await this.fileSize(outputImage);
            if (imageBytes === null) {
                return {
                    ok: false,
                    error: `Aseprite exited 0 but wrote no sheet at ${outputImage} (missing input file? bad --sheet path?)`,
                };
            }
            const dataBytes = dataFile ? await this.fileSize(dataFile) : null;
            if (dataFile && dataBytes === null) {
                return { ok: false, error: `Aseprite exited 0 but wrote no data file at ${dataFile}` };
            }
            return {
                ok: true,
                data: {
                    filePath,
                    outputImage,
                    dataFile: dataFile ?? null,
                    sheetType,
                    outputImageBytes: imageBytes,
                    dataFileBytes: dataBytes,
                },
            };
        }
        catch (err) {
            return { ok: false, error: err.message };
        }
    }
    async handleExportFrame(args) {
        const filePath = this.requireParam(args, "filePath");
        const frameNumber = this.numParam(args, "frameNumber", 1);
        const outputPath = this.requireParam(args, "outputPath");
        const layerName = this.optParam(args, "layerName");
        const cliArgs = ["-b", filePath];
        if (layerName)
            cliArgs.push("--layer", layerName);
        cliArgs.push("--frame-range", `${frameNumber - 1},${frameNumber - 1}`);
        cliArgs.push("--save-as", outputPath);
        try {
            await this.runCliCommand(cliArgs);
            return this.ok({ exportedFrame: frameNumber, outputPath });
        }
        catch (err) {
            return this.error(`Frame export failed: ${err.message}`);
        }
    }
    async handleExportLayers(args) {
        const filePath = this.requireParam(args, "filePath");
        const outputPattern = this.requireParam(args, "outputPattern");
        const frameNumber = this.numParam(args, "frameNumber");
        const cliArgs = ["-b", filePath, "--split-layers"];
        if (frameNumber !== undefined) {
            cliArgs.push("--frame-range", `${frameNumber - 1},${frameNumber - 1}`);
        }
        cliArgs.push("--save-as", outputPattern);
        try {
            await this.runCliCommand(cliArgs);
            return this.ok({ outputPattern });
        }
        catch (err) {
            return this.error(`Layer export failed: ${err.message}`);
        }
    }
    // -- Slices --
    async handleCreateSlice(args) {
        const filePath = this.requireParam(args, "filePath");
        const sliceName = this.requireParam(args, "sliceName");
        const x = this.numParam(args, "x");
        const y = this.numParam(args, "y");
        const width = this.numParam(args, "width");
        const height = this.numParam(args, "height");
        const pivotX = this.numParam(args, "pivotX");
        const pivotY = this.numParam(args, "pivotY");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        let pivotLine = "";
        if (pivotX !== undefined && pivotY !== undefined) {
            pivotLine = `slice.pivot = Point(${pivotX}, ${pivotY})`;
        }
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local slice = spr:newSlice(Rectangle(${x}, ${y}, ${width}, ${height}))
slice.name = "${luaEscape(sliceName)}"
${pivotLine}
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = {
  name = slice.name,
  bounds = { x = ${x}, y = ${y}, width = ${width}, height = ${height} },
  savedTo = "${luaPath(savePath)}"
} }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to create slice");
        return this.ok(result.data ?? result);
    }
    async handleRemoveSlice(args) {
        const filePath = this.requireParam(args, "filePath");
        const sliceName = this.requireParam(args, "sliceName");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local found = nil
for i, slice in ipairs(spr.slices) do
  if slice.name == "${luaEscape(sliceName)}" then found = slice; break end
end
if not found then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Slice not found: ${luaEscape(sliceName)}" }))
  return
end
spr:deleteSlice(found)
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = { removed = "${luaEscape(sliceName)}", savedTo = "${luaPath(savePath)}" } }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to remove slice");
        return this.ok(result.data ?? result);
    }
    // -- Utility --
    async handleRunScript(args) {
        const filePath = this.optParam(args, "filePath");
        const script = this.requireParam(args, "script");
        // C1: withMeter injects the meters' own Lua core into the same chunk (chunk-locals are visible
        // to the snippet that follows), so a one-off probe can call __px / __scan_cel / __composite
        // instead of hand-copying them. Same source string as the built-in tools -> no drift.
        const withMeter = this.boolParam(args, "withMeter", false) === true;
        const fullScript = withMeter
            ? `-- withMeter: MCP meter core injected inline (identical source to build/index.js)\n${this.p0CoreLua()}\n${script}`
            : script;
        const result = await this.runLuaScript(fullScript, filePath);
        return this.ok(result.data ?? result);
    }
    async handleGetAsepriteVersion(_args) {
        // Read it through Lua: the CLI's --version goes to stdout, which is unavailable
        // when the child runs with stdio "ignore". app.version is a Version userdata, so it
        // must be stringified before json.encode or it serializes to null.
        const script = `
io.write("__RESULT__" .. json.encode({ success = true, data = { version = tostring(app.version) } }))
`;
        try {
            const result = await this.runLuaScript(script);
            if (!result.success) {
                return this.error(result.error ?? "Failed to get Aseprite version");
            }
            return this.ok(result.data ?? result);
        }
        catch (err) {
            return this.error(`Failed to get Aseprite version: ${err.message}`, ["Ensure Aseprite is installed and ASEPRITE_PATH is set correctly"]);
        }
    }
    // ---- Pixel-art primitive handlers (2026-10-14) --------------------------
    /** Parse "#rrggbb" into [r,g,b]; falls back to white on malformed input. */
    parseHexColor(value) {
        const hex = String(value ?? "#ffffff").replace(/^#/, "").trim();
        if (!/^[0-9a-fA-F]{6}$/.test(hex))
            return [255, 255, 255];
        return [
            parseInt(hex.slice(0, 2), 16),
            parseInt(hex.slice(2, 4), 16),
            parseInt(hex.slice(4, 6), 16),
        ];
    }
    /** Lua that resolves `targetLayer` (by name, else the first layer). */
    layerSetupLua(layerName) {
        if (layerName)
            return this.findLayerLua("targetLayer", layerName);
        return `local targetLayer = spr.layers[1]`;
    }
    /**
     * Shared Lua body: snap every pixel to the nearest palette entry.
     * Skips `spr.transparentColor` (index 0 is the transparency slot, see SKILL.md),
     * keeps fully transparent pixels untouched, and caches by packed pixel value so
     * only unique colors pay the O(ncolors) scan.
     * Must run with `spr` in scope; defines __snapped / __scanned / __paletteSize.
     */
    snapLuaBody() {
        return `
local __pal = spr.palettes[1]
local __ncolors = #__pal
local __skip = spr.transparentColor
local __cache = {}
local function __snap(v)
  local hit = __cache[v]
  if hit ~= nil then return hit end
  local a = app.pixelColor.rgbaA(v)
  if a == 0 then __cache[v] = v return v end
  local r = app.pixelColor.rgbaR(v)
  local g = app.pixelColor.rgbaG(v)
  local b = app.pixelColor.rgbaB(v)
  local best, bdist = v, 1e18
  for i = 0, __ncolors - 1 do
    if i ~= __skip then
      local c = __pal:getColor(i)
      local dr = r - c.red
      local dg = g - c.green
      local db = b - c.blue
      local d = dr * dr * 0.299 + dg * dg * 0.587 + db * db * 0.114
      if d < bdist then bdist = d best = c end
    end
  end
  local out = app.pixelColor.rgba(best.red, best.green, best.blue, a)
  __cache[v] = out
  return out
end
local __snapped, __scanned = 0, 0
for _, __layer in ipairs(spr.layers) do
  for _, __cel in ipairs(__layer.cels) do
    local __img = __cel.image
    for y = 0, __img.height - 1 do
      for x = 0, __img.width - 1 do
        local v = __img:getPixel(x, y)
        __scanned = __scanned + 1
        local nv = __snap(v)
        if nv ~= v then __img:putPixel(x, y, nv) __snapped = __snapped + 1 end
      end
    end
  end
end
local __paletteSize = __ncolors
`;
    }
    async handleIndexify(args) {
        const filePath = this.requireParam(args, "filePath");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const dithering = this.optParam(args, "dithering") ?? "none";
        const colorCountRaw = args.colorCount;
        const colorCount = colorCountRaw === undefined || colorCountRaw === null ? null : Number(colorCountRaw);
        // ChangePixelFormat ALONE collapses the image: measured on a 4-color RGB sprite it
        // reported colorMode=INDEXED while paletteSize stayed 1 and the image went to ONE
        // unique color. colorCount on that command is ignored. The working order is
        // ColorQuantization (builds the palette) THEN ChangePixelFormat.
        // Second trap: ColorQuantization needs maxColors STRICTLY GREATER than the unique
        // color count, because index 0 is the transparency slot and eats one entry.
        // Measured on a 4-color sprite: maxColors=4 -> 1 color (collapsed), 5/8/16/256 -> 4 colors.
        const targetExpr = colorCount !== null && Number.isFinite(colorCount)
            ? String(Math.max(3, Math.min(256, Math.trunc(colorCount) + 1)))
            : "math.max(3, math.min(256, uniqBefore + 1))";
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local modeBefore = tostring(spr.colorMode)

local function countUnique(sprite)
  local seen, n = {}, 0
  for _, layer in ipairs(sprite.layers) do
    for _, cel in ipairs(layer.cels) do
      local img = cel.image
      for y = 0, img.height - 1 do
        for x = 0, img.width - 1 do
          local v = img:getPixel(x, y)
          if not seen[v] then seen[v] = true n = n + 1 end
        end
      end
    end
  end
  return n
end

local uniqBefore = countUnique(spr)
local target = ${targetExpr}
app.command.ColorQuantization{ maxColors = target }
app.command.ChangePixelFormat{ format = "indexed", dithering = "${dithering}" }
local uniqAfter = countUnique(spr)
local pal = spr.palettes[1]

-- Gate: never save, and never report success, for a collapse to a single color.
if uniqBefore > 1 and uniqAfter <= 1 then
  io.write("__RESULT__" .. json.encode({ success = false, error = "indexify collapsed " .. tostring(uniqBefore) .. " colors to 1; file NOT saved" }))
  return
end

spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = {
  savedTo = "${luaPath(savePath)}",
  colorModeBefore = modeBefore,
  colorModeAfter = tostring(spr.colorMode),
  paletteSize = #pal,
  transparentIndex = spr.transparentColor,
  uniqueColorsBefore = uniqBefore,
  uniqueColorsAfter = uniqAfter,
  quantizeTarget = target
} }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to indexify");
        return this.ok(result.data ?? result);
    }
    async handleGetLayerImage(args) {
        const filePath = this.requireParam(args, "filePath");
        const savePath = this.requireParam(args, "savePath");
        const layerName = this.optParam(args, "layerName");
        const frameNumber = this.numParam(args, "frameNumber") ?? 1;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
${this.layerSetupLua(layerName)}
local cel = targetLayer:cel(${frameNumber})
if not cel then
  io.write("__RESULT__" .. json.encode({ success = false, error = "No cel on that layer/frame" }))
  return
end
local img = cel.image
img:saveAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = {
  savedTo = "${luaPath(savePath)}",
  layer = targetLayer.name,
  frame = ${frameNumber},
  width = img.width,
  height = img.height,
  celBounds = { x = cel.position.x, y = cel.position.y, width = cel.bounds.width, height = cel.bounds.height }
} }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to read layer image");
        return this.ok(result.data ?? result);
    }
    async handleSetLayerImage(args) {
        const filePath = this.requireParam(args, "filePath");
        const imagePath = this.requireParam(args, "imagePath");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const layerName = this.optParam(args, "layerName");
        const frameNumber = this.numParam(args, "frameNumber") ?? 1;
        // app.open() switches the active sprite, so the source sprite is re-activated
        // before touching its cels (verified: app.activeSprite is assignable in batch mode).
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local incoming = app.open("${luaPath(imagePath)}")
if not incoming then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open source image" }))
  return
end
local newImg = incoming.cels[1].image
local srcW, srcH = newImg.width, newImg.height
app.activeSprite = spr
${this.layerSetupLua(layerName)}
local cel = targetLayer:cel(${frameNumber})
if not cel then
  cel = spr:newCel(targetLayer, ${frameNumber})
end
cel.image = newImg
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = {
  savedTo = "${luaPath(savePath)}",
  layer = targetLayer.name,
  frame = ${frameNumber},
  width = srcW,
  height = srcH,
  colorMode = tostring(spr.colorMode)
} }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to write layer image");
        return this.ok(result.data ?? result);
    }
    async handleSnapToPalette(args) {
        const filePath = this.requireParam(args, "filePath");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
if spr.colorMode == ColorMode.INDEXED then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Sprite is already INDEXED; pixels are palette indices already" }))
  return
end
${this.snapLuaBody()}
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = {
  savedTo = "${luaPath(savePath)}",
  paletteSize = __paletteSize,
  transparentIndex = spr.transparentColor,
  pixelsScanned = __scanned,
  pixelsSnapped = __snapped,
  uniqueColorsInCache = (function() local n = 0 for _ in pairs(__cache) do n = n + 1 end return n end)()
} }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to snap to palette");
        return this.ok(result.data ?? result);
    }
    async handleRemapPalette(args) {
        const filePath = this.requireParam(args, "filePath");
        const palettePath = this.requireParam(args, "palettePath");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const remapPixels = args.remapPixels === true;
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local palBefore = #spr.palettes[1]
-- R1 负例（live §6.2/§6.3）：INDEXED 稿上 remapPixels 是**范畴错误**，必须报错拒绝，
-- 不能"空转 + changed=0"（那正是假绿的形状）。校验放在任何写入之前 ⇒ 拒绝时状态零改动。
if ${remapPixels ? "true" : "false"} and spr.colorMode == ColorMode.INDEXED then
  io.write("__RESULT__" .. json.encode({ success = false, error =
    "remap_palette{remapPixels:true} is meaningless on an INDEXED sheet: pixels are palette INDICES, so "
    .. "'nearest colour in the new palette' would rewrite indices from decoded bytes. Call it again with "
    .. "remapPixels=false - swapping the palette already changes every colour, with zero pixel change.",
    diagnostics = { color_mode = "indexed", palette_size = palBefore, transparent_color = tostring(spr.transparentColor) } }))
  return
end
local __palBefore = __pal_snapshot(spr.palettes[1])
app.command.LoadPalette{ filename = "${luaPath(palettePath)}" }
local palAfter = #spr.palettes[1]
${remapPixels ? `${this.snapLuaBody()}
local snapped, scanned = __snapped, __scanned
` : `local snapped, scanned = 0, 0
`}
spr:saveCopyAs("${luaPath(savePath)}")
local __palChanges = __pal_changes(__palBefore, __pal_snapshot(spr.palettes[1]))
io.write("__RESULT__" .. json.encode({ success = true, data = {
  savedTo = "${luaPath(savePath)}",
  paletteSizeBefore = palBefore,
  paletteSizeAfter = palAfter,
  remapped = ${remapPixels ? "true" : "false"},
  pixelsScanned = scanned,
  pixelsSnapped = snapped,
  colorModeName = ({ "rgb", "grayscale", "indexed" })[spr.colorMode + 1] or tostring(spr.colorMode),
  palette_changes = __palChanges
} }))
`;
        const result = await this.runLuaScript(`${this.p0CoreLua()}\n${script}`);
        if (!result.success)
            return this.error(result.error ?? "Failed to remap palette");
        const data = (result.data ?? {});
        const history = await this.recordPaletteHistory({
            file: savePath,
            tool: "remap_palette",
            changes: (data.palette_changes ?? []),
            note: `remapPixels=${remapPixels} palette=${palettePath}`,
        });
        return this.ok({ ...data, palette_history: history });
    }
    async handleApplyMask(args) {
        const filePath = this.requireParam(args, "filePath");
        const maskPath = this.requireParam(args, "maskPath");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const layerName = this.optParam(args, "layerName");
        const frameNumber = this.numParam(args, "frameNumber") ?? 1;
        const mode = this.optParam(args, "mode") ?? "fill";
        const [fr, fg, fb] = this.parseHexColor(this.optParam(args, "color"));
        // No ApplyMask/NewMask command in Aseprite 1.3.16.1 (probed), so the mask is applied
        // pixel-wise: "inside" = mask pixel is opaque AND has a light red channel.
        const script = `
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local mspr = app.open("${luaPath(maskPath)}")
if not mspr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open mask" }))
  return
end
local mimg = mspr.cels[1].image
local mw, mh = mimg.width, mimg.height
-- 2026-10-16（lint 抓到的盲点）：掩膜也可能是 INDEXED 稿（PNG 转索引很常见）。索引图上没有 RGB，
-- "不透明且红通道 >127" 两个条件都无意义 ⇒ 索引模式按"索引 != 透明索引"判 inside。
local maskIndexed = (mspr.colorMode == ColorMode.INDEXED)
local maskTransparent = mspr.transparentColor
local maskModeName = ({ "rgb", "grayscale", "indexed" })[mspr.colorMode + 1] or tostring(mspr.colorMode)
app.activeSprite = spr
${this.layerSetupLua(layerName)}
local cel = targetLayer:cel(${frameNumber})
if not cel then
  io.write("__RESULT__" .. json.encode({ success = false, error = "No cel on that layer/frame" }))
  return
end
local img = cel.image
local mode = "${mode}"
local fill = app.pixelColor.rgba(${fr}, ${fg}, ${fb}, 255)
local touched, insideCount, scanned = 0, 0, 0
for y = 0, img.height - 1 do
  for x = 0, img.width - 1 do
    scanned = scanned + 1
    local inside = false
    if x < mw and y < mh then
      local mv = mimg:getPixel(x, y)
      if maskIndexed then
        inside = (mv ~= maskTransparent)
      else
        inside = (app.pixelColor.rgbaA(mv) > 127) and (app.pixelColor.rgbaR(mv) > 127)
      end
    end
    if inside then insideCount = insideCount + 1 end
    local v = img:getPixel(x, y)
    local nv = v
    if mode == "keep" then
      if not inside then nv = 0 end
    elseif inside then
      if mode == "clear" then nv = 0 else nv = fill end
    end
    if nv ~= v then img:putPixel(x, y, nv) touched = touched + 1 end
  end
end
spr:saveCopyAs("${luaPath(savePath)}")
io.write("__RESULT__" .. json.encode({ success = true, data = {
  savedTo = "${luaPath(savePath)}",
  layer = targetLayer.name,
  mode = mode,
  maskSize = tostring(mw) .. "x" .. tostring(mh),
  maskColorMode = maskModeName,
  mask_basis = maskIndexed and "index ~= mask.transparentColor" or "alpha > 127 and red > 127",
  pixelsScanned = scanned,
  pixelsInsideMask = insideCount,
  pixelsTouched = touched
} }))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(result.error ?? "Failed to apply mask");
        return this.ok(result.data ?? result);
    }
    // ---- Gate engine (B1) ----------------------------------------------------
    /**
     * Python used by the vision checkers.
     *
     * Order: DSH_PYTHON_PATH -> the interpreter bundled with DSH under the user's home ->
     * python3/python on PATH. Nothing here may hardcode one machine's layout.
     */
    pythonPath() {
        if (process.env.DSH_PYTHON_PATH)
            return process.env.DSH_PYTHON_PATH;
        const home = process.env.DSH_HOME || join(homedir(), ".dsh");
        const bundled = join(home, "dsh-runtimes", "dsh-primary-runtime", "dependencies", "python", "python.exe");
        if (existsSync(bundled))
            return bundled;
        return process.platform === "win32" ? "python" : "python3";
    }
    /**
     * `vision/` ships next to `mcp/` inside the installed toolbox
     * (<toolkit>/mcp/build/index.js -> <toolkit>/vision).
     * Override with ASEPRITE_VISION_DIR.
     */
    visionDir() {
        if (process.env.ASEPRITE_VISION_DIR)
            return process.env.ASEPRITE_VISION_DIR;
        return join(dirname(fileURLToPath(import.meta.url)), "..", "..", "vision");
    }
    /**
     * Spawn a child with stdout+stderr pointed at a FILE descriptor.
     * Piped stdio is EPERM under the DSH sandbox, so nothing here may use "pipe".
     */
    async runToLog(cmd, argv, logPath, timeoutMs = 180000) {
        const fd = openSync(logPath, "w");
        try {
            return await new Promise((resolve) => {
                let child;
                try {
                    child = spawn(cmd, argv, { stdio: ["ignore", fd, fd], timeout: timeoutMs });
                }
                catch {
                    resolve(null);
                    return;
                }
                child.on("error", () => resolve(null));
                child.on("close", (code) => resolve(code));
            });
        }
        finally {
            closeSync(fd);
        }
    }
    async handleVerifyArtifact(args) {
        const artifactPath = this.requireParam(args, "artifactPath");
        if (!existsSync(artifactPath))
            return this.error(`Artifact not found: ${artifactPath}`);
        const palettePath = this.optParam(args, "palettePath") ?? "";
        const wantSidecar = args.sidecar !== false;
        // farm02 round 2 / F.3: every gate must declare the FEATURE it needs to see, and a gate whose
        // feature is absent from the artifact is CHECKER_INVALID - never PASS. verify-idle.py hardcoded
        // outline color #131313 while the character sheet's outline is #171310, so "outline did not
        // thicken: 0 blocks" was reported against a meter that could never see its own target, in five
        // separate spec documents. A meter that cannot find its target reports 0 for everything.
        const rawProbes = args.probes ?? args.probeColors ?? args.probe_colors;
        const probes = [];
        if (rawProbes !== undefined && rawProbes !== null) {
            if (!Array.isArray(rawProbes)) {
                return this.error('probes must be an array of { color: "#rrggbb", minPixels?, id? }');
            }
            for (const item of rawProbes) {
                const obj = (item ?? {});
                const color = typeof obj.color === "string" ? obj.color.trim() : "";
                if (!/^#?[0-9a-fA-F]{6}$/.test(color)) {
                    return this.error(`probes[].color must be #rrggbb, got ${JSON.stringify(color)}`);
                }
                const norm = `#${color.replace(/^#/, "").toLowerCase()}`;
                probes.push({
                    id: typeof obj.id === "string" && obj.id ? obj.id : "feature.color_present",
                    color: norm,
                    minPixels: typeof obj.minPixels === "number" ? obj.minPixels : 1,
                });
            }
        }
        const script = join(this.visionDir(), "discipline-metrics.py");
        if (!existsSync(script)) {
            return this.error(`Checker not found: ${script} (set ASEPRITE_VISION_DIR)`);
        }
        const py = this.pythonPath();
        if (!existsSync(py)) {
            return this.error(`Python not found: ${py} (set DSH_PYTHON_PATH)`);
        }
        await mkdir(TMP_DIR, { recursive: true });
        // Gate on the meter itself FIRST. A checker that cannot pass its own selftest must
        // never be allowed to certify an artifact: "always fails" and "always passes" are
        // both non-instruments.
        const stLog = join(TMP_DIR, `verify-selftest-${randomUUID()}.log`);
        const stCode = await this.runToLog(py, [script, "--selftest"], stLog);
        let stText = "";
        try {
            stText = await readFile(stLog, "utf-8");
        }
        catch {
            stText = "";
        }
        await unlink(stLog).catch(() => undefined);
        const checkerOk = stCode === 0 && /SELFTEST=PASS/.test(stText);
        const selfLine = stText.split("\n").map((l) => l.trim()).filter((l) => l.startsWith("SELFTEST="))[0] || "";
        // Then the artifact itself.
        const jsonPath = join(TMP_DIR, `verify-${randomUUID()}.json`);
        const logPath = join(TMP_DIR, `verify-${randomUUID()}.log`);
        const pyArgs = ["--glob", artifactPath, "--json-out", jsonPath];
        if (palettePath)
            pyArgs.push("--palette", palettePath);
        for (const p of probes)
            pyArgs.push("--probe-color", p.color);
        const code = await this.runToLog(py, [script, ...pyArgs], logPath);
        let parsed = null;
        let parseError = "";
        try {
            parsed = JSON.parse(await readFile(jsonPath, "utf-8"));
        }
        catch (err) {
            parseError = err.message;
        }
        await unlink(jsonPath).catch(() => undefined);
        await unlink(logPath).catch(() => undefined);
        const rows = (parsed?.rows ?? []);
        if (rows.length === 0) {
            return this.error(`Checker produced no rows (exit=${code}). ${parseError || "Check artifact path/format."}`);
        }
        const gates = [];
        const invalidReasons = [];
        const paletteSize = Number(parsed?.palette_size ?? 0);
        for (const r of rows) {
            const p = rows.length > 1 ? `${r.file}: ` : "";
            // Vacuity guard: with no opaque pixel every ratio below is 0 for free, so a "clean" verdict
            // would be a false green produced by an empty measurement rather than by good pixels.
            const empty = r.opaque_px === 0;
            if (empty)
                invalidReasons.push(`${p}opaque_px=0 (nothing to measure)`);
            const vacuousDetail = `${p}opaque_px=0: no opaque pixel, so this ratio is vacuously 0`;
            gates.push(empty
                ? {
                    id: "alpha.binary",
                    ok: null,
                    status: "CHECKER_INVALID",
                    detail: vacuousDetail,
                    spec_gate: "alpha",
                }
                : {
                    id: "alpha.binary",
                    ok: r.alpha_non_binary === 0,
                    detail: `${p}alpha_non_binary=${r.alpha_non_binary}`,
                    spec_gate: "alpha",
                });
            gates.push(empty
                ? {
                    id: "dither.checker_cells",
                    ok: null,
                    status: "CHECKER_INVALID",
                    detail: vacuousDetail,
                    spec_gate: "dither",
                }
                : {
                    id: "dither.checker_cells",
                    ok: r.checker_cell_ratio <= 0.03,
                    detail: `${p}checker=${(r.checker_cell_ratio * 100).toFixed(2)}% (limit 3%)`,
                    spec_gate: "dither",
                });
            gates.push(empty
                ? {
                    id: "dither.isolated",
                    ok: null,
                    status: "CHECKER_INVALID",
                    detail: vacuousDetail,
                    spec_gate: "dither",
                }
                : {
                    id: "dither.isolated",
                    ok: r.isolated4_ratio <= 0.08,
                    detail: `${p}isolated=${(r.isolated4_ratio * 100).toFixed(2)}% (limit 8%)`,
                    spec_gate: "dither",
                });
            // The palette gate declares two features: a palette file that actually has entries, and
            // pixels to compare against it. Either one missing makes "0 colors outside" meaningless.
            if (!palettePath) {
                gates.push({
                    id: "palette.outside",
                    ok: null,
                    detail: "skipped: no palettePath given",
                    spec_gate: "palette",
                });
            }
            else if (paletteSize <= 0) {
                invalidReasons.push(`${p}palette has ${paletteSize} entries`);
                gates.push({
                    id: "palette.outside",
                    ok: null,
                    status: "CHECKER_INVALID",
                    detail: `${p}declared palette has ${paletteSize} entries: "outside palette" cannot be judged against an empty palette`,
                    spec_gate: "palette",
                });
            }
            else if (empty) {
                gates.push({
                    id: "palette.outside",
                    ok: null,
                    status: "CHECKER_INVALID",
                    detail: vacuousDetail,
                    spec_gate: "palette",
                });
            }
            else {
                gates.push({
                    id: "palette.outside",
                    ok: r.colors_outside_palette === 0,
                    detail: `${p}colors_outside_palette=${r.colors_outside_palette} (palette ${paletteSize} entries)`,
                    spec_gate: "palette",
                });
            }
            // Caller-declared feature probes: the gate names the feature it looks for and reports the
            // pixels it actually hit. 0 hits => the meter cannot see its target => not a PASS.
            for (const pr of probes) {
                const perRow = (r.probes ?? {});
                const hits = Number(perRow[pr.color] ?? 0);
                if (hits === 0) {
                    invalidReasons.push(`${p}declared feature ${pr.color} absent`);
                    gates.push({
                        id: `feature.present ${pr.color}`,
                        ok: null,
                        status: "CHECKER_INVALID",
                        feature_hits: 0,
                        detail: `${p}declared feature ${pr.color} appears 0 times in the artifact: this gate cannot ` +
                            `see its own target, so its verdict would be vacuous (id='${pr.id}')`,
                        spec_gate: "feature",
                    });
                }
                else {
                    gates.push({
                        id: `feature.present ${pr.color}`,
                        ok: hits >= pr.minPixels,
                        feature_hits: hits,
                        detail: `${p}${pr.color} hits=${hits} (min ${pr.minPixels}) (id='${pr.id}')`,
                        spec_gate: "feature",
                    });
                }
            }
        }
        const failed = gates.filter((g) => g.ok === false);
        const invalid = !checkerOk ? "checker selftest failed" : invalidReasons.length ? invalidReasons.join("; ") : "";
        const verdict = invalid ? "CHECKER_INVALID" : failed.length ? "FAIL" : "PASS";
        const payload = {
            artifact: artifactPath,
            verdict,
            checker: {
                ok: checkerOk,
                selftest_line: selfLine,
                selftest_exit: stCode,
                invalid_reasons: invalidReasons,
            },
            probes_declared: probes.map((p) => ({ id: p.id, color: p.color, minPixels: p.minPixels })),
            feature_hits: rows.map((r) => ({ file: r.file, probes: r.probes ?? null })),
            gates,
            metrics: rows.map((r) => ({
                file: r.file,
                size: r.size,
                opaque_px: r.opaque_px,
                unique_colors: r.unique_colors,
                alpha_non_binary: r.alpha_non_binary,
                checker_cell_ratio: r.checker_cell_ratio,
                isolated4_ratio: r.isolated4_ratio,
            })),
            palette_path: palettePath || null,
            palette_size: palettePath ? paletteSize : null,
            verified_at: new Date().toISOString(),
            note: verdict === "PASS"
                ? "All machine-checkable gates passed, and every gate could see the feature it measures."
                : verdict === "CHECKER_INVALID"
                    ? `REFUSED: ${invalid}. A meter that cannot see its own target must not certify anything.`
                    : `FAILED gates: ${failed.map((f) => f.id).join(", ")}`,
            // Honesty field: these spec gates are NOT covered here, so a PASS must not be
            // read as "this artifact satisfies the whole spec".
            gates_not_machine_checkable: [
                "per_tool_selftest",
                "metric_consistency",
                "layered_artifact (needs layer PNGs + composite)",
                "no_new_colors",
                "silhouette",
            ],
        };
        // Only a valid meter may leave a "verified" mark. Writing a sidecar for
        // CHECKER_INVALID would let a broken instrument certify an artifact by side effect.
        if (wantSidecar && verdict !== "CHECKER_INVALID") {
            const sidecar = `${artifactPath}.verify.json`;
            await writeFile(sidecar, JSON.stringify(payload, null, 1), "utf-8");
            payload.sidecar = sidecar;
        }
        else {
            payload.sidecar = null;
            payload.sidecar_skipped =
                verdict === "CHECKER_INVALID"
                    ? "CHECKER_INVALID: refusing to mark the artifact as verified"
                    : "sidecar=false was requested";
        }
        return this.ok(payload);
    }
    // ==========================================================================
    // Batch-edit + verification family (crop_cels / measure_sprite / remap_colors / diff_sprites)
    //
    // Derived from the farm02 sessions: each of these replaced a Lua/Python loop that was
    // hand-written 3-6 times per round. Semantics were probed against Aseprite 1.3.16.1
    // before implementation (see aseprite/_probe/p0-*.lua), which is how two real traps
    // were found and avoided:
    //   1. spr:crop only changes the canvas - cel data survives, so "shrink then re-expand"
    //      is a no-op (measured 400 -> 400 -> 400 px). A true crop must rebuild every
    //      cel.image and read the old pixels through cel.bounds.
    //   2. Image:drawImage() does NOT respect a negative destination offset nor a srcRect
    //      offset as expected: drawing an 8x8 source into 4x4 at Point(-2,-2) fills all 16
    //      destination pixels, and the 3-arg (srcRect, dstRect) overload sampled the source
    //      at (0,0) instead of srcRect.x/y (a 4x4 keep-case silently became 256 px). So the
    //      crop path copies pixels manually over the overlap only - verified lossless.
    // ==========================================================================
    /** Shared Lua helpers: content scanning, alpha fingerprint, frame composite, CIE L* and HSV. */
    p0CoreLua() {
        return `
local function __bbox(x0, y0, x1, y1)
  if not x0 then return nil end
  return { x = x0, y = y0, w = x1 - x0 + 1, h = y1 - y0 + 1 }
end

local function __round(v)
  if v == nil then return nil end
  return math.floor(v * 10000 + 0.5) / 10000
end

-- R1 (live §6.3): order-independent index fingerprint over every cel (honours cel.bounds).
-- INDEXED only: on an RGB sheet (v ~= transparentColor) is meaningless - use __alpha_fp there.
local function __index_hist(spr)
  local h, total = {}, 0
  local tc = spr.transparentColor
  for _, c in ipairs(spr.cels) do
    local img, b = c.image, c.bounds
    for y = 0, b.height - 1 do
      for x = 0, b.width - 1 do
        local v = img:getPixel(x, y)
        if v ~= tc then
          h[v] = (h[v] or 0) + 1
          total = total + 1
        end
      end
    end
  end
  return h, total
end

-- R3 (live §6.3): palette snapshot / diff, so a palette edit can leave a before/after trace.
local function __pal_snapshot(pal)
  local out = {}
  for i = 0, #pal - 1 do
    local c = pal:getColor(i)
    out[i + 1] = string.format("#%02x%02x%02x", c.red, c.green, c.blue)
  end
  return out
end

local function __pal_changes(before, after)
  local ch = {}
  local n = math.max(#before, #after)
  for i = 1, n do
    if before[i] ~= after[i] then
      ch[#ch + 1] = { index = i - 1, before = before[i], after = after[i] }
    end
  end
  return ch
end

-- Mode-aware pixel decode. In INDEXED mode getPixel returns a palette INDEX, so
-- app.pixelColor.rgbaA(index) is a meaningless byte (usually 0) and every opacity test
-- silently reads "transparent". This helper is the single place that knows the rule;
-- never call rgbaA(getPixel(...)) directly (2026-10-16: that blind spot made
-- measure_layers / measure_sprite / diff_sprites / crop_cels lie on INDEXED sheets).
local function __px(spr, v, pal)
  if spr.colorMode == ColorMode.INDEXED then
    if v == spr.transparentColor then return 0, 0, 0, 0 end
    local p = pal or spr.palettes[1]
    local c = p:getColor(v)
    return 255, c.red, c.green, c.blue
  end
  return app.pixelColor.rgbaA(v), app.pixelColor.rgbaR(v), app.pixelColor.rgbaG(v), app.pixelColor.rgbaB(v)
end

-- Opacity scan of one cel, honouring cel.bounds (cel.image origin != sprite origin).
local function __scan_cel(spr, cel, rx, ry, rw, rh)
  local img, b = cel.image, cel.bounds
  local sx0, sy0, sx1, sy1 = 0, 0, b.width, b.height
  if rw then
    sx0 = math.max(0, rx - b.x)
    sy0 = math.max(0, ry - b.y)
    sx1 = math.min(b.width, rx + rw - b.x)
    sy1 = math.min(b.height, ry + rh - b.y)
  end
  local n, X0, Y0, X1, Y1 = 0, nil, nil, nil, nil
  for y = sy0, sy1 - 1 do
    for x = sx0, sx1 - 1 do
      if (__px(spr, img:getPixel(x, y))) > 0 then
        n = n + 1
        local wx, wy = b.x + x, b.y + y
        if not X0 or wx < X0 then X0 = wx end
        if not Y0 or wy < Y0 then Y0 = wy end
        if not X1 or wx > X1 then X1 = wx end
        if not Y1 or wy > Y1 then Y1 = wy end
      end
    end
  end
  return n, X0, Y0, X1, Y1
end

-- one frame (used by the measuring tools)
local function __scan_frame(spr, fi, rx, ry, rw, rh)
  local n, X0, Y0, X1, Y1 = 0, nil, nil, nil, nil
  for li = 1, #spr.layers do
    local layer = spr.layers[li]
    if not layer.isGroup then
      local cel = layer:cel(fi)
      if cel then
        local cn, x0, y0, x1, y1 = __scan_cel(spr, cel, rx, ry, rw, rh)
        n = n + cn
        if x0 then
          if not X0 or x0 < X0 then X0 = x0 end
          if not Y0 or y0 < Y0 then Y0 = y0 end
          if not X1 or x1 > X1 then X1 = x1 end
          if not Y1 or y1 > Y1 then Y1 = y1 end
        end
      end
    end
  end
  return n, X0, Y0, X1, Y1
end

-- all frames (used by crop_cels, which rebuilds every cel it finds)
local function __scan_all(spr, rx, ry, rw, rh)
  local n, X0, Y0, X1, Y1 = 0, nil, nil, nil, nil
  for fi = 1, #spr.frames do
    local fn, x0, y0, x1, y1 = __scan_frame(spr, fi, rx, ry, rw, rh)
    n = n + fn
    if x0 then
      if not X0 or x0 < X0 then X0 = x0 end
      if not Y0 or y0 < Y0 then Y0 = y0 end
      if not X1 or x1 > X1 then X1 = x1 end
      if not Y1 or y1 > Y1 then Y1 = y1 end
    end
  end
  return n, X0, Y0, X1, Y1
end

-- alpha fingerprint: opaque count + alpha sum + position-weighted sum.
-- Re-read from the saved file it is an independent check that a color edit kept the silhouette.
local function __alpha_fp(spr, rx, ry, rw, rh)
  local opaque, sum, weighted = 0, 0, 0
  for li = 1, #spr.layers do
    local layer = spr.layers[li]
    if not layer.isGroup then
      for fi = 1, #spr.frames do
        local cel = layer:cel(fi)
        if cel then
          local img, b = cel.image, cel.bounds
          local sx0, sy0, sx1, sy1 = 0, 0, b.width, b.height
          if rw then
            sx0 = math.max(0, rx - b.x)
            sy0 = math.max(0, ry - b.y)
            sx1 = math.min(b.width, rx + rw - b.x)
            sy1 = math.min(b.height, ry + rh - b.y)
          end
          for y = sy0, sy1 - 1 do
            for x = sx0, sx1 - 1 do
              local a = __px(spr, img:getPixel(x, y))
              if a > 0 then
                opaque = opaque + 1
                sum = sum + a
                weighted = weighted + a * (1 + ((b.x + x) % 97) + 7 * ((b.y + y) % 89) + 13 * fi)
              end
            end
          end
        end
      end
    end
  end
  return opaque, sum, weighted
end

-- Composite one frame onto a canvas-sized image.
-- Single-frame sprites use the engine's own flatten (real blending);
-- multi-frame sprites fall back to the topmost opaque pixel per position.
local function __composite(spr, frameNumber, W, H)
  local out = Image(W, H, spr.colorMode)
  if #spr.frames == 1 then
    spr:flatten()
    local layer = spr.layers[1]
    local cel = layer and layer:cel(1)
    if cel then
      local img, b = cel.image, cel.bounds
      for y = 0, b.height - 1 do
        local wy = b.y + y
        if wy >= 0 and wy < H then
          for x = 0, b.width - 1 do
            local wx = b.x + x
            if wx >= 0 and wx < W then out:putPixel(wx, wy, img:getPixel(x, y)) end
          end
        end
      end
    end
    return out, "flatten_composite"
  end
  local filled = {}
  for li = #spr.layers, 1, -1 do
    local layer = spr.layers[li]
    if not layer.isGroup then
      local cel = layer:cel(frameNumber)
      if cel then
        local img, b = cel.image, cel.bounds
        for y = 0, b.height - 1 do
          local wy = b.y + y
          if wy >= 0 and wy < H then
            for x = 0, b.width - 1 do
              local wx = b.x + x
              if wx >= 0 and wx < W and not filled[wy * W + wx] then
                local px = img:getPixel(x, y)
                if (__px(spr, px)) > 0 then
                  out:putPixel(wx, wy, px)
                  filled[wy * W + wx] = true
                end
              end
            end
          end
        end
      end
    end
  end
  return out, "topmost_opaque_per_position"
end

local function __lin(c)
  c = c / 255
  if c <= 0.04045 then return c / 12.92 end
  return ((c + 0.055) / 1.055) ^ 2.4
end

-- CIE L* (D65, Yn = 1) - same formulas as projects/farm02/tools/ref-colors.py so the
-- numbers can be compared with the analyses that drove the farm02 color decisions.
local function __lstar(r, g, b)
  local Y = 0.2126 * __lin(r) + 0.7152 * __lin(g) + 0.0722 * __lin(b)
  if Y > 0.008856 then return 116.0 * (Y ^ (1 / 3)) - 16.0 end
  return 903.3 * Y
end

-- HSV hue (deg) / saturation, matching the Python hsv() in the same scripts.
local function __hsv(r, g, b)
  local mx = math.max(r, g, b)
  local mn = math.min(r, g, b)
  local d = mx - mn
  local sat = 0
  if mx > 0 then sat = d / mx end
  local hue = 0
  if d > 0 then
    if mx == r then hue = ((g - b) / d) % 6
    elseif mx == g then hue = (b - r) / d + 2
    else hue = (r - g) / d + 4 end
    hue = (hue * 60) % 360
  end
  return hue, sat
end

local function __hex(r, g, b)
  return string.format("#%02x%02x%02x", r, g, b)
end

-- Percentile from a 0.1-wide L* histogram (bins 1..1001).
local function __pct(hist, total, p)
  if total <= 0 then return nil end
  local target = math.floor(total * p)
  if target < 1 then target = 1 end
  local acc = 0
  for i = 1, #hist do
    acc = acc + hist[i]
    if acc >= target then return (i - 0.5) / 10 end
  end
  return (#hist - 0.5) / 10
end
`;
    }
    /** Parse a {x,y,w,h} object param; throws a descriptive Error on malformed input. */
    rectParam(args, name) {
        const raw = args[name] ?? args[this.toSnakeCase(name)];
        if (raw === undefined || raw === null)
            return null;
        if (typeof raw !== "object" || Array.isArray(raw)) {
            throw new Error(`${name} must be an object {x,y,w,h}`);
        }
        const o = raw;
        const nums = ["x", "y", "w", "h"].map((k) => Number(o[k]));
        if (nums.some((v) => !Number.isFinite(v))) {
            throw new Error(`${name} must have finite numeric x, y, w and h`);
        }
        return { x: nums[0], y: nums[1], w: nums[2], h: nums[3] };
    }
    /** Optional layer-name list; accepts an array or a comma-separated string. */
    layerNamesParam(args, name) {
        const raw = args[name] ?? args[this.toSnakeCase(name)];
        if (raw === undefined || raw === null || raw === "")
            return null;
        if (Array.isArray(raw))
            return raw.map((v) => String(v));
        return String(raw)
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean);
    }
    /** Parse a #rrggbb string into channels; null when malformed. */
    parseHexRgb(v) {
        if (typeof v !== "string")
            return null;
        const m = /^#?([0-9a-fA-F]{6})$/.exec(v.trim());
        if (!m)
            return null;
        const h = m[1].toLowerCase();
        return {
            r: parseInt(h.slice(0, 2), 16),
            g: parseInt(h.slice(2, 4), 16),
            b: parseInt(h.slice(4, 6), 16),
            hex: "#" + h,
        };
    }
    luaRect(r) {
        return r === null ? "nil" : `{ ${r.x}, ${r.y}, ${r.w}, ${r.h} }`;
    }
    // -- crop_cels ------------------------------------------------------------
    async handleCropCels(args) {
        const filePath = this.requireParam(args, "filePath");
        const rect = this.rectParam(args, "rect");
        const anchor = (this.optParam(args, "anchor") ?? "top-left").toLowerCase();
        if (anchor !== "top-left" && anchor !== "content") {
            return this.error(`crop_cels: anchor must be "top-left" or "content" (got "${anchor}")`);
        }
        const padToTile = this.numParam(args, "padToTile");
        if (padToTile !== undefined && (!Number.isFinite(padToTile) || padToTile < 1)) {
            return this.error("crop_cels: padToTile must be a positive number");
        }
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const t0 = Date.now();
        const script = `
${this.p0CoreLua()}
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local OLDW, OLDH = spr.width, spr.height
local REQ = ${this.luaRect(rect)}
local ANCHOR = "${anchor}"
local PAD = ${padToTile === undefined ? "nil" : String(padToTile)}
local SAVE = "${luaPath(savePath)}"

local cn, cx0, cy0, cx1, cy1 = __scan_all(spr, nil)

local rx, ry, rw, rh
if REQ then
  rx, ry, rw, rh = REQ[1], REQ[2], REQ[3], REQ[4]
else
  if not cx0 then
    -- C4 (2026-10-16): 拒绝时把"为什么"带上。旧消息只有 "no opaque pixels"，看不出是**真的空稿**
    -- 还是**量尺瞎了**（INDEXED 盲区那次就是后者，只能靠交叉验证才发现）。
    local modeName = ({ "rgb", "grayscale", "indexed" })[spr.colorMode + 1] or tostring(spr.colorMode)
    io.write("__RESULT__" .. json.encode({ success = false, error =
      "Sprite has no opaque pixels; pass an explicit rect"
      .. " [color_mode=" .. modeName
      .. " canvas=" .. spr.width .. "x" .. spr.height
      .. " layers=" .. #spr.layers .. " frames=" .. #spr.frames
      .. " opaque_px=" .. cn
      .. " transparent_color=" .. tostring(spr.transparentColor) .. "]"
      .. (spr.colorMode == ColorMode.INDEXED
        and " - INDEXED opacity is 'index ~= transparentColor' decoded through the palette, so 0 means the sheet really is empty"
        or " - no cel pixel had alpha > 0"),
      diagnostics = {
        color_mode = modeName,
        canvas = spr.width .. "x" .. spr.height,
        layers = #spr.layers,
        frames = #spr.frames,
        opaque_px = cn,
        transparent_color = tostring(spr.transparentColor)
      } }))
    return
  end
  rx, ry, rw, rh = cx0, cy0, cx1 - cx0 + 1, cy1 - cy0 + 1
end

if ANCHOR == "content" and cx0 then
  local ax0 = math.min(rx, cx0)
  local ay0 = math.min(ry, cy0)
  local ax1 = math.max(rx + rw, cx1 + 1)
  local ay1 = math.max(ry + rh, cy1 + 1)
  rx, ry, rw, rh = ax0, ay0, ax1 - ax0, ay1 - ay0
end

local padded = false
if PAD and PAD > 0 then
  local nw = math.ceil(rw / PAD) * PAD
  local nh = math.ceil(rh / PAD) * PAD
  local ex, ey = nw - rw, nh - rh
  if ANCHOR == "content" then
    rx = rx - math.floor(ex / 2)
    ry = ry - math.floor(ey / 2)
  end
  rw, rh = nw, nh
  padded = (ex > 0 or ey > 0)
end

if rw <= 0 or rh <= 0 then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Crop rect is empty after padding" }))
  return
end

local inside = __scan_all(spr, rx, ry, rw, rh)

local layerNames = {}
for li = 1, #spr.layers do
  local layer = spr.layers[li]
  if not layer.isGroup then
    layerNames[#layerNames + 1] = layer.name
    for fi = 1, #spr.frames do
      local cel = layer:cel(fi)
      if cel then
        local old, b = cel.image, cel.bounds
        local ni = Image(rw, rh, spr.colorMode)
        local dx, dy = b.x - rx, b.y - ry
        local sx0 = math.max(0, -dx)
        local sy0 = math.max(0, -dy)
        local sx1 = math.min(b.width, rw - dx)
        local sy1 = math.min(b.height, rh - dy)
        for y = sy0, sy1 - 1 do
          for x = sx0, sx1 - 1 do
            ni:putPixel(x + dx, y + dy, old:getPixel(x, y))
          end
        end
        cel.image = ni
        cel.position = Point(0, 0)
      end
    end
  end
end

-- canvas only: spr:resize would SCALE content (probed: a 64x64 cel became 32x32).
spr:crop(0, 0, rw, rh)

local after, ax0, ay0, ax1, ay1 = __scan_all(spr, nil)
spr:saveCopyAs(SAVE)

io.write("__RESULT__" .. json.encode({ success = true, data = {
  canvas = { w = spr.width, h = spr.height },
  canvas_before = { w = OLDW, h = OLDH },
  rect = { x = rx, y = ry, w = rw, h = rh },
  content_bbox = __bbox(ax0, ay0, ax1, ay1),
  content_bbox_before = __bbox(cx0, cy0, cx1, cy1),
  pixel_before = cn,
  pixel_inside_rect_before = inside,
  pixel_after = after,
  discarded_pixels = cn - inside,
  lossless = (after == inside),
  padded = padded,
  rect_outside_canvas = (rx < 0 or ry < 0 or rx + rw > OLDW or ry + rh > OLDH),
  layers = layerNames,
  savedTo = SAVE
}}))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(`crop_cels: ${result.error ?? "failed"}`);
        const data = (result.data ?? {});
        return this.ok({ ...data, elapsed_ms: Date.now() - t0 });
    }
    // -- measure_sprite -------------------------------------------------------
    async handleMeasureSprite(args) {
        const filePath = this.requireParam(args, "filePath");
        const region = this.rectParam(args, "region");
        const topColors = this.numParam(args, "topColors", 12);
        const frameNumber = this.numParam(args, "frameNumber", 1);
        const t0 = Date.now();
        const script = `
${this.p0CoreLua()}
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local W, H = spr.width, spr.height
local R = ${this.luaRect(region)}
local FN = ${frameNumber}
local TOPN = ${topColors}
if FN < 1 or FN > #spr.frames then
  io.write("__RESULT__" .. json.encode({ success = false, error = "frameNumber out of range (1.." .. #spr.frames .. ")" }))
  return
end
local RX, RY, RW, RH = nil, nil, nil, nil
if R then RX, RY, RW, RH = R[1], R[2], R[3], R[4] end

-- per-layer scan (the measured frame only)
local layers = {}
local layerSum = 0
local LX0, LY0, LX1, LY1 = nil, nil, nil, nil
for li = 1, #spr.layers do
  local layer = spr.layers[li]
  if not layer.isGroup then
    local cel = layer:cel(FN)
    local lp, x0, y0, x1, y1 = 0, nil, nil, nil, nil
    if cel then lp, x0, y0, x1, y1 = __scan_cel(spr, cel, RX, RY, RW, RH) end
    layers[#layers + 1] = {
      name = layer.name,
      pixels = lp,
      bbox = __bbox(x0, y0, x1, y1),
      visible = layer.isVisible,
      opacity = layer.opacity,
    }
    layerSum = layerSum + lp
    if x0 then
      if not LX0 or x0 < LX0 then LX0 = x0 end
      if not LY0 or y0 < LY0 then LY0 = y0 end
      if not LX1 or x1 > LX1 then LX1 = x1 end
      if not LY1 or y1 > LY1 then LY1 = y1 end
    end
  end
end

local comp, basis = __composite(spr, FN, W, H)

-- palette membership (measured from the composite)
local pal = spr.palettes[1]
local palSize = 0
local palset = {}
if pal then
  palSize = #pal
  for i = 0, palSize - 1 do
    local c = pal:getColor(i)
    palset[c.red * 65536 + c.green * 256 + c.blue] = true
  end
end

local hist = {}
for i = 1, 1001 do hist[i] = 0 end
local colorHist = {}
local n, Lsum, satSum, nlt20, ngt60, alphaNonBinary, oop = 0, 0, 0, 0, 0, 0, 0
local sx0, sy0, sx1, sy1 = 0, 0, W, H
if R then
  sx0 = math.max(0, RX)
  sy0 = math.max(0, RY)
  sx1 = math.min(W, RX + RW)
  sy1 = math.min(H, RY + RH)
end
local BX0, BY0, BX1, BY1 = nil, nil, nil, nil
for y = sy0, sy1 - 1 do
  for x = sx0, sx1 - 1 do
    local px = comp:getPixel(x, y)
    local a, r, g, b = __px(spr, px, pal)
    if a > 0 then
      n = n + 1
      local key = r * 65536 + g * 256 + b
      colorHist[key] = (colorHist[key] or 0) + 1
      if a < 255 then alphaNonBinary = alphaNonBinary + 1 end
      if palSize > 0 and not palset[key] then oop = oop + 1 end
      local L = __lstar(r, g, b)
      Lsum = Lsum + L
      local bin = math.floor(L * 10) + 1
      if bin < 1 then bin = 1 elseif bin > 1001 then bin = 1001 end
      hist[bin] = hist[bin] + 1
      if L < 20 then nlt20 = nlt20 + 1 end
      if L > 60 then ngt60 = ngt60 + 1 end
      local _, s = __hsv(r, g, b)
      satSum = satSum + s
      if not BX0 or x < BX0 then BX0 = x end
      if not BY0 or y < BY0 then BY0 = y end
      if not BX1 or x > BX1 then BX1 = x end
      if not BY1 or y > BY1 then BY1 = y end
    end
  end
end

local arr = {}
for key, cnt in pairs(colorHist) do arr[#arr + 1] = { key = key, n = cnt } end
table.sort(arr, function(p, q)
  if p.n == q.n then return p.key < q.key end
  return p.n > q.n
end)

local top = {}
for i = 1, math.min(TOPN, #arr) do
  local key = arr[i].key
  local r = math.floor(key / 65536) % 256
  local g = math.floor(key / 256) % 256
  local b = key % 256
  local h, s = __hsv(r, g, b)
  top[#top + 1] = {
    hex = __hex(r, g, b),
    n = arr[i].n,
    share = __round(arr[i].n / n),
    L = __round(__lstar(r, g, b)),
    sat = __round(s),
    hue = __round(h),
  }
end

local tone = nil
if n > 0 then
  tone = {
    L_mean = __round(Lsum / n),
    L_p50 = __round(__pct(hist, n, 0.50)),
    L_p95 = __round(__pct(hist, n, 0.95)),
    share_L_lt20 = __round(nlt20 / n),
    share_L_gt60 = __round(ngt60 / n),
    sat_mean = __round(satSum / n),
  }
end

io.write("__RESULT__" .. json.encode({ success = true, data = {
  canvas = { w = W, h = H },
  colorMode = ({ "rgb", "grayscale", "indexed" })[spr.colorMode + 1] or tostring(spr.colorMode),
  frame = FN,
  frame_count = #spr.frames,
  region = R and { x = RX, y = RY, w = RW, h = RH } or nil,
  content_bbox = __bbox(LX0, LY0, LX1, LY1),
  measured_bbox = __bbox(BX0, BY0, BX1, BY1),
  measured_pixels = n,
  layer_pixels = layerSum,
  layers = layers,
  colors = {
    unique = #arr,
    palette_size = palSize,
    out_of_palette = (palSize > 0) and oop or nil,
    out_of_palette_share = (palSize > 0 and n > 0) and __round(oop / n) or nil,
    alpha_non_binary = alphaNonBinary,
    top = top,
  },
  tone = tone,
  stats_basis = basis,
  layer_scan_basis = "frame_only"
}}))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(`measure_sprite: ${result.error ?? "failed"}`);
        const data = (result.data ?? {});
        return this.ok({ ...data, elapsed_ms: Date.now() - t0 });
    }
    // -- measure_layers (farm02 round 2 / F.4) --------------------------------
    //
    // Semantics were taken from the scripts this replaces, not invented:
    //   components4 / comp_detail / thick2x2 / bbox / lowest row / cyclic frame diff
    //   == projects/farm02/tools/build-action-water.lua and build-action-hoe.lua.
    // Those four to six hand-written copies per round are exactly why the gate moved here.
    async handleMeasureLayers(args) {
        const filePath = this.requireParam(args, "filePath");
        const t0 = Date.now();
        const rawFrames = args.frames;
        const frameList = [];
        if (rawFrames !== undefined && rawFrames !== null) {
            if (!Array.isArray(rawFrames))
                return this.error("frames must be an array of 1-indexed frame numbers");
            for (const v of rawFrames) {
                const n = Number(v);
                if (!Number.isInteger(n) || n < 1) {
                    return this.error(`frames must be positive 1-indexed integers, got ${JSON.stringify(v)}`);
                }
                frameList.push(n);
            }
            if (frameList.length === 0)
                return this.error("frames must not be empty (omit it to measure every frame)");
        }
        const rawLayers = args.layers;
        const layerList = [];
        if (rawLayers !== undefined && rawLayers !== null) {
            if (!Array.isArray(rawLayers))
                return this.error("layers must be an array of layer names");
            for (const v of rawLayers) {
                if (typeof v !== "string" || !v)
                    return this.error(`layers entries must be non-empty strings, got ${JSON.stringify(v)}`);
                layerList.push(v);
            }
            if (layerList.length === 0)
                return this.error("layers must not be empty (omit it to measure every non-group layer)");
        }
        const diffCycle = this.boolParam(args, "diffCycle", true) === true;
        const hasOutline = args.outlineColor !== undefined && args.outlineColor !== null && args.outlineColor !== "";
        const ref = hasOutline ? this.luaColorRef(args.outlineColor) : null;
        const script = `
${this.p0CoreLua()}
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local W, H, NF = spr.width, spr.height, #spr.frames
local indexed = (spr.colorMode == ColorMode.INDEXED)
local DIFF_CYCLE = ${diffCycle ? "true" : "false"}

-- 输入存在性断言（开工检查表第 1 条）：要量的层名/帧号必须真的存在，不接受静默跳过。
local wantFrames = ${`{${frameList.join(",")}}`}
local wantLayers = ${layerList.length ? `{${layerList.map((n) => `"${luaEscape(n)}"`).join(",")}}` : "nil"}
local frames = {}
if #wantFrames == 0 then
  for i = 1, NF do frames[#frames + 1] = i end
else
  for _, f in ipairs(wantFrames) do
    if f > NF then
      io.write("__RESULT__" .. json.encode({ success = false, error = "frame " .. f .. " out of range (sprite has " .. NF .. " frames)" }))
      return
    end
    frames[#frames + 1] = f
  end
end

local sel = {}
if wantLayers then
  for _, nm in ipairs(wantLayers) do
    local found = false
    for li = 1, #spr.layers do
      if spr.layers[li].name == nm then found = true break end
    end
    if not found then
      local avail = {}
      for li = 1, #spr.layers do avail[#avail + 1] = spr.layers[li].name end
      io.write("__RESULT__" .. json.encode({ success = false, error = "no such layer: " .. nm .. " (available: " .. table.concat(avail, ", ") .. ")" }))
      return
    end
    sel[nm] = true
  end
end
for li = 1, #spr.layers do
  local layer = spr.layers[li]
  if not layer.isGroup and not wantLayers then sel[layer.name] = true end
end

-- 模式感知的 alpha：索引图里 rgbaA(index) 是无意义的字节，透明只能看 transparentColor。
local function alphaOf(v)
  if indexed then
    if v == spr.transparentColor then return 0 end
    return 255
  end
  return app.pixelColor.rgbaA(v)
end

-- F.1 同款判定：thick2x2 是"某颜色铺满 2x2"，所以必须先能认出该颜色。
local OUTLINE = ${hasOutline ? "true" : "false"}
local MATCH = "${ref?.kind ?? "rgb"}"
local matchIndex = ${ref?.index ?? -1}
local matchR, matchG, matchB = ${ref?.r ?? -1}, ${ref?.g ?? -1}, ${ref?.b ?? -1}
local outlineIdentifiable = false
if OUTLINE then
  if MATCH == "index" then
    if indexed then outlineIdentifiable = true end
  elseif matchR >= 0 then
    if indexed then
      local pal = spr.palettes[1]
      for i = 0, #pal - 1 do
        local c = pal:getColor(i)
        if c.red == matchR and c.green == matchG and c.blue == matchB then
          matchIndex = i
          outlineIdentifiable = true
          break
        end
      end
    else
      outlineIdentifiable = true
    end
  end
end
local function isOutlinePx(v)
  if not outlineIdentifiable then return false end
  if indexed then return v == matchIndex end
  if app.pixelColor.rgbaA(v) == 0 then return false end
  return app.pixelColor.rgbaR(v) == matchR
     and app.pixelColor.rgbaG(v) == matchG
     and app.pixelColor.rgbaB(v) == matchB
end

-- 同一帧的两个像素算不算"变了"：覆盖不同，或双方都不透明而 RGB 不同（与 diff_sprites 同规则）。
local function samePixel(pa, pb)
  local aa, ab = alphaOf(pa), alphaOf(pb)
  if (aa > 0) ~= (ab > 0) then return false end
  if aa == 0 then return true end
  if indexed then return pa == pb end
  return app.pixelColor.rgbaR(pa) == app.pixelColor.rgbaR(pb)
     and app.pixelColor.rgbaG(pa) == app.pixelColor.rgbaG(pb)
     and app.pixelColor.rgbaB(pa) == app.pixelColor.rgbaB(pb)
end

local function gridOf(fi)
  local comp = __composite(spr, fi, W, H)
  local g = {}
  for y = 0, H - 1 do
    for x = 0, W - 1 do g[y * W + x + 1] = comp:getPixel(x, y) end
  end
  return g
end

local rows = {}
local firstGrid, lastGrid, prevGrid = nil, nil, nil
local firstFrame, lastFrame = frames[1], frames[#frames]
for _, fi in ipairs(frames) do
  -- 每层像素数：读该帧自己的 cel（count 与"看得见的"不是一回事，二者都报）
  local layerPixels = {}
  local layerTotal = 0
  for li = 1, #spr.layers do
    local layer = spr.layers[li]
    if not layer.isGroup and sel[layer.name] then
      local cel = layer:cel(fi)
      local n = 0
      if cel then n = __scan_cel(spr, cel) end
      layerPixels[layer.name] = n
      layerTotal = layerTotal + n
    end
  end

  local g = gridOf(fi)
  if fi == firstFrame then firstGrid = g end
  lastGrid = g

  local n, minx, miny, maxx, maxy = 0, nil, nil, nil, nil
  local alphaSum, weighted = 0, 0
  local opaque4 = {}
  for y = 0, H - 1 do
    for x = 0, W - 1 do
      local vv = g[y * W + x + 1]
      local a = alphaOf(vv)
      if a > 0 then
        n = n + 1
        alphaSum = alphaSum + a
        weighted = weighted + a * (1 + (x % 97) + 7 * (y % 89) + 13 * fi)
        opaque4[y * W + x] = true
        if not minx or x < minx then minx = x end
        if not miny or y < miny then miny = y end
        if not maxx or x > maxx then maxx = x end
        if not maxy or y > maxy then maxy = y end
      end
    end
  end

  local seen, comps, compList = {}, 0, {}
  for key in pairs(opaque4) do
    if not seen[key] then
      comps = comps + 1
      local stack = { key }
      seen[key] = true
      local size, x0, y0, x1, y1 = 0, nil, nil, nil, nil
      while #stack > 0 do
        local cur = table.remove(stack)
        local cy = math.floor(cur / W)
        local cx = cur - cy * W
        size = size + 1
        if not x0 or cx < x0 then x0 = cx end
        if not y0 or cy < y0 then y0 = cy end
        if not x1 or cx > x1 then x1 = cx end
        if not y1 or cy > y1 then y1 = cy end
        if cx > 0 and opaque4[cur - 1] and not seen[cur - 1] then seen[cur - 1] = true; stack[#stack + 1] = cur - 1 end
        if cx < W - 1 and opaque4[cur + 1] and not seen[cur + 1] then seen[cur + 1] = true; stack[#stack + 1] = cur + 1 end
        if cy > 0 and opaque4[cur - W] and not seen[cur - W] then seen[cur - W] = true; stack[#stack + 1] = cur - W end
        if cy < H - 1 and opaque4[cur + W] and not seen[cur + W] then seen[cur + W] = true; stack[#stack + 1] = cur + W end
      end
      if comps > 1 or size < n then
        compList[#compList + 1] = { n = size, x0 = x0, y0 = y0, x1 = x1, y1 = y1 }
      end
    end
  end
  -- pairs() 顺序不定 ⇒ 排序，否则同一份产物的 comp_detail 每次都不一样（不可复跑的判据不是判据）。
  table.sort(compList, function(p, q)
    if p.n ~= q.n then return p.n > q.n end
    if p.y0 ~= q.y0 then return p.y0 < q.y0 end
    return p.x0 < q.x0
  end)
  local parts = {}
  for _, c in ipairs(compList) do
    parts[#parts + 1] = string.format("n=%d(%d,%d..%d,%d)", c.n, c.x0, c.y0, c.x1, c.y1)
  end

  local solid2x2, thick2x2 = 0, 0
  for y = 0, H - 2 do
    for x = 0, W - 2 do
      local p1 = g[y * W + x + 1]
      local p2 = g[y * W + x + 2]
      local p3 = g[(y + 1) * W + x + 1]
      local p4 = g[(y + 1) * W + x + 2]
      if alphaOf(p1) > 0 and alphaOf(p2) > 0 and alphaOf(p3) > 0 and alphaOf(p4) > 0 then
        if p1 == p2 and p1 == p3 and p1 == p4 then solid2x2 = solid2x2 + 1 end
        if OUTLINE and outlineIdentifiable
           and isOutlinePx(p1) and isOutlinePx(p2) and isOutlinePx(p3) and isOutlinePx(p4) then
          thick2x2 = thick2x2 + 1
        end
      end
    end
  end

  local changed = nil
  local prevFrame = nil
  if prevGrid then
    changed = 0
    for i = 1, W * H do
      if not samePixel(g[i], prevGrid[i]) then changed = changed + 1 end
    end
    prevFrame = fi - 1
  end

  rows[#rows + 1] = {
    frame = fi,
    bbox = __bbox(minx, miny, maxx, maxy),
    lowest_row = maxy,
    opaque_px = n,
    layer_total_px = layerTotal,
    layer_pixels = layerPixels,
    components4 = comps,
    comp_detail = (#parts == 0) and "" or table.concat(parts, " "),
    components_outside_main = #compList,
    solid2x2_blocks = solid2x2,
    thick2x2_blocks = (OUTLINE and outlineIdentifiable) and thick2x2 or nil,
    alpha_fingerprint = { opaque = n, alpha_sum = alphaSum, weighted = weighted },
    changed_vs_prev = changed,
    prev_frame = prevFrame,
  }
  prevGrid = g
end

-- 首帧没有"上一帧"：按 farm02 的循环约定拿最后一帧比（diffCycle）。
if DIFF_CYCLE and #rows > 1 and rows[1].changed_vs_prev == nil and firstGrid and lastGrid then
  local changed = 0
  for i = 1, W * H do
    if not samePixel(firstGrid[i], lastGrid[i]) then changed = changed + 1 end
  end
  rows[1].changed_vs_prev = changed
  rows[1].prev_frame = lastFrame
end
-- 自证：循环比较到底有没有跑（未定义全局/条件写错都会静默退化成"首帧没有改动量"）。
local diffCycleApplied = (rows[1].changed_vs_prev ~= nil)

local layerNames = {}
for nm in pairs(sel) do layerNames[#layerNames + 1] = nm end
table.sort(layerNames)

io.write("__RESULT__" .. json.encode({ success = true, data = {
  canvas = { w = W, h = H },
  colorMode = ({ "rgb", "grayscale", "indexed" })[spr.colorMode + 1] or tostring(spr.colorMode),
  frame_count = NF,
  frames_measured = frames,
  layers_measured = layerNames,
  outline = OUTLINE and {
    given = true,
    identifiable = outlineIdentifiable,
    match = MATCH,
    index = (MATCH == "index" or indexed) and matchIndex or nil,
    hex = (not indexed and matchR >= 0) and __hex(matchR, matchG, matchB) or nil,
  } or nil,
  diff_basis = "coverage, then rgb when opaque on both sides (same rule as diff_sprites)",
  diff_cycle_requested = DIFF_CYCLE,
  diff_cycle_applied = diffCycleApplied,
  frames = rows,
}}))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(`measure_layers: ${result.error ?? "failed"}`);
        const data = (result.data ?? {});
        return this.ok({ ...data, elapsed_ms: Date.now() - t0 });
    }
    // -- derive_mirror (farm02 round 2 / F.5) ---------------------------------
    async handleDeriveMirror(args) {
        const src = this.requireParam(args, "src");
        const dst = this.requireParam(args, "dst");
        if (!existsSync(src))
            return this.error(`derive_mirror: src not found: ${src}`);
        const axis = (this.optParam(args, "axis") ?? "horizontal").toLowerCase();
        if (axis !== "horizontal" && axis !== "vertical") {
            return this.error(`derive_mirror: axis must be horizontal or vertical, got ${JSON.stringify(axis)}`);
        }
        const t0 = Date.now();
        const frameList = [];
        if (args.frames !== undefined && args.frames !== null) {
            if (!Array.isArray(args.frames))
                return this.error("frames must be an array of 1-indexed frame numbers");
            for (const v of args.frames) {
                const n = Number(v);
                if (!Number.isInteger(n) || n < 1)
                    return this.error(`frames must be positive integers, got ${JSON.stringify(v)}`);
                frameList.push(n);
            }
            if (!frameList.length)
                return this.error("frames must not be empty (omit it to mirror every frame)");
        }
        const layerList = [];
        if (args.layers !== undefined && args.layers !== null) {
            if (!Array.isArray(args.layers))
                return this.error("layers must be an array of layer names");
            for (const v of args.layers) {
                if (typeof v !== "string" || !v)
                    return this.error(`layers entries must be non-empty strings, got ${JSON.stringify(v)}`);
                layerList.push(v);
            }
            if (!layerList.length)
                return this.error("layers must not be empty (omit it to mirror every non-group layer)");
        }
        const patches = [];
        if (args.patch !== undefined && args.patch !== null) {
            if (!Array.isArray(args.patch))
                return this.error("patch must be an array of rects");
            for (const raw of args.patch) {
                const o = (raw ?? {});
                const num = (k) => {
                    const v = Number(o[k]);
                    if (!Number.isInteger(v) || v < 0)
                        throw new Error(`patch[].${k} must be a non-negative integer, got ${JSON.stringify(o[k])}`);
                    return v;
                };
                let x0;
                let y0;
                let x1;
                let y1;
                let fromSprite;
                try {
                    x0 = num("x0");
                    y0 = num("y0");
                    x1 = num("x1");
                    y1 = num("y1");
                    fromSprite = typeof o.fromSprite === "string" ? o.fromSprite : "";
                }
                catch (err) {
                    return this.error(err.message);
                }
                if (!fromSprite)
                    return this.error("patch[].fromSprite is required");
                if (!existsSync(fromSprite))
                    return this.error(`derive_mirror: patch fromSprite not found: ${fromSprite}`);
                if (x1 < x0 || y1 < y0) {
                    return this.error(`patch rect must have x1>=x0 and y1>=y0 (inclusive), got x0=${x0},x1=${x1},y0=${y0},y1=${y1}`);
                }
                patches.push({
                    x0,
                    y0,
                    x1,
                    y1,
                    fromSprite,
                    frame: Number.isInteger(Number(o.frame)) && Number(o.frame) >= 1 ? Number(o.frame) : 1,
                    layer: typeof o.layer === "string" ? o.layer : "",
                });
            }
        }
        const luaPatches = patches
            .map((p) => `  { x0 = ${p.x0}, y0 = ${p.y0}, x1 = ${p.x1}, y1 = ${p.y1}, from = "${luaPath(p.fromSprite)}", frame = ${p.frame}, layer = ${p.layer ? `"${luaEscape(p.layer)}"` : "nil"} },`)
            .join("\n");
        const script = `
${this.p0CoreLua()}
local src = app.open("${luaPath(src)}")
if not src then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open src" }))
  return
end
local W, H, NF = src.width, src.height, #src.frames
local indexed = (src.colorMode == ColorMode.INDEXED)
local HORIZONTAL = ${axis === "horizontal" ? "true" : "false"}

local function alphaOf(v)
  if indexed then
    if v == src.transparentColor then return 0 end
    return 255
  end
  return app.pixelColor.rgbaA(v)
end

local function layerByName(spr, name)
  for li = 1, #spr.layers do
    if spr.layers[li].name == name then return spr.layers[li] end
  end
  return nil
end

-- 输入存在性断言：层/帧必须真的存在（静默跳过 = 假绿）
local wantFrames = ${`{${frameList.join(",")}}`}
local frames = {}
if #wantFrames == 0 then
  for i = 1, NF do frames[#frames + 1] = i end
else
  for _, f in ipairs(wantFrames) do
    if f > NF then
      io.write("__RESULT__" .. json.encode({ success = false, error = "frame " .. f .. " out of range (src has " .. NF .. " frames)" }))
      return
    end
    frames[#frames + 1] = f
  end
end

local wantLayers = ${layerList.length ? `{${layerList.map((n) => `"${luaEscape(n)}"`).join(",")}}` : "nil"}
local layerNames = {}
if wantLayers then
  for _, nm in ipairs(wantLayers) do
    if not layerByName(src, nm) then
      local avail = {}
      for li = 1, #src.layers do avail[#avail + 1] = src.layers[li].name end
      io.write("__RESULT__" .. json.encode({ success = false, error = "no such layer: " .. nm .. " (available: " .. table.concat(avail, ", ") .. ")" }))
      return
    end
    layerNames[#layerNames + 1] = nm
  end
else
  for li = 1, #src.layers do
    if not src.layers[li].isGroup then layerNames[#layerNames + 1] = src.layers[li].name end
  end
end

local patches = {
${luaPatches}
}

-- 画布级缓冲区：镜像在**精灵坐标**里做（像素 (x,y) -> (W-1-x,y)），
-- 而不是在 cel.image 自己的坐标系里翻（cel.position 不对称时那是错的）。
local function bufferOf(spr, layerName, fi)
  local buf = {}
  local layer = layerByName(spr, layerName)
  local cel = layer and layer:cel(fi)
  if cel then
    local img, b = cel.image, cel.bounds
    for y = 0, b.height - 1 do
      local wy = b.y + y
      if wy >= 0 and wy < H then
        for x = 0, b.width - 1 do
          local wx = b.x + x
          if wx >= 0 and wx < W then buf[wy * W + wx + 1] = img:getPixel(x, y) end
        end
      end
    end
  end
  return buf
end

-- 剪影只有在"镜像后不存在该格"时才当透明；RGB 的 0 / 索引图的 transparentColor 都是透明。
local function opaqueAt(buf, i)
  local v = buf[i]
  if v == nil then return false end
  return alphaOf(v) > 0
end

-- 期望值：原始快照 -> 镜像 -> 贴 patch（先镜像再贴，与写路径同序）
local function expectedFrom(orig)
  local exp = {}
  for y = 0, H - 1 do
    for x = 0, W - 1 do
      local sx = HORIZONTAL and (W - 1 - x) or x
      local sy = HORIZONTAL and y or (H - 1 - y)
      exp[y * W + x + 1] = orig[sy * W + sx + 1]
    end
  end
  return exp
end

-- 读一份 patch 源（同 src 时用快照，别去 open 正在被改的那个精灵）
local srcSnapshot = {}
local fromSprites = {}
local fatal = nil
local function patchPixels(p, layerName)
  if p.layer and p.layer ~= layerName then return nil end
  local key = p.from
  local buf = srcSnapshot[layerName]
  if key ~= "${luaPath(src)}" then
    if not fromSprites[key] then
      local s = app.open(key)
      if not s then
        fatal = "Failed to open patch source: " .. key
        return nil
      end
      fromSprites[key] = s
    end
    local s = fromSprites[key]
    if not layerByName(s, layerName) then
      fatal = "patch source " .. key .. " has no layer '" .. layerName .. "' (patch without an explicit layer applies to every selected layer)"
      return nil
    end
    if p.frame > #s.frames then
      fatal = "patch frame " .. p.frame .. " out of range for " .. key
      return nil
    end
    return bufferOf(s, layerName, p.frame)
  end
  return buf
end

-- 1) 快照（在**任何**写入之前）：写路径与期望值都以它为起点
for _, nm in ipairs(layerNames) do
  srcSnapshot[nm] = {}
  for _, fi in ipairs(frames) do
    srcSnapshot[nm][fi] = bufferOf(src, nm, fi)
  end
end

-- 2) 期望值（与写路径分开算）
local patchOpaque = 0
local expected = {}
for _, nm in ipairs(layerNames) do
  expected[nm] = {}
  for _, fi in ipairs(frames) do
    local exp = expectedFrom(srcSnapshot[nm][fi])
    for _, p in ipairs(patches) do
      local pbuf = patchPixels(p, nm)
      if pbuf then
        for y = p.y0, p.y1 do
          for x = p.x0, p.x1 do
            if x >= 0 and x < W and y >= 0 and y < H then
              local i = y * W + x + 1
              exp[i] = pbuf[i]
              if opaqueAt(pbuf, i) then patchOpaque = patchOpaque + 1 end
            end
          end
        end
      end
    end
    expected[nm][fi] = exp
  end
end
if fatal then
  -- 单条出口：patchPixels 里再写一次 __RESULT__ 会让结果文件出现两个标记，调用方只会看到
  -- "Failed to parse result JSON"，真错误被埋掉。
  io.write("__RESULT__" .. json.encode({ success = false, error = fatal }))
  return
end

-- 3) 写：按期望值重建每个 cel（紧致 bbox），这是唯一改精灵的地方
local rebuilt = 0
for _, nm in ipairs(layerNames) do
  local layer = layerByName(src, nm)
  for _, fi in ipairs(frames) do
    local exp = expected[nm][fi]
    local x0, y0, x1, y1 = nil, nil, nil, nil
    for y = 0, H - 1 do
      for x = 0, W - 1 do
        if opaqueAt(exp, y * W + x + 1) then
          if not x0 or x < x0 then x0 = x end
          if not y0 or y < y0 then y0 = y end
          if not x1 or x > x1 then x1 = x end
          if not y1 or y > y1 then y1 = y end
        end
      end
    end
    local cel = layer:cel(fi)
    if not x0 then
      if cel then src:deleteCel(cel) end
    else
      if not cel then cel = src:newCel(layer, fi) end
      local bw, bh = x1 - x0 + 1, y1 - y0 + 1
      local img = Image(bw, bh, src.colorMode)
      for y = y0, y1 do
        for x = x0, x1 do
          -- 缓冲区里没有值的格子是**透明**（新 Image 本来就是透明初始化）。
          -- 直接 putPixel(nil) 会写出 0xFF000000 黑块 —— 这正是本工具自查两条抓到的第一个真 bug。
          local v = exp[y * W + x + 1]
          if v ~= nil then img:putPixel(x - x0, y - y0, v) end
        end
      end
      cel.image = img
      cel.position = Point(x0, y0)
      rebuilt = rebuilt + 1
    end
  end
end

src:saveCopyAs("${luaPath(dst)}")

-- 4) 自检：重新从盘上打开 dst，与期望值逐像素比（期望值来自写入前的快照，故两条都能咬人）
local out = app.open("${luaPath(dst)}")
if not out then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to reopen dst for the self-check" }))
  return
end
if out.width ~= W or out.height ~= H then
  io.write("__RESULT__" .. json.encode({ success = false, error = "dst canvas changed: " .. out.width .. "x" .. out.height .. " vs " .. W .. "x" .. H }))
  return
end

local function inAnyPatch(x, y)
  for _, p in ipairs(patches) do
    if x >= p.x0 and x <= p.x1 and y >= p.y0 and y <= p.y1 then return true end
  end
  return false
end

local silhouetteMismatch, pixelMismatch = 0, 0
local silhouetteSample, pixelSample = {}, {}
local compared, patchRects = 0, #patches
local patchedPixels = 0
for _, nm in ipairs(layerNames) do
  for _, fi in ipairs(frames) do
    local exp = expected[nm][fi]
    local act = bufferOf(out, nm, fi)
    for y = 0, H - 1 do
      for x = 0, W - 1 do
        if inAnyPatch(x, y) then
          patchedPixels = patchedPixels + 1
        else
          compared = compared + 1
          local i = y * W + x + 1
          local ev, av = exp[i], act[i]
          if opaqueAt(exp, i) ~= opaqueAt(act, i) then
            silhouetteMismatch = silhouetteMismatch + 1
            if #silhouetteSample < 8 then silhouetteSample[#silhouetteSample + 1] = { x = x, y = y } end
          end
          local same
          if ev == nil and av == nil then
            same = true
          elseif opaqueAt(exp, i) and opaqueAt(act, i) and not indexed then
            same = app.pixelColor.rgbaR(ev) == app.pixelColor.rgbaR(av)
               and app.pixelColor.rgbaG(ev) == app.pixelColor.rgbaG(av)
               and app.pixelColor.rgbaB(ev) == app.pixelColor.rgbaB(av)
          else
            same = (ev or 0) == (av or 0)
          end
          if not same then
            pixelMismatch = pixelMismatch + 1
            if #pixelSample < 8 then pixelSample[#pixelSample + 1] = { x = x, y = y } end
          end
        end
      end
    end
  end
end

io.write("__RESULT__" .. json.encode({ success = true, data = {
  src = "${luaPath(src)}",
  dst = "${luaPath(dst)}",
  canvas = { w = W, h = H },
  axis = HORIZONTAL and "horizontal" or "vertical",
  frames_mirrored = frames,
  layers_mirrored = layerNames,
  cels_rebuilt = rebuilt,
  patch_count = patchRects,
  patched_pixels_per_layer_frame = patchedPixels,
  patch_opaque_pixels = patchOpaque,
  compared_pixels = compared,
  checks = {
    silhouette_match = { mismatches = silhouetteMismatch, sample = silhouetteSample },
    patched_only_change = { mismatches = pixelMismatch, sample = pixelSample },
  },
  checks_passed = (silhouetteMismatch == 0 and pixelMismatch == 0),
  self_report = (patchRects > 0 and patchOpaque == 0)
    and "patch rects were given but copied 0 opaque pixels - check fromSprite/layer/frame, the patch did nothing"
    or nil,
  check_basis = "expected = pristine pre-edit snapshot of src mirrored in sprite space then patched; actual = dst reopened from disk",
}}))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(`derive_mirror: ${result.error ?? "failed"}`);
        const data = (result.data ?? {});
        return this.ok({ ...data, elapsed_ms: Date.now() - t0 });
    }
    /**
     * R3 (live §6.3): a palette change must leave a trace. "文档表写皮肤 #a06a45 / 草帽 #96703c，实物板却是
     * #8e6f5a / #877358" was only discovered by eye - the board had been desaturated 40% and nothing recorded it.
     * Every palette-mutating tool reports the entries it actually changed; this appends one JSON line to
     * <artifact>.palette-history.jsonl (append-only, so a file's whole desaturation/swap history survives).
     * Never throws: losing the trace must not fail the edit, but it IS reported in the payload.
     */
    async recordPaletteHistory(opts) {
        if (!opts.changes.length)
            return null;
        const p = `${opts.file}.palette-history.jsonl`;
        const rec = {
            when: new Date().toISOString(),
            file: opts.file,
            tool: opts.tool,
            change_count: opts.changes.length,
            changes: opts.changes,
            note: opts.note ?? null,
        };
        try {
            await appendFile(p, JSON.stringify(rec) + "\n", "utf-8");
            return p;
        }
        catch (err) {
            logDebug(`palette history not written: ${err.message}`);
            return null;
        }
    }
    // -- swap_family_palette (live §6.3 / R1) ---------------------------------
    //
    // Batch-swap one material family's palette entries on INDEXED sheets, ZERO pixel change.
    // §6.4's hard constraint is encoded here: entries are set on EACH FILE'S OWN palette
    // (`pal:setColor`) instead of LoadPalette'ing one external board - the 24 v3 sheets have
    // palettes of 20 / 26 / 28 entries, so an external board simply does not line up.
    async handleSwapFamilyPalette(args) {
        const rawFiles = args.files;
        if (!Array.isArray(rawFiles) || rawFiles.length === 0) {
            return this.error("swap_family_palette: files must be a non-empty array of paths");
        }
        const files = rawFiles.map((f) => String(f));
        for (const f of files) {
            if (!existsSync(f))
                return this.error(`swap_family_palette: file not found: ${f}`);
        }
        // family indices: explicit, or resolved from a family table JSON
        let indices = [];
        if (Array.isArray(args.indices)) {
            indices = args.indices.map((v) => Number(v));
        }
        else if (args.family || args.familyTable) {
            const tablePath = this.optParam(args, "familyTable");
            const familyName = this.optParam(args, "family");
            if (!tablePath || !familyName) {
                return this.error("swap_family_palette: pass indices[], or BOTH family and familyTable");
            }
            if (!existsSync(tablePath))
                return this.error(`swap_family_palette: familyTable not found: ${tablePath}`);
            let table;
            try {
                table = JSON.parse(await readFile(tablePath, "utf-8"));
            }
            catch (err) {
                return this.error(`swap_family_palette: familyTable is not JSON: ${err.message}`);
            }
            const fams = (table.families ?? table);
            const entry = fams?.[familyName];
            const list = Array.isArray(entry) ? entry : entry?.indices;
            if (!Array.isArray(list)) {
                return this.error(`swap_family_palette: family "${familyName}" not found in ${tablePath} (expected families.<name> = [indices] or {indices:[...]})`);
            }
            indices = list.map((v) => Number(v));
        }
        if (indices.length === 0)
            return this.error("swap_family_palette: no family indices (pass indices[] or family+familyTable)");
        for (const i of indices) {
            if (!Number.isInteger(i) || i < 0)
                return this.error(`swap_family_palette: indices must be non-negative integers, got ${JSON.stringify(i)}`);
        }
        if (new Set(indices).size !== indices.length)
            return this.error("swap_family_palette: indices contain duplicates");
        const rawRamp = args.ramp ?? args.targetRamp;
        if (!Array.isArray(rawRamp) || rawRamp.length !== indices.length) {
            return this.error(`swap_family_palette: ramp must be an array of ${indices.length} #rrggbb (one per index, in index order)`);
        }
        const ramp = [];
        for (const c of rawRamp) {
            const rgb = this.parseHexRgb(c);
            if (!rgb)
                return this.error(`swap_family_palette: ramp entries must be #rrggbb, got ${JSON.stringify(c)}`);
            ramp.push(rgb);
        }
        const rawExpect = args.expectRamp ?? args.expect;
        let expect = null;
        if (rawExpect !== undefined && rawExpect !== null) {
            if (!Array.isArray(rawExpect) || rawExpect.length !== indices.length) {
                return this.error(`swap_family_palette: expectRamp must have ${indices.length} #rrggbb entries (current values, in index order)`);
            }
            expect = rawExpect.map((c) => {
                const rgb = this.parseHexRgb(c);
                return rgb ? rgb.hex : String(c);
            });
        }
        const outDir = this.optParam(args, "outDir");
        const inPlace = this.boolParam(args, "inPlace", false) === true;
        if (!outDir && !inPlace) {
            return this.error("swap_family_palette: refusing to guess where to write - pass outDir (source files stay untouched, e.g. assets_anim_blue/) or inPlace=true");
        }
        const toOut = (f) => {
            const base = f.replace(/\\/g, "/").split("/").pop() ?? f;
            return join(outDir, base);
        };
        const idxLua = `{${indices.join(", ")}}`;
        const rampLua = ramp.map((c) => `{ r = ${c.r}, g = ${c.g}, b = ${c.b} }`).join(", ");
        const expectLua = expect ? `{ ${expect.map((h) => `"${h}"`).join(", ")} }` : "nil";
        const prelude = this.p0CoreLua();
        // ---- pass 1: preconditions for EVERY file, before anything is written (all-or-nothing) ----
        const pre = await this.runLuaScript(`
${prelude}
local FILES = { ${files.map((f) => `"${luaPath(f)}"`).join(", ")} }
local IDX = ${idxLua}
local EXPECT = ${expectLua}
local maxIdx = 0
for _, i in ipairs(IDX) do if i > maxIdx then maxIdx = i end end
local out, bad = {}, 0
for _, f in ipairs(FILES) do
  local spr = app.open(f)
  if not spr then
    out[#out + 1] = { file = f, ok = false, error = "cannot open" }
    bad = bad + 1
  else
    local record = { file = f }
    local indexed = (spr.colorMode == ColorMode.INDEXED)
    local n = #spr.palettes[1]
    record.color_mode = ({ "rgb", "grayscale", "indexed" })[spr.colorMode + 1] or tostring(spr.colorMode)
    record.palette_size = n
    if not indexed then
      record.ok = false
      record.error = "not INDEXED: a family swap only changes palette entries, and an RGB sheet stores baked colours"
      bad = bad + 1
    elseif n <= maxIdx then
      -- 范围必须先判：否则 getColor(越界) 抛的是 Lua 越界错，调用方看不到"板长 20/26/28 分叉"这个真原因
      record.ok = false
      record.error = "palette has " .. n .. " entries but index " .. maxIdx .. " is required (the 20/26/28 board split)"
      bad = bad + 1
    else
      local cur = {}
      for k, i in ipairs(IDX) do
        local c = spr.palettes[1]:getColor(i)
        cur[k] = string.format("#%02x%02x%02x", c.red, c.green, c.blue)
      end
      record.current = cur
      if EXPECT then
        local why = nil
        for k, want in ipairs(EXPECT) do
          if cur[k] ~= want then why = "index " .. IDX[k] .. " is " .. cur[k] .. ", expected " .. want break end
        end
        if why then
          record.ok = false
          record.error = "pre-change assertion failed: " .. why
          bad = bad + 1
        else
          record.ok = true
        end
      else
        record.ok = true
      end
    end
    out[#out + 1] = record
    spr:close()
  end
end
io.write("__RESULT__" .. json.encode({ success = true, data = { files = out, bad = bad } }))
`);
        if (!pre.success)
            return this.error(`swap_family_palette (precheck): ${pre.error ?? "failed"}`);
        const preData = (pre.data ?? {});
        if ((preData.bad ?? 0) > 0) {
            const bad = (preData.files ?? []).filter((f) => f.ok === false);
            return this.error(`swap_family_palette: ${bad.length} file(s) failed the preconditions - NOTHING was written. ` +
                bad.map((f) => `${String(f.file).split(/[\\/]/).pop()}: ${String(f.error)}`).join(" | "));
        }
        // ---- pass 2: apply + verify each file on disk ----
        const results = [];
        let okCount = 0;
        for (const f of files) {
            const dst = outDir ? toOut(f) : f;
            if (outDir)
                await mkdir(outDir, { recursive: true });
            const r = await this.runLuaScript(`
${prelude}
local SRC = "${luaPath(f)}"
local OUT = "${luaPath(dst)}"
local IDX = ${idxLua}
local RAMP = { ${rampLua} }
local spr = app.open(SRC)
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "cannot open " .. SRC }))
  return
end
local pal = spr.palettes[1]
local palBefore = __pal_snapshot(pal)
local oldRgb = {}
for i = 0, #pal - 1 do
  local c = pal:getColor(i)
  oldRgb[i] = c.red * 65536 + c.green * 256 + c.blue
end
local hBefore, totBefore = __index_hist(spr)
local famset = {}
for k, i in ipairs(IDX) do famset[i] = true end
-- 只改族内条目（按**这件自己的板**改，不 LoadPalette 外部板）
for k, i in ipairs(IDX) do pal:setColor(i, Color(RAMP[k].r, RAMP[k].g, RAMP[k].b)) end
spr:saveCopyAs(OUT)
spr:close()

local chk = app.open(OUT)
if not chk then
  io.write("__RESULT__" .. json.encode({ success = false, error = "cannot reopen " .. OUT }))
  return
end
local newPal = chk.palettes[1]
local palAfter = __pal_snapshot(newPal)
local hAfter, totAfter = __index_hist(chk)

-- ① 索引指纹逐索引相等
local fpOk = (totBefore == totAfter)
if fpOk then
  for k, v in pairs(hBefore) do if hAfter[k] ~= v then fpOk = false break end end
  for k, v in pairs(hAfter) do if hBefore[k] ~= v then fpOk = false break end end
end
-- ② 板差异集合恰好 == IDX
local diff = __pal_changes(palBefore, palAfter)
local diffSet = {}
for _, c in ipairs(diff) do diffSet[c.index] = true end
local diffOk = (#diff == #IDX)
if diffOk then
  for _, i in ipairs(IDX) do if not diffSet[i] then diffOk = false break end end
  for i in pairs(diffSet) do
    local wanted = false
    for _, j in ipairs(IDX) do if j == i then wanted = true break end end
    if not wanted then diffOk = false break end
  end
end
-- ④ RGB 改动只允许落在族内索引的像素上
local inFamily, rgbChanged, outside = 0, 0, 0
local sample = {}
for _, c in ipairs(chk.cels) do
  local img, b = c.image, c.bounds
  for y = 0, b.height - 1 do
    for x = 0, b.width - 1 do
      local v = img:getPixel(x, y)
      if v ~= chk.transparentColor then
        if famset[v] then inFamily = inFamily + 1 end
        local nc = newPal:getColor(v)
        local newval = nc.red * 65536 + nc.green * 256 + nc.blue
        if newval ~= oldRgb[v] then
          rgbChanged = rgbChanged + 1
          if not famset[v] then
            outside = outside + 1
            if #sample < 8 then sample[#sample + 1] = { x = x + b.x, y = y + b.y, index = v } end
          end
        end
      end
    end
  end
end
local palSizeOk = (#palBefore == #palAfter)
chk:close()

io.write("__RESULT__" .. json.encode({ success = true, data = {
  src = SRC,
  out = OUT,
  palette_size = #palBefore,
  indices = IDX,
  ramp_before = (function() local t = {} for k, i in ipairs(IDX) do t[#t + 1] = palBefore[i + 1] end return t end)(),
  ramp_after = (function() local t = {} for k, i in ipairs(IDX) do t[#t + 1] = palAfter[i + 1] end return t end)(),
  palette_diff = diff,
  palette_size_unchanged = palSizeOk,
  index_fingerprint_ok = fpOk,
  palette_diff_matches_family = diffOk,
  opaque_before = totBefore,
  opaque_after = totAfter,
  pixels_in_family = inFamily,
  rgb_changed_pixels = rgbChanged,
  rgb_changed_outside_family = outside,
  outside_sample = sample,
  checks_ok = (fpOk and diffOk and palSizeOk and outside == 0),
  palette_changes = diff,
} }))
`);
            if (!r.success) {
                results.push({ file: f, out: dst, ok: false, error: r.error });
                continue;
            }
            const d = (r.data ?? {});
            const ok = d.checks_ok === true;
            if (ok)
                okCount += 1;
            if (this.boolParam(args, "recordHistory", true) !== false) {
                d.palette_history = await this.recordPaletteHistory({
                    file: dst,
                    tool: "swap_family_palette",
                    changes: (d.palette_changes ?? []),
                    note: `family ${JSON.stringify(indices)} -> ${ramp.map((c) => c.hex).join("/")}`,
                });
            }
            results.push({ file: f, ok, ...d });
        }
        const failed = results.filter((r) => r.ok !== true);
        const payload = {
            count: files.length,
            ok_count: okCount,
            failed_count: failed.length,
            all_ok: failed.length === 0,
            mode: outDir ? `outDir=${outDir}` : "inPlace",
            indices,
            ramp: ramp.map((c) => c.hex),
            expect_ramp: expect,
            zero_pixel_change: failed.length === 0,
            files: results,
        };
        if (okCount === 0) {
            return this.error(`swap_family_palette: every one of ${files.length} file(s) failed verification: ${JSON.stringify(failed.slice(0, 2))}`);
        }
        return this.ok(payload);
    }
    // -- family_palette_consistency (live §6.3 / R2) --------------------------
    //
    // "立绘是蓝的、动画帧是棕的" must become numbers: per family, the anchor hue and L* on both
    // sides, their difference, and how much of each artifact the family actually covers.
    // The comparison is made on the PALETTE COLOURS at the given indices - never on family names -
    // because the two sides legitimately name the same material differently (overalls vs denim).
    async handleFamilyPaletteConsistency(args) {
        const a = this.requireParam(args, "a");
        const b = this.requireParam(args, "b");
        if (!existsSync(a))
            return this.error(`family_palette_consistency: a not found: ${a}`);
        if (!existsSync(b))
            return this.error(`family_palette_consistency: b not found: ${b}`);
        const rawFamilies = args.families;
        let families = [];
        if (Array.isArray(rawFamilies)) {
            for (const f of rawFamilies) {
                const o = (f ?? {});
                const indices = Array.isArray(o.indices) ? o.indices.map((v) => Number(v)) : [];
                if (!indices.length)
                    return this.error(`family_palette_consistency: every families[] entry needs indices[] (got ${JSON.stringify(f)})`);
                families.push({ name: typeof o.name === "string" && o.name ? o.name : indices.join("/"), indices });
            }
        }
        else if (Array.isArray(args.indices)) {
            const indices = args.indices.map((v) => Number(v));
            const probe = (args.probeIndex !== undefined) ? this.numParam(args, "probeIndex") : indices[indices.length - 1];
            // 便利形式：只给 indices + 一个"代表索引"（默认最后一个），用来做"同族不同色"的对照
            families.push({ name: this.optParam(args, "family") ?? indices.join("/"), indices });
            if (probe === undefined)
                return this.error("family_palette_consistency: indices given but no probeIndex");
        }
        if (!families.length) {
            return this.error("family_palette_consistency: pass families:[{name, indices:[...]}] (or family+indices)");
        }
        const hueThreshold = this.numParam(args, "hueThreshold", 25);
        const lightnessThreshold = this.numParam(args, "lightnessThreshold");
        const readGpl = (p) => {
            const out = [];
            for (const line of readFileSync(p, "utf-8").split(/\r?\n/)) {
                const t = line.trim().split(/\s+/);
                if (t.length >= 3 && /^-?\d+$/.test(t[0]) && /^-?\d+$/.test(t[1]) && /^-?\d+$/.test(t[2])) {
                    const r = Number(t[0]);
                    const g = Number(t[1]);
                    const bl = Number(t[2]);
                    out.push({ hex: `#${[r, g, bl].map((v) => v.toString(16).padStart(2, "0")).join("")}`, r, g, b: bl });
                }
            }
            return out;
        };
        const loadSide = async (p) => {
            if (p.toLowerCase().endsWith(".gpl")) {
                if (!existsSync(p))
                    return { error: `not found: ${p}` };
                return { path: p, kind: "gpl", palette: readGpl(p) };
            }
            const r = await this.runLuaScript(`
${this.p0CoreLua()}
local spr = app.open("${luaPath(p)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local pal = spr.palettes[1]
local out = {}
for i = 0, #pal - 1 do
  local c = pal:getColor(i)
  out[#out + 1] = string.format("#%02x%02x%02x", c.red, c.green, c.blue)
end
local hist, opaque = {}, 0
local indexed = (spr.colorMode == ColorMode.INDEXED)
for _, c in ipairs(spr.cels) do
  local img, b = c.image, c.bounds
  for y = 0, b.height - 1 do
    for x = 0, b.width - 1 do
      local v = img:getPixel(x, y)
      local a = __px(spr, v, pal)
      if a > 0 then
        opaque = opaque + 1
        if indexed then hist[tostring(v)] = (hist[tostring(v)] or 0) + 1 end
      end
    end
  end
end
io.write("__RESULT__" .. json.encode({ success = true, data = {
  palette = out, histogram = hist, opaque = opaque,
  colorMode = ({ "rgb", "grayscale", "indexed" })[spr.colorMode + 1] or tostring(spr.colorMode) } }))
`);
            if (!r.success)
                return { error: r.error ?? "probe failed" };
            const d = (r.data ?? {});
            return {
                path: p,
                kind: "sprite",
                palette: (d.palette ?? []).map((hex) => ({ hex, r: parseInt(hex.slice(1, 3), 16), g: parseInt(hex.slice(3, 5), 16), b: parseInt(hex.slice(5, 7), 16) })),
                histogram: d.histogram ?? {},
                opaque: d.opaque ?? 0,
                colorMode: d.colorMode,
            };
        };
        const sa = await loadSide(a);
        const sb = await loadSide(b);
        for (const [label, s] of [["a", sa], ["b", sb]]) {
            if ("error" in s)
                return this.error(`family_palette_consistency: ${label}: ${s.error}`);
        }
        const A = sa;
        const B = sb;
        const results = families.map((fam) => {
            const pick = (s) => {
                const missing = fam.indices.filter((i) => i < 0 || i >= s.palette.length);
                return { missing, colors: fam.indices.filter((i) => i >= 0 && i < s.palette.length).map((i) => s.palette[i]) };
            };
            const pa = pick(A);
            const pb = pick(B);
            const stat = (colors) => {
                if (!colors.length)
                    return null;
                const hsv = colors.map((c) => rgb2hsv(c.r, c.g, c.b));
                const L = colors.map((c) => lstarCie(c.r, c.g, c.b));
                const sats = hsv.map((h) => h[1]);
                const sat = sats.reduce((x, y) => x + y, 0) / sats.length;
                const hues = hsv.map((h) => h[0]);
                return {
                    hexes: colors.map((c) => c.hex),
                    hues: hues.map((h) => Number(h.toFixed(1))),
                    lstar: L.map((v) => Number(v.toFixed(1))),
                    anchor_hue: Number(circularMeanHue(hues).toFixed(1)),
                    mean_lstar: Number((L.reduce((x, y) => x + y, 0) / L.length).toFixed(1)),
                    mean_sat: Number(sat.toFixed(2)),
                };
            };
            const ra = stat(pa.colors);
            const rb = stat(pb.colors);
            const share = (s) => {
                if (s.kind === "gpl" || !s.histogram)
                    return null;
                const total = s.opaque ?? 0;
                if (!total)
                    return null;
                let n = 0;
                for (const i of fam.indices)
                    n += s.histogram[String(i)] ?? 0;
                return { pixels: n, opaque: total, share: Number((n / total).toFixed(4)) };
            };
            const shA = share(A);
            const shB = share(B);
            const hueComparable = !!ra && !!rb && ra.mean_sat > 0.05 && rb.mean_sat > 0.05;
            const hueDelta = hueComparable ? Number(hueDistance(ra.anchor_hue, rb.anchor_hue).toFixed(1)) : null;
            // Per-colour nearest-match distances, the statistic portrait/color-compare.py reported ("162~167deg").
            // The anchor difference above is a DIFFERENT number (circular means, e.g. 175.7deg on that same pair),
            // so both are reported instead of letting one masquerade as the other.
            const pairwise = (ra && rb)
                ? ra.hues.map((h, i) => {
                    let best = 360;
                    for (const h2 of rb.hues)
                        best = Math.min(best, hueDistance(h, h2));
                    return { a_hex: ra.hexes[i], a_hue: h, nearest_b_delta: Number(best.toFixed(1)) };
                })
                : [];
            const pairwiseDeltas = pairwise.map((p) => p.nearest_b_delta);
            const lDelta = ra && rb ? Number(Math.abs(ra.mean_lstar - rb.mean_lstar).toFixed(1)) : null;
            const hueFail = hueComparable && hueDelta > hueThreshold;
            const lFail = lightnessThreshold !== undefined && lDelta !== null && lDelta > lightnessThreshold;
            return {
                family: fam.name,
                indices: fam.indices,
                a: { ...ra, pixel_share: shA, missing_indices: pa.missing },
                b: { ...rb, pixel_share: shB, missing_indices: pb.missing },
                hue_delta: hueDelta,
                pairwise_nearest_hue: pairwise,
                pairwise_hue_delta_range: pairwiseDeltas.length
                    ? [Math.min(...pairwiseDeltas), Math.max(...pairwiseDeltas)]
                    : null,
                lightness_delta: lDelta,
                hue_comparable: hueComparable,
                hue_threshold: hueThreshold,
                lightness_threshold: lightnessThreshold ?? null,
                verdict: hueFail || lFail ? "FAIL" : "PASS",
                why: hueFail
                    ? `anchor hue differs by ${hueDelta}deg > ${hueThreshold}deg - these read as DIFFERENT materials`
                    : lFail
                        ? `mean L* differs by ${lDelta} > ${lightnessThreshold}`
                        : hueComparable
                            ? `anchor hue differs by ${hueDelta}deg <= ${hueThreshold}deg`
                            : "hue not comparable (a near-neutral family on one side): judged on nothing, see mean_sat",
                note: (pa.missing.length || pb.missing.length)
                    ? `indices missing: a=${pa.missing.join(",") || "-"} b=${pb.missing.join(",") || "-"}`
                    : null,
            };
        });
        const failed = results.filter((r) => r.verdict === "FAIL");
        return this.ok({
            a: { path: A.path, kind: A.kind, colorMode: A.colorMode ?? null, palette_size: A.palette.length },
            b: { path: B.path, kind: B.kind, colorMode: B.colorMode ?? null, palette_size: B.palette.length },
            compared_by: "palette colours at the given indices (hue anchor + mean L*), NOT family names",
            hue_threshold: hueThreshold,
            families: results,
            failed_count: failed.length,
            verdict: failed.length ? "FAIL" : "PASS",
        });
    }
    // -- palette_spec_check (live §6.3 / R3b) ---------------------------------
    //
    // "CHAR-V2 §2 表写皮肤 #a06a45，实物板却是 #8e6f5a" was found by eye. This compares a spec table
    // against the artifact AND against the artifact's own palette history, so the answer can be
    // "the spec was right; v3 desaturated it afterwards, here is the record" instead of a mystery.
    async handlePaletteSpecCheck(args) {
        const file = this.requireParam(args, "file");
        if (!existsSync(file))
            return this.error(`palette_spec_check: file not found: ${file}`);
        let spec = null;
        const rawSpec = args.spec;
        const specPath = this.optParam(args, "specPath");
        if (rawSpec && typeof rawSpec === "object" && !Array.isArray(rawSpec)) {
            spec = rawSpec;
        }
        else if (specPath) {
            if (!existsSync(specPath))
                return this.error(`palette_spec_check: specPath not found: ${specPath}`);
            try {
                const parsed = JSON.parse(await readFile(specPath, "utf-8"));
                spec = (parsed.colors ?? parsed.entries ?? parsed);
            }
            catch (err) {
                return this.error(`palette_spec_check: specPath is not JSON: ${err.message}`);
            }
        }
        if (!spec) {
            return this.error('palette_spec_check: pass spec as {"2":"#a06a45", ...} (palette index -> expected #rrggbb) or specPath');
        }
        const hueTol = this.numParam(args, "hueTolerance");
        const lTol = this.numParam(args, "lightnessTolerance");
        // palette of the artifact (.gpl read here; a sprite is probed through Lua)
        let palette;
        let kind;
        if (file.toLowerCase().endsWith(".gpl")) {
            kind = "gpl";
            palette = [];
            for (const line of readFileSync(file, "utf-8").split(/\r?\n/)) {
                const t = line.trim().split(/\s+/);
                if (t.length >= 3 && /^-?\d+$/.test(t[0]) && /^-?\d+$/.test(t[1]) && /^-?\d+$/.test(t[2])) {
                    const r = Number(t[0]);
                    const g = Number(t[1]);
                    const b = Number(t[2]);
                    palette.push({ hex: `#${[r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("")}`, r, g, b });
                }
            }
        }
        else {
            kind = "sprite";
            const r = await this.runLuaScript(`
local spr = app.open("${luaPath(file)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local out = {}
for i = 0, #spr.palettes[1] - 1 do
  local c = spr.palettes[1]:getColor(i)
  out[#out + 1] = string.format("#%02x%02x%02x", c.red, c.green, c.blue)
end
io.write("__RESULT__" .. json.encode({ success = true, data = { palette = out,
  colorMode = ({ "rgb", "grayscale", "indexed" })[spr.colorMode + 1] or tostring(spr.colorMode) } }))
`);
            if (!r.success)
                return this.error(`palette_spec_check: ${r.error ?? "probe failed"}`);
            const hexes = (r.data ?? {}).palette ?? [];
            palette = hexes.map((hex) => ({ hex, r: parseInt(hex.slice(1, 3), 16), g: parseInt(hex.slice(3, 5), 16), b: parseInt(hex.slice(5, 7), 16) }));
        }
        const sat = (c) => {
            const mx = Math.max(c.r, c.g, c.b);
            const mn = Math.min(c.r, c.g, c.b);
            return mx === 0 ? 0 : (mx - mn) / mx;
        };
        const norm = (v) => {
            const rgb = this.parseHexRgb(v);
            return rgb ? rgb.hex : String(v).toLowerCase();
        };
        const entries = Object.entries(spec).map(([k, v]) => {
            const index = Number(k);
            const expected = norm(v);
            const actualC = palette[index];
            const actual = actualC ? actualC.hex : null;
            const expectedRgb = this.parseHexRgb(v);
            const match = actual !== null && expected !== null && actual === expected;
            let hueDelta = null;
            let lDelta = null;
            let satExpected = null;
            let satActual = null;
            if (expectedRgb && actualC) {
                hueDelta = Number(hueDistance(rgb2hsv(expectedRgb.r, expectedRgb.g, expectedRgb.b)[0], rgb2hsv(actualC.r, actualC.g, actualC.b)[0]).toFixed(1));
                lDelta = Number(Math.abs(lstarCie(expectedRgb.r, expectedRgb.g, expectedRgb.b) - lstarCie(actualC.r, actualC.g, actualC.b)).toFixed(1));
                satExpected = Number(sat(expectedRgb).toFixed(3));
                satActual = Number(sat(actualC).toFixed(3));
            }
            const tolFail = !match &&
                ((hueTol !== undefined && hueDelta !== null && hueDelta <= hueTol) ||
                    (lTol !== undefined && lDelta !== null && lDelta <= lTol));
            return {
                index,
                expected,
                actual,
                match,
                hue_delta: hueDelta,
                lightness_delta: lDelta,
                sat_expected: satExpected,
                sat_actual: satActual,
                sat_drop_pct: satExpected !== null && satActual !== null && satExpected > 0
                    ? Number((((satExpected - satActual) / satExpected) * 100).toFixed(1))
                    : null,
                tolerated: tolFail,
            };
        });
        const mismatches = entries.filter((e) => !e.match && !e.tolerated);
        // R3: use the artifact's own palette history to explain WHEN the spec stopped matching.
        const sidecar = `${file}.palette-history.jsonl`;
        let history = null;
        if (existsSync(sidecar) && this.boolParam(args, "checkHistory", true) !== false) {
            let recs = [];
            try {
                recs = readFileSync(sidecar, "utf-8")
                    .split(/\r?\n/)
                    .filter((l) => l.trim())
                    .map((l) => JSON.parse(l));
            }
            catch {
                recs = [];
            }
            // find the most recent record whose "before" state matches the spec (that is when the spec was still true)
            let explanation = null;
            for (let i = recs.length - 1; i >= 0 && !explanation; i -= 1) {
                const rec = recs[i];
                const changes = rec.changes ?? [];
                const matchesSpec = entries.every((e) => {
                    const ch = changes.find((c) => c.index === e.index);
                    const valueThen = ch ? ch.before : (palette[e.index]?.hex ?? null);
                    return valueThen === e.expected;
                });
                const touchesSpecd = entries.some((e) => changes.some((c) => c.index === e.index));
                if (matchesSpec && touchesSpecd) {
                    explanation = {
                        record_index: i,
                        when: rec.when ?? null,
                        tool: rec.tool ?? null,
                        why: "the spec matches the palette state recorded BEFORE this change - the spec is not wrong, " +
                            "the artifact was re-coloured afterwards (this is the desaturation/family-swap trace)",
                    };
                }
            }
            history = { sidecar, records: recs.length, explanation };
        }
        const payload = {
            file,
            kind,
            palette_size: palette.length,
            spec_size: entries.length,
            entries,
            mismatches: mismatches.map((m) => m.index),
            verdict: mismatches.length ? "FAIL" : "PASS",
            max_sat_drop_pct: mismatches.length
                ? Math.max(...mismatches.map((m) => m.sat_drop_pct ?? 0))
                : 0,
            history,
            note: mismatches.length
                ? history && history.explanation
                    ? "spec vs actual differ, AND the history explains it (see history.explanation)"
                    : "spec vs actual differ - no palette history record explains it (either it never matched, or the change was not recorded)"
                : "every spec entry matches the artifact",
        };
        return this.ok(payload);
    }
    // -- meter module (farm02 round 3 / C1) ----------------------------------
    // The same Lua the built-in meters run, exposed as a Lua module so an in-artifact script
    // (tools/_action4.lua, build-action-*.lua, ...) calls ONE implementation instead of the 7th
    // hand-written copy of it. `p0CoreLua()` defines the helpers as chunk-locals, so appending a
    // `return {...}` in the SAME text gives a module that sees exactly those locals - the tools and
    // the exported file can never drift apart, because there is only one string.
    meterModuleLua() {
        return `${this.p0CoreLua()}
-- farm02 round 3 / C1: same helpers the built-in meters use, as a module.
return {
  px = __px,
  index_hist = __index_hist,
  pal_snapshot = __pal_snapshot,
  pal_changes = __pal_changes,
  scan_cel = __scan_cel,
  scan_frame = __scan_frame,
  scan_all = __scan_all,
  alpha_fp = __alpha_fp,
  composite = __composite,
  bbox = __bbox,
  round = __round,
  lin = __lin,
  lstar = __lstar,
  hsv = __hsv,
  hex = __hex,
  pct = __pct,
}
`;
    }
    async handleExportMeterModule(args) {
        const outputPath = this.requireParam(args, "outputPath");
        const text = this.meterModuleLua();
        try {
            await mkdir(dirname(outputPath), { recursive: true });
            await writeFile(outputPath, text, "utf-8");
        }
        catch (err) {
            return this.error(`export_meter_module: could not write ${outputPath}: ${err.message}`);
        }
        const bytes = (await this.fileSize(outputPath)) ?? 0;
        if (bytes <= 0)
            return this.error(`export_meter_module: ${outputPath} is empty after writing`);
        return this.ok({
            path: outputPath,
            bytes,
            functions: ["px", "index_hist", "pal_snapshot", "pal_changes", "scan_cel", "scan_frame", "scan_all", "alpha_fp", "composite", "bbox", "round", "lin", "lstar", "hsv", "hex", "pct"],
            usage: `local M = dofile("${outputPath.replace(/\\/g, "/")}")  -- then M.px(spr, v, pal) / M.scan_cel(spr, cel) / M.composite(...)`,
            note: "Same source string the built-in meters embed, so an in-artifact check and the MCP meter cannot drift.",
        });
    }
    // -- remap_colors ---------------------------------------------------------
    async handleRemapColors(args) {
        const filePath = this.requireParam(args, "filePath");
        const rawMap = args.map ?? args.colorMap ?? args.color_map;
        if (rawMap === undefined || rawMap === null || typeof rawMap !== "object" || Array.isArray(rawMap)) {
            return this.error('remap_colors: map must be an object, e.g. {"#3b4a5a":"#546474"}');
        }
        const entries = [];
        const bad = [];
        for (const [k, v] of Object.entries(rawMap)) {
            const from = this.parseHexRgb(k);
            const to = this.parseHexRgb(v);
            if (!from || !to) {
                bad.push(`${k}->${String(v)}`);
                continue;
            }
            entries.push({ from, to });
        }
        if (bad.length) {
            return this.error(`remap_colors: keys and values must be #rrggbb (bad: ${bad.join(", ")})`);
        }
        if (entries.length === 0)
            return this.error("remap_colors: map is empty");
        const region = this.rectParam(args, "region");
        const layers = this.layerNamesParam(args, "layers");
        const frameNumber = this.numParam(args, "frameNumber");
        const savePath = this.optParam(args, "savePath") ?? filePath;
        const verifyAlpha = this.boolParam(args, "verifyAlpha", true);
        const mapLua = entries
            .map((e) => `  { fr = ${e.from.r}, fg = ${e.from.g}, fb = ${e.from.b}, tr = ${e.to.r}, tg = ${e.to.g}, tb = ${e.to.b}, hex = "${e.from.hex}", toHex = "${e.to.hex}" }`)
            .join(",\n");
        const layersLua = layers
            ? `{ ${layers.map((n) => `"${luaEscape(n)}"`).join(", ")} }`
            : "nil";
        const t0 = Date.now();
        const script = `
${this.p0CoreLua()}
local spr = app.open("${luaPath(filePath)}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open sprite" }))
  return
end
local MAP = {
${mapLua}
}
local lookup = {}
for i = 1, #MAP do
  lookup[MAP[i].fr * 65536 + MAP[i].fg * 256 + MAP[i].fb] = i
end

-- B1 (2026-10-16): INDEXED 稿上不能靠"把打包 RGB 写回像素"来改色 —— 索引图里每个像素只存一个
-- 调色板索引，putPixel 收的是**索引**。所以先把每个目标色解析成板内索引；解析不到就记 unmappable，
-- 而不是静默跳过（odie 之前的表现：changed=0，而 verifyAlpha 还据此自证"没变"，双假绿）。
local indexed = (spr.colorMode == ColorMode.INDEXED)
local pal = spr.palettes[1]
local unmappable = {}
local mapsToTransparent = {}
if indexed then
  local palSize = #pal
  for i = 1, #MAP do
    local m = MAP[i]
    local found = nil
    for k = 0, palSize - 1 do
      local c = pal:getColor(k)
      if c.red == m.tr and c.green == m.tg and c.blue == m.tb then found = k break end
    end
    if found == nil then
      unmappable[#unmappable + 1] = { from = m.hex, to = m.toHex }
    else
      MAP[i].tidx = found
      if found == spr.transparentColor then mapsToTransparent[#mapsToTransparent + 1] = m.toHex end
    end
  end
end
local R = ${this.luaRect(region)}
local RX, RY, RW, RH = nil, nil, nil, nil
if R then RX, RY, RW, RH = R[1], R[2], R[3], R[4] end
local WANT_LAYERS = ${layersLua}
local FN = ${frameNumber === undefined ? "nil" : String(frameNumber)}
local SAVE = "${luaPath(savePath)}"

local targets = {}
if WANT_LAYERS then
  for _, nm in ipairs(WANT_LAYERS) do
    local found = nil
    for li = 1, #spr.layers do
      if spr.layers[li].name == nm then found = spr.layers[li] break end
    end
    if not found then
      io.write("__RESULT__" .. json.encode({ success = false, error = "Layer not found: " .. nm }))
      return
    end
    targets[#targets + 1] = found
  end
else
  targets = spr.layers
end

local frames = spr.frames
if FN then
  if FN < 1 or FN > #spr.frames then
    io.write("__RESULT__" .. json.encode({ success = false, error = "frameNumber out of range (1.." .. #spr.frames .. ")" }))
    return
  end
  frames = { spr.frames[FN] }
end

-- fingerprint BEFORE the edit (memory)
local fpOpaque, fpSum, fpWeighted = __alpha_fp(spr, RX, RY, RW, RH)

local counts = {}
local total, changed, unmappablePixels = 0, 0, 0
local touchedLayers = {}
app.transaction(function()
  for _, layer in ipairs(targets) do
    if not layer.isGroup then
      touchedLayers[#touchedLayers + 1] = layer.name
      for _, fr in ipairs(frames) do
        local cel = layer:cel(fr.frameNumber)
        if cel then
          local b = cel.bounds
          local xi0, yi0, xi1, yi1 = 0, 0, b.width, b.height
          if R then
            xi0 = math.max(0, RX - b.x)
            yi0 = math.max(0, RY - b.y)
            xi1 = math.min(b.width, RX + RW - b.x)
            yi1 = math.min(b.height, RY + RH - b.y)
          end
          local img = cel.image:clone()
          for y = yi0, yi1 - 1 do
            for x = xi0, xi1 - 1 do
              local px = img:getPixel(x, y)
              -- 模式感知解码（B1）：INDEXED 下 rgbaA(索引) 是无意义字节 ⇒ 必须走 __px。
              local a, pr, pg, pb = __px(spr, px, pal)
              if a > 0 then
                total = total + 1
                local key = pr * 65536 + pg * 256 + pb
                local idx = lookup[key]
                if idx then
                  local m = MAP[idx]
                  if indexed then
                    if m.tidx then
                      -- INDEXED：写索引。不透明像素写非透明索引 ⇒ 剪影自动不变。
                      img:putPixel(x, y, m.tidx)
                      counts[idx] = (counts[idx] or 0) + 1
                      changed = changed + 1
                    else
                      counts[idx] = counts[idx] or 0
                      unmappablePixels = unmappablePixels + 1
                    end
                  else
                    -- RGB：alpha 从**源像素**带过来，绝不从 hex 字面量取 —— 这才保证剪影逐像素不变。
                    img:putPixel(x, y, app.pixelColor.rgba(m.tr, m.tg, m.tb, a))
                    counts[idx] = (counts[idx] or 0) + 1
                    changed = changed + 1
                  end
                end
              end
            end
          end
          cel.image = img
        end
      end
    end
  end
end)

spr:saveCopyAs(SAVE)

local perKey = {}
for i = 1, #MAP do
  local e = { to = MAP[i].toHex, count = counts[i] or 0 }
  if indexed then
    e.target_index = MAP[i].tidx
    e.unmappable = (MAP[i].tidx == nil) or nil
  end
  perKey[MAP[i].hex] = e
end

-- Independent check: re-open the SAVED file and re-measure the alpha fingerprint there.
local alphaUnchanged, outOfPaletteAfter, outOfPaletteShareAfter, alphaAfter = nil, nil, nil, nil
if ${verifyAlpha ? "true" : "false"} then
  local saved = app.open(SAVE)
  if saved then
    local o2, s2, w2 = __alpha_fp(saved, RX, RY, RW, RH)
    alphaAfter = { opaque = o2, alpha_sum = s2, weighted = w2 }
    alphaUnchanged = (o2 == fpOpaque and s2 == fpSum and w2 == fpWeighted)
    local pal = saved.palettes[1]
    local palSize = 0
    local palset = {}
    if pal then
      palSize = #pal
      for i = 0, palSize - 1 do
        local c = pal:getColor(i)
        palset[c.red * 65536 + c.green * 256 + c.blue] = true
      end
    end
    if palSize > 0 then
      local comp = __composite(saved, 1, saved.width, saved.height)
      local oop, cnt = 0, 0
      for y = 0, saved.height - 1 do
        for x = 0, saved.width - 1 do
          local px = comp:getPixel(x, y)
          -- B1：回读校验同样要走 __px，否则 INDEXED 下这条"独立校验"和被测代码一起瞎（假绿）。
          local a2, r2, g2, b2 = __px(saved, px, pal)
          if a2 > 0 then
            cnt = cnt + 1
            local key = r2 * 65536 + g2 * 256 + b2
            if not palset[key] then oop = oop + 1 end
          end
        end
      end
      outOfPaletteAfter = oop
      outOfPaletteShareAfter = (cnt > 0) and __round(oop / cnt) or nil
    end
  else
    alphaUnchanged = false
  end
end

-- 自报：changed=0 有两种完全不同的原因，必须能分辨（B1 的假绿就是第二种被当成"没东西可改"）。
local selfReport = nil
if changed == 0 and unmappablePixels > 0 then
  selfReport = "changed=0 while " .. unmappablePixels .. " pixel(s) matched a map key whose target colour is absent "
    .. "from this INDEXED palette - add the target colour to the palette (or map to an index); this is NOT 'nothing to change'"
elseif changed == 0 and total > 0 then
  selfReport = "changed=0: no scanned pixel colour matched any map key (opaque pixels scanned = " .. total .. ")"
end

io.write("__RESULT__" .. json.encode({ success = true, data = {
  changed = changed,
  total = total,
  perKey = perKey,
  colorMode = ({ "rgb", "grayscale", "indexed" })[spr.colorMode + 1] or tostring(spr.colorMode),
  alpha_basis = indexed
    and "index: a non-transparent palette index stays non-transparent (INDEXED has no per-pixel alpha)"
    or "pixel alpha carried over from the source pixel",
  unmappable = unmappable,
  unmappable_pixels = unmappablePixels,
  maps_to_transparent_color = mapsToTransparent,
  self_report = selfReport,
  alpha_unchanged = alphaUnchanged,
  alpha_before = { opaque = fpOpaque, alpha_sum = fpSum, weighted = fpWeighted },
  alpha_after = alphaAfter,
  out_of_palette_after = outOfPaletteAfter,
  out_of_palette_share_after = outOfPaletteShareAfter,
  alpha_verified_on_disk = ${verifyAlpha ? "true" : "false"},
  layers = touchedLayers,
  map_size = #MAP,
  savedTo = SAVE
}}))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(`remap_colors: ${result.error ?? "failed"}`);
        const data = (result.data ?? {});
        return this.ok({ ...data, elapsed_ms: Date.now() - t0 });
    }
    // -- diff_sprites ---------------------------------------------------------
    async handleDiffSprites(args) {
        const a = this.requireParam(args, "a");
        const b = this.requireParam(args, "b");
        const region = this.rectParam(args, "region");
        const includeAlpha = this.boolParam(args, "includeAlpha", true);
        const maskPng = this.optParam(args, "maskPng");
        const frameNumber = this.numParam(args, "frameNumber", 1);
        const t0 = Date.now();
        const script = `
${this.p0CoreLua()}
local A = app.open("${luaPath(a)}")
if not A then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open a: ${luaEscape(a)}" }))
  return
end
local B = app.open("${luaPath(b)}")
if not B then
  io.write("__RESULT__" .. json.encode({ success = false, error = "Failed to open b: ${luaEscape(b)}" }))
  return
end
if A.width ~= B.width or A.height ~= B.height then
  io.write("__RESULT__" .. json.encode({ success = false, error =
    "Size mismatch: a=" .. A.width .. "x" .. A.height .. " b=" .. B.width .. "x" .. B.height }))
  return
end
local W, H = A.width, A.height
local FN = ${frameNumber}
if FN < 1 or FN > #A.frames or FN > #B.frames then
  io.write("__RESULT__" .. json.encode({ success = false, error = "frameNumber out of range" }))
  return
end

local ca, basisA = __composite(A, FN, W, H)
local cb, basisB = __composite(B, FN, W, H)
-- INDEXED composites hold palette INDICES: decode through each sprite's OWN palette,
-- otherwise both sides read "transparent" and every pair reports changed = 0.
local palA = A.palettes[1]
local palB = B.palettes[1]

local R = ${this.luaRect(region)}
local RX, RY, RW, RH = nil, nil, nil, nil
if R then RX, RY, RW, RH = R[1], R[2], R[3], R[4] end
local INCLUDE_ALPHA = ${includeAlpha ? "true" : "false"}
local MASK = ${maskPng ? `"${luaPath(maskPng)}"` : "nil"}
local mask = nil
if MASK then mask = Image(W, H, ColorMode.RGB) end   -- RGB: an INDEXED mask would store the packed red as a garbage index

local total, changed, changedIn, changedOut, alphaChanged = 0, 0, 0, 0, 0
-- B2 (2026-10-16): 位置加权的 alpha 指纹 + 逐边不透明数。changed==0 时它们必须自洽，
-- 否则就是"量尺在骗人"（INDEXED 盲区曾让"锄地 vs 浇水"报 changed=0）。
local opaqueA, opaqueB, fpA, fpB = 0, 0, 0, 0
local X0, Y0, X1, Y1 = nil, nil, nil, nil
for y = 0, H - 1 do
  for x = 0, W - 1 do
    total = total + 1
    local pa, pb = ca:getPixel(x, y), cb:getPixel(x, y)
    local aa, ar, ag, ab = __px(A, pa, palA)
    local ba, br, bg, bb = __px(B, pb, palB)
    local wgt = 1 + (x % 97) + 7 * (y % 89)
    if aa > 0 then opaqueA = opaqueA + 1; fpA = fpA + aa * wgt end
    if ba > 0 then opaqueB = opaqueB + 1; fpB = fpB + ba * wgt end
    if aa ~= ba then alphaChanged = alphaChanged + 1 end
    local oa, ob = aa > 0, ba > 0
    local diff = false
    if oa ~= ob then
      diff = true
    elseif oa and ob then
      local sameRgb = (ar == br) and (ag == bg) and (ab == bb)
      if not sameRgb then diff = true end
      if INCLUDE_ALPHA and aa ~= ba then diff = true end
    end
    if diff then
      changed = changed + 1
      if (not R) or (x >= RX and x < RX + RW and y >= RY and y < RY + RH) then
        changedIn = changedIn + 1
      else
        changedOut = changedOut + 1
      end
      if not X0 or x < X0 then X0 = x end
      if not Y0 or y < Y0 then Y0 = y end
      if not X1 or x > X1 then X1 = x end
      if not Y1 or y > Y1 then Y1 = y end
      if mask then mask:putPixel(x, y, app.pixelColor.rgba(255, 0, 0, 255)) end
    end
  end
end
if mask then mask:saveAs(MASK) end

-- B2：空洞通过（vacuous pass）自检。decode 正确时 changed==0 ⇒ 覆盖完全一致 ⇒ 下面三条都不可能成立；
-- 任何一条成立都说明**量尺自己瞎了**，而不是"两张图一样"。
local suspiciousZero, suspiciousReason = false, nil
if changed == 0 and opaqueA > 0 and opaqueB > 0 then
  if opaqueA ~= opaqueB then
    suspiciousZero = true
    suspiciousReason = "changed=0 but the opaque counts differ (a=" .. opaqueA .. " b=" .. opaqueB
      .. "): identical images cannot have different coverage - the meter is likely decoding the wrong way"
  elseif fpA ~= fpB then
    suspiciousZero = true
    suspiciousReason = "changed=0 but the position-weighted alpha fingerprints differ (a=" .. fpA .. " b=" .. fpB
      .. "): the two images are not identical"
  elseif INCLUDE_ALPHA and alphaChanged > 0 then
    suspiciousZero = true
    suspiciousReason = "changed=0 but " .. alphaChanged .. " pixel(s) differ in alpha while includeAlpha=true"
  end
end

io.write("__RESULT__" .. json.encode({ success = true, data = {
  same_size = true,
  size = { w = W, h = H },
  frame = FN,
  region = R and { x = RX, y = RY, w = RW, h = RH } or nil,
  total_pixels = total,
  changed = changed,
  changed_inside_region = changedIn,
  changed_outside_region = changedOut,
  alpha_changed = alphaChanged,
  opaque_a = opaqueA,
  opaque_b = opaqueB,
  alpha_fingerprint_a = fpA,
  alpha_fingerprint_b = fpB,
  suspicious_zero = suspiciousZero,
  suspicious_reason = suspiciousReason,
  bbox = __bbox(X0, Y0, X1, Y1),
  mask_png = MASK,
  stats_basis = { a = basisA, b = basisB },
  semantics = "coverage change, or RGB change where both sides are opaque; RGB under fully transparent pixels is ignored",
  include_alpha = INCLUDE_ALPHA
}}))
`;
        const result = await this.runLuaScript(script);
        if (!result.success)
            return this.error(`diff_sprites: ${result.error ?? "failed"}`);
        const data = (result.data ?? {});
        return this.ok({ ...data, elapsed_ms: Date.now() - t0 });
    }
    // ---- Handler dispatch ---------------------------------------------------
    setupHandlers() {
        // Map tool names to handler methods
        this.toolHandlers.set("create_sprite", (a) => this.handleCreateSprite(a));
        this.toolHandlers.set("open_sprite", (a) => this.handleOpenSprite(a));
        this.toolHandlers.set("save_sprite", (a) => this.handleSaveSprite(a));
        this.toolHandlers.set("get_sprite_info", (a) => this.handleGetSpriteInfo(a));
        this.toolHandlers.set("resize_sprite", (a) => this.handleResizeSprite(a));
        this.toolHandlers.set("crop_sprite", (a) => this.handleCropSprite(a));
        this.toolHandlers.set("add_layer", (a) => this.handleAddLayer(a));
        this.toolHandlers.set("remove_layer", (a) => this.handleRemoveLayer(a));
        this.toolHandlers.set("list_layers", (a) => this.handleListLayers(a));
        this.toolHandlers.set("set_layer_properties", (a) => this.handleSetLayerProperties(a));
        this.toolHandlers.set("add_frame", (a) => this.handleAddFrame(a));
        this.toolHandlers.set("remove_frame", (a) => this.handleRemoveFrame(a));
        this.toolHandlers.set("set_frame_duration", (a) => this.handleSetFrameDuration(a));
        this.toolHandlers.set("list_frames", (a) => this.handleListFrames(a));
        this.toolHandlers.set("create_tag", (a) => this.handleCreateTag(a));
        this.toolHandlers.set("remove_tag", (a) => this.handleRemoveTag(a));
        this.toolHandlers.set("list_tags", (a) => this.handleListTags(a));
        this.toolHandlers.set("draw_pixels", (a) => this.handleDrawPixels(a));
        this.toolHandlers.set("draw_rect", (a) => this.handleDrawRect(a));
        this.toolHandlers.set("draw_circle", (a) => this.handleDrawCircle(a));
        this.toolHandlers.set("draw_line", (a) => this.handleDrawLine(a));
        this.toolHandlers.set("draw_ellipse", (a) => this.handleDrawEllipse(a));
        this.toolHandlers.set("fill_area", (a) => this.handleFillArea(a));
        this.toolHandlers.set("replace_color", (a) => this.handleReplaceColor(a));
        this.toolHandlers.set("flip_sprite", (a) => this.handleFlipSprite(a));
        this.toolHandlers.set("rotate_sprite", (a) => this.handleRotateSprite(a));
        this.toolHandlers.set("flatten_layers", (a) => this.handleFlattenLayers(a));
        this.toolHandlers.set("merge_down", (a) => this.handleMergeDown(a));
        this.toolHandlers.set("outline", (a) => this.handleOutline(a));
        this.toolHandlers.set("get_palette", (a) => this.handleGetPalette(a));
        this.toolHandlers.set("set_palette_colors", (a) => this.handleSetPaletteColors(a));
        this.toolHandlers.set("load_palette", (a) => this.handleLoadPalette(a));
        this.toolHandlers.set("resize_palette", (a) => this.handleResizePalette(a));
        this.toolHandlers.set("move_cel", (a) => this.handleMoveCel(a));
        this.toolHandlers.set("set_cel_opacity", (a) => this.handleSetCelOpacity(a));
        this.toolHandlers.set("clear_cel", (a) => this.handleClearCel(a));
        this.toolHandlers.set("export_sprite_sheet", (a) => this.handleExportSpriteSheet(a));
        this.toolHandlers.set("export_frame", (a) => this.handleExportFrame(a));
        this.toolHandlers.set("export_layers", (a) => this.handleExportLayers(a));
        this.toolHandlers.set("create_slice", (a) => this.handleCreateSlice(a));
        this.toolHandlers.set("remove_slice", (a) => this.handleRemoveSlice(a));
        this.toolHandlers.set("run_script", (a) => this.handleRunScript(a));
        this.toolHandlers.set("get_aseprite_version", (a) => this.handleGetAsepriteVersion(a));
        this.toolHandlers.set("indexify", (a) => this.handleIndexify(a));
        this.toolHandlers.set("get_layer_image", (a) => this.handleGetLayerImage(a));
        this.toolHandlers.set("set_layer_image", (a) => this.handleSetLayerImage(a));
        this.toolHandlers.set("snap_to_palette", (a) => this.handleSnapToPalette(a));
        this.toolHandlers.set("remap_palette", (a) => this.handleRemapPalette(a));
        this.toolHandlers.set("apply_mask", (a) => this.handleApplyMask(a));
        this.toolHandlers.set("verify_artifact", (a) => this.handleVerifyArtifact(a));
        this.toolHandlers.set("crop_cels", (a) => this.handleCropCels(a));
        this.toolHandlers.set("measure_sprite", (a) => this.handleMeasureSprite(a));
        this.toolHandlers.set("measure_layers", (a) => this.handleMeasureLayers(a));
        this.toolHandlers.set("derive_mirror", (a) => this.handleDeriveMirror(a));
        this.toolHandlers.set("export_meter_module", (a) => this.handleExportMeterModule(a));
        this.toolHandlers.set("swap_family_palette", (a) => this.handleSwapFamilyPalette(a));
        this.toolHandlers.set("family_palette_consistency", (a) => this.handleFamilyPaletteConsistency(a));
        this.toolHandlers.set("palette_spec_check", (a) => this.handlePaletteSpecCheck(a));
        this.toolHandlers.set("remap_colors", (a) => this.handleRemapColors(a));
        this.toolHandlers.set("diff_sprites", (a) => this.handleDiffSprites(a));
        // ListTools handler
        this.server.setRequestHandler(ListToolsRequestSchema, async () => ({
            tools: this.toolDefinitions(),
        }));
        // CallTool handler
        this.server.setRequestHandler(CallToolRequestSchema, async (request) => {
            const { name, arguments: args } = request.params;
            const handler = this.toolHandlers.get(name);
            if (!handler) {
                return this.error(`Unknown tool: ${name}`);
            }
            const argObject = args ?? {};
            const missing = this.missingRequired(name, argObject);
            if (missing) {
                return this.error(missing);
            }
            try {
                return await handler(argObject);
            }
            catch (err) {
                const msg = err instanceof Error ? err.message : String(err);
                return this.error(`Tool ${name} failed: ${msg}`);
            }
        });
    }
    // ---- Start --------------------------------------------------------------
    async run() {
        const transport = new StdioServerTransport();
        await this.server.connect(transport);
        console.error("[aseprite-mcp] Server running on stdio");
    }
}
// ---------------------------------------------------------------------------
// Main — only runs when executed directly, not when imported
// ---------------------------------------------------------------------------
const isMainModule = process.argv[1] &&
    (process.argv[1].endsWith("index.js") || process.argv[1].endsWith("index.ts"));
if (isMainModule) {
    const server = new AsepriteMcpServer();
    server.run().catch((err) => {
        console.error("Fatal error:", err);
        process.exit(1);
    });
}
