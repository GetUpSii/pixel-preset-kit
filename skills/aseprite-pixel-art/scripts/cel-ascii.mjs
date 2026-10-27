#!/usr/bin/env node
// cel-ascii.mjs — 画稿 ↔ ASCII 网格（C3）
//
// Why this exists: in the 2026-10-15 farm02 session every character change was
// "edit letters -> re-emit the sprite", and the ASCII grid IS the stored source of truth
// (see projects/farm02/spec/SCALE-AND-CHAR-2026-10-15.md §5). That loop was hand-written
// Lua every time, ~100+ lines per edit, and it kept re-deciding the table shape — which is
// exactly where the four Lua data-shape failures (#11-#14) came from.
//
// This tool owns the loop and the two assertions that actually caught bugs:
//   * every row length == canvas width
//   * every letter used must be declared in the legend
// plus, when writing back into an INDEXED sprite, "no colour outside the palette"
// (i.e. gate B = 板外色 0) is enforced BEFORE anything is mutated.
//
// Usage
//   node cel-ascii.mjs dump  <src.(aseprite|png)> [--layer NAME] [--frame N]
//                            [--legend in.json] [--legend-out out.json] [--out grid.txt]
//   node cel-ascii.mjs apply <src.(aseprite|png)> --grid <grid.txt> --out <file>
//                            [--layer NAME] [--frame N] [--legend in.json] [--dry-run]
//   node cel-ascii.mjs --selftest            parser + assertion negatives (no Aseprite)
//   node cel-ascii.mjs --selftest-live       full dump -> edit -> apply -> dump round trip
//
// Grid file format (deliberately the format already used in the spec docs):
//   // cel-ascii dump  src=... layer=... frame=1  4x4  mode=INDEXED
//   // legend
//   //   .  transparent
//   //   #  #171310  idx1  n=4
//   ....
//   .##.   1
//   .##.   2
//   ....
// Rows are read as: the first <width> characters, then a separator (>= 1 space) or EOL.
// Everything after the separator is a free-form note and is ignored.
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { runLua } from "./aseprite-lua.mjs";

const TRANSPARENT = ".";
// Deliberately excludes "." (transparent) and excludes any character that could be
// confused with a comment marker or a shell/PowerShell metacharacter.
const CHARSET = "#0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";
const COMMENT = "//";
// One Lua round trip per pixel would be silly, but a whole 1024x576 scene in one JSON
// payload is worse. Refuse politely and point at the batch measurement tools instead.
const MAX_CELLS = 65536;

class GridError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "GridError";
    this.code = code;
  }
}

// ── CLI ────────────────────────────────────────────────────────────────────────
const argv = process.argv.slice(2);
const has = (n) => argv.includes(n);
function val(n, dflt = null) {
  const i = argv.indexOf(n);
  return i >= 0 && i + 1 < argv.length ? argv[i + 1] : dflt;
}

const luaStr = (s) => JSON.stringify(String(s).replace(/\\/g, "/"));
const hex = (r, g, b, a) =>
  "#" + [r, g, b].map((v) => Number(v).toString(16).padStart(2, "0")).join("") +
  (Number(a) === 255 ? "" : Number(a).toString(16).padStart(2, "0"));

function parseHexColor(spec) {
  const m = /^#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$/.exec(String(spec).trim());
  if (!m) return null;
  return {
    r: parseInt(m[1].slice(0, 2), 16),
    g: parseInt(m[1].slice(2, 4), 16),
    b: parseInt(m[1].slice(4, 6), 16),
    a: m[2] ? parseInt(m[2], 16) : 255,
  };
}

// ── grid parsing (pure: no Aseprite, so --selftest can drive it) ───────────────
/**
 * @returns {{rows: string[], letters: Map<string, object>}}
 * @throws {GridError} with a code the selftest asserts on.
 */
export function parseGrid(text, { width, height, legend = null, label = "<grid>" } = {}) {
  if (!Number.isInteger(width) || !Number.isInteger(height)) {
    throw new GridError("E_NO_DIMENSIONS", "width/height are required to validate a grid");
  }
  const letters = new Map();
  if (legend && typeof legend === "object") {
    for (const [ch, spec] of Object.entries(legend)) {
      if (ch === TRANSPARENT || spec === null) {
        letters.set(TRANSPARENT, { transparent: true });
        continue;
      }
      if ([...ch].length !== 1) throw new GridError("E_LEGEND_KEY", `legend key "${ch}" is not a single character`);
      const c = typeof spec === "object" ? spec : parseHexColor(spec);
      if (!c) throw new GridError("E_LEGEND_COLOR", `legend "${ch}" has an unreadable colour: ${JSON.stringify(spec)}`);
      letters.set(ch, c);
    }
  }

  const rows = [];
  // Letters carried in from an external --legend JSON are authoritative; the grid file's
  // own header may repeat them (a dump always writes its whole legend out), so a repeat
  // is only a duplicate when BOTH declarations come from the file.
  const preSeeded = new Set(letters.keys());
  const fileDeclared = new Set();
  const lines = text.split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();
    if (trimmed === "") continue;
    if (trimmed.startsWith(COMMENT)) {
      // dump writes one legend entry per comment line: "//   <ch>  <#hex|transparent>"
      const body = trimmed.slice(COMMENT.length).trim();
      const toks = body.split(/\s+/);
      if (toks.length >= 2 && [...toks[0]].length === 1) {
        const ch = toks[0];
        if (toks[1] === "transparent" || toks[1] === "none") {
          if (!letters.has(TRANSPARENT)) letters.set(TRANSPARENT, { transparent: true });
          fileDeclared.add(TRANSPARENT);
        } else if (parseHexColor(toks[1])) {
          if (fileDeclared.has(ch)) {
            throw new GridError(
              "E_LEGEND_DUPLICATE",
              `${label}:${i + 1}: letter "${ch}" is declared twice in the legend`,
            );
          }
          fileDeclared.add(ch);
          if (!preSeeded.has(ch)) letters.set(ch, parseHexColor(toks[1]));
        }
      }
      continue;
    }
    rows.push({ line, n: i + 1 });
  }

  if (rows.length !== height) {
    throw new GridError(
      "E_ROW_COUNT",
      `${label}: expected ${height} row(s) for a ${width}x${height} canvas, found ${rows.length}`,
    );
  }

  const used = new Set();
  const out = [];
  for (const { line, n } of rows) {
    if (line.length < width) {
      throw new GridError(
        "E_ROW_SHORT",
        `${label}:${n}: row is ${line.length} char(s) but the canvas is ${width} wide`,
      );
    }
    const row = line.slice(0, width);
    for (let x = 0; x < width; x++) {
      const ch = row[x];
      // A row that stops early and is followed by a note lands here: the padding shows up
      // as whitespace inside the width window. Say "short row", not "unknown letter ' '".
      if (/\s/.test(ch)) {
        throw new GridError(
          "E_ROW_SHORT",
          `${label}:${n}: row gives only ${x} column(s) before whitespace but the canvas is ${width} wide`,
        );
      }
      if (!letters.has(ch)) {
        throw new GridError(
          "E_UNKNOWN_LETTER",
          `${label}:${n}: letter "${ch}" at column ${x} is not in the legend` +
            ` (declared: ${[...letters.keys()].join("")})`,
        );
      }
      used.add(ch);
    }
    if (line.length > width && (letters.has(line[width]) || line[width] === TRANSPARENT)) {
      throw new GridError(
        "E_NO_SEPARATOR",
        `${label}:${n}: column ${width} is "${line[width]}", which is a legend letter —` +
          ` a row must be followed by a separator (>= 1 space) or end the line`,
      );
    }
    out.push(row);
  }

  if (out.length && !letters.has(TRANSPARENT)) letters.set(TRANSPARENT, { transparent: true });
  return { rows: out, letters, used };
}

/**
 * Turn parsed rows into colour rows (null = transparent) so a round trip can be compared
 * without depending on which letters a particular dump happened to assign.
 */
export function rowsToHex(rows, letters) {
  return rows.map((r) => [...r].map((ch) => {
    if (ch === TRANSPARENT) return null;
    const c = letters.get(ch);
    return c?.transparent ? null : hex(c.r, c.g, c.b, c.a);
  }));
}

// ── Aseprite side ──────────────────────────────────────────────────────────────
const OPEN_GUARD = (varName, pathExpr) => `
local ${varName} = app.open(${pathExpr})
if not ${varName} then
  io.write("__RESULT__" .. json.encode({ success = false, error = "cannot open " .. ${pathExpr} }))
  return
end`;

const FIND_LAYER = (spr) => `
local __wantLayer = WANT_LAYER
local __layer = nil
if __wantLayer ~= "" then
  for __i = 1, #${spr}.layers do
    if ${spr}.layers[__i].name == __wantLayer then __layer = ${spr}.layers[__i] end
  end
  if __layer == nil then
    local __names = {}
    for __i = 1, #${spr}.layers do __names[#__names + 1] = ${spr}.layers[__i].name end
    io.write("__RESULT__" .. json.encode({ success = false, error =
      "no layer named '" .. __wantLayer .. "'; have: " .. table.concat(__names, ", ") }))
    return
  end
else
  __layer = ${spr}.layers[1]
end`;

async function probe(src, scratchDir) {
  const lua = `
${OPEN_GUARD("src", luaStr(src))}
local indexed = (src.colorMode == ColorMode.INDEXED)
local names = {}
for i = 1, #src.layers do names[#names + 1] = src.layers[i].name end
local entries = {}
if indexed then
  local pal = src.palettes[1]
  for i = 0, 255 do
    local ok, c = pcall(function() return pal:getColor(i) end)
    if (not ok) or (c == nil) then break end
    entries[#entries + 1] = { r = c.red, g = c.green, b = c.blue, a = c.alpha }
  end
end
io.write("__RESULT__" .. json.encode({ success = true, data = {
  width = src.width, height = src.height, frames = #src.frames,
  indexed = indexed,
  transparentColor = indexed and src.transparentColor or -1,
  layers = names, palette = entries
}}))
`;
  const res = await runLua(lua, { scratchDir });
  if (!res.success) throw new GridError("E_ASEPRITE", res.error);
  return res.data;
}

async function readCel(src, { layer, frame }, scratchDir) {
  const lua = `
${OPEN_GUARD("src", luaStr(src))}
local indexed = (src.colorMode == ColorMode.INDEXED)
local pal = src.palettes[1]
local tc = indexed and src.transparentColor or -1
local WANT_LAYER = ${luaStr(layer ?? "")}
${FIND_LAYER("src")}
local frame = ${Number(frame)}
local w, h = src.width, src.height
if w * h > ${MAX_CELLS} then
  io.write("__RESULT__" .. json.encode({ success = false, error =
    "canvas " .. w .. "x" .. h .. " exceeds the " .. ${MAX_CELLS} .. "-pixel grid limit" }))
  return
end
local cel = __layer:cel(frame)
if cel == nil then
  io.write("__RESULT__" .. json.encode({ success = false, error =
    "layer '" .. __layer.name .. "' has no cel on frame " .. frame }))
  return
end
local img = cel.image
local cx, cy = cel.position.x, cel.position.y
local cells = {}
local colors = {}
local keyToId = {}
for y = 0, h - 1 do
  for x = 0, w - 1 do
    local r, g, b, a, idx = 0, 0, 0, 0, -1
    local lx, ly = x - cx, y - cy
    if lx >= 0 and ly >= 0 and lx < img.width and ly < img.height then
      local v = img:getPixel(lx, ly)
      if indexed then
        idx = v
        if v == tc then
          a = 0
        else
          local c = pal:getColor(v)
          r, g, b, a = c.red, c.green, c.blue, c.alpha
        end
      else
        local c = Color(v)
        r, g, b, a = c.red, c.green, c.blue, c.alpha
      end
    end
    local k = r .. "," .. g .. "," .. b .. "," .. a
    local id = keyToId[k]
    if id == nil then
      colors[#colors + 1] = { r = r, g = g, b = b, a = a, idx = idx, count = 1 }
      id = #colors
      keyToId[k] = id
    else
      colors[id].count = colors[id].count + 1
    end
    cells[#cells + 1] = id
  end
end
io.write("__RESULT__" .. json.encode({ success = true, data = {
  width = w, height = h, frame = frame, layer = __layer.name,
  indexed = indexed, cells = cells, colors = colors
}}))
`;
  const res = await runLua(lua, { scratchDir });
  if (!res.success) throw new GridError("E_ASEPRITE", res.error);
  return res.data;
}

// ── dump ───────────────────────────────────────────────────────────────────────
function assignLetters(colors, fixed) {
  const byHex = new Map();
  if (fixed) {
    for (const [ch, spec] of Object.entries(fixed)) {
      if (ch === TRANSPARENT || spec === null) continue;
      const c = typeof spec === "object" ? spec : parseHexColor(spec);
      if (c) byHex.set(hex(c.r, c.g, c.b, c.a).toUpperCase(), ch);
    }
  }
  const letters = new Map([[TRANSPARENT, { transparent: true, count: 0 }]]);
  const taken = new Set([...byHex.values()]);
  const notes = [];
  for (const c of colors) {
    if (c.a === 0) {
      letters.get(TRANSPARENT).count += c.count;
      continue;
    }
    const h = hex(c.r, c.g, c.b, c.a);
    const preferred = byHex.get(h.toUpperCase());
    if (preferred != null && !letters.has(preferred)) {
      letters.set(preferred, { ...c, hex: h });
      continue;
    }
    if (preferred != null) notes.push(`${h} wanted letter "${preferred}" but it is already used`);
    const free = [...CHARSET].find((ch) => !taken.has(ch) && !letters.has(ch));
    if (free == null) {
      throw new GridError(
        "E_TOO_MANY_COLORS",
        `${letters.size} distinct colours; the single-character legend holds ${CHARSET.length}` +
          ` — quantize first (snap_to_palette / indexify)`,
      );
    }
    taken.add(free);
    letters.set(free, { ...c, hex: h });
  }
  return { letters, notes };
}

async function dump(opts) {
  const src = opts.src;
  const dir = dirname(resolve(src));
  const cel = await readCel(src, { layer: opts.layer, frame: opts.frame ?? 1 }, dir);
  const { letters, notes } = assignLetters(cel.colors, opts.legend);
  const rows = [];
  for (let y = 0; y < cel.height; y++) {
    let row = "";
    for (let x = 0; x < cel.width; x++) {
      const c = cel.colors[cel.cells[y * cel.width + x] - 1];
      if (c.a === 0) {
        row += TRANSPARENT;
        continue;
      }
      const h = hex(c.r, c.g, c.b, c.a).toUpperCase();
      const hit = [...letters.entries()].find(([, v]) => !v.transparent && v.hex.toUpperCase() === h);
      row += hit[0];
    }
    rows.push(row);
  }

  const legendJson = {};
  for (const [ch, v] of letters) legendJson[ch] = v.transparent ? null : v.hex;
  if (opts.legendOut) {
    writeFileSync(opts.legendOut, JSON.stringify(legendJson, null, 2) + "\n", "utf-8");
  }

  const header = [
    `${COMMENT} cel-ascii dump  src=${src.replace(/\\/g, "/")}  layer=${cel.layer}  frame=${cel.frame}` +
      `  ${cel.width}x${cel.height}  mode=${cel.indexed ? "INDEXED" : "RGB"}`,
    `${COMMENT} legend (letter -> colour, idx = palette index)`,
    `${COMMENT}   ${TRANSPARENT}  transparent  n=${letters.get(TRANSPARENT)?.count ?? 0}`,
    ...[...letters.entries()]
      .filter(([, v]) => !v.transparent)
      .map(([ch, v]) => `${COMMENT}   ${ch}  ${v.hex}  idx${v.idx}  n=${v.count}`),
  ];
  const body = rows.map((r, y) => `${r}   ${y}`).join("\n");
  const text = header.join("\n") + "\n" + body + "\n";

  if (opts.out) {
    mkdirSync(dirname(resolve(opts.out)), { recursive: true });
    writeFileSync(opts.out, text, "utf-8");
  } else {
    process.stdout.write(text);
  }

  // Re-parse what we just emitted: the dump must be a grid its own `apply` accepts.
  const check = parseGrid(text, { width: cel.width, height: cel.height, label: "<dump>" });
  if (check.rows.length !== cel.height) throw new GridError("E_SELF", "dump round trip lost rows");
  const summary = {
    mode: cel.indexed ? "INDEXED" : "RGB",
    canvas: `${cel.width}x${cel.height}`,
    layer: cel.layer,
    frame: cel.frame,
    distinctColors: [...letters.values()].filter((v) => !v.transparent).length,
    transparentPixels: letters.get(TRANSPARENT)?.count ?? 0,
    out: opts.out ?? "(stdout)",
  };
  console.log(JSON.stringify(summary, null, 2));
  for (const n of notes) console.error(`  warn: ${n}`);
  if (opts.legendOut) console.log(`legend: ${opts.legendOut}`);
  return { rows, letters, ...summary };
}

// ── apply ──────────────────────────────────────────────────────────────────────
async function apply(opts) {
  const src = opts.src;
  const dir = dirname(resolve(src));
  const info = await probe(src, dir);
  if (!existsSync(opts.grid)) throw new GridError("E_IO", `grid not found: ${opts.grid}`);
  const text = readFileSync(opts.grid, "utf-8");
  const parsed = parseGrid(text, {
    width: info.width,
    height: info.height,
    legend: opts.legend,
    label: opts.grid,
  });

  // Resolve every used letter once, so a bad input fails BEFORE any mutation.
  const usable = new Map();
  const palette = info.palette ?? [];
  for (const ch of parsed.used) {
    if (ch === TRANSPARENT) continue;
    const c = parsed.letters.get(ch);
    if (c.transparent) continue;
    let idx = -1;
    if (info.indexed) {
      if (c.a !== 255) {
        throw new GridError(
          "E_ALPHA_NOT_BINARY",
          `${opts.grid}: letter "${ch}" is ${hex(c.r, c.g, c.b, c.a)} but an INDEXED sprite has no` +
            ` partial alpha — use the transparent letter (${TRANSPARENT}) or a palette entry`,
        );
      }
      const hits = palette
        .map((p, i) => ({ i, p }))
        .filter(({ i, p }) => i !== info.transparentColor && p.r === c.r && p.g === c.g && p.b === c.b);
      if (!hits.length) {
        throw new GridError(
          "E_OUT_OF_PALETTE",
          `${opts.grid}: letter "${ch}" = ${hex(c.r, c.g, c.b, c.a)} is not in the palette` +
            ` (板外色). Palette has ${palette.length} entry/entries; snap_to_palette or load_palette first.`,
        );
      }
      if (hits.length > 1) {
        console.error(`  warn: ${hex(c.r, c.g, c.b, c.a)} matches palette indices ${hits.map((h) => h.i).join(",")}; using ${hits[0].i}`);
      }
      idx = hits[0].i;
    }
    usable.set(ch, { ...c, idx });
  }

  const luaEntries = [...usable.entries()]
    .map(([ch, c]) => `  ["${ch}"] = { r = ${c.r}, g = ${c.g}, b = ${c.b}, a = ${c.a}, idx = ${c.idx} },`)
    .join("\n");
  const luaRows = parsed.rows.map((r) => `"${r}",`).join("\n");

  const lua = `
${OPEN_GUARD("src", luaStr(src))}
local WANT_LAYER = ${luaStr(opts.layer ?? "")}
${FIND_LAYER("src")}
local indexed = (src.colorMode == ColorMode.INDEXED)
local w, h = src.width, src.height
local LETTERS = {
${luaEntries}
}
local ROWS = {
${luaRows}
}
local img = Image(w, h, src.colorMode)
for y = 0, h - 1 do
  local row = ROWS[y + 1]
  for x = 0, w - 1 do
    local ch = string.sub(row, x + 1, x + 1)
    if ch == "${TRANSPARENT}" then
      if indexed then
        img:putPixel(x, y, src.transparentColor)
      else
        img:putPixel(x, y, Color(0, 0, 0, 0))
      end
    else
      local e = LETTERS[ch]
      if indexed then
        img:putPixel(x, y, e.idx)
      else
        img:putPixel(x, y, Color(e.r, e.g, e.b, e.a))
      end
    end
  end
end
local frame = ${Number(opts.frame ?? 1)}
local cel = __layer:cel(frame)
if cel == nil then
  src:newCel(__layer, frame, img, Point(0, 0))
else
  cel.image = img
  cel.position = Point(0, 0)
end
src:saveCopyAs(${luaStr(opts.out)})
io.write("__RESULT__" .. json.encode({ success = true, data = {
  out = ${luaStr(opts.out)}, layer = __layer.name, frame = frame,
  width = w, height = h, mode = (indexed and "INDEXED" or "RGB"),
  letters = ${parsed.used.size}
}}))
`;
  if (opts.dryRun) {
    console.log("DRY-RUN: grid validates and every colour resolves; nothing written");
    console.log(JSON.stringify({
      canvas: `${info.width}x${info.height}`,
      mode: info.indexed ? "INDEXED" : "RGB",
      letters: [...parsed.used].join(""),
    }, null, 2));
    return;
  }
  const res = await runLua(lua, { scratchDir: dirname(resolve(opts.out)) });
  if (!res.success) throw new GridError("E_ASEPRITE", res.error);
  console.log(JSON.stringify(res.data, null, 2));
  return res.data;
}

// ── selftest (pure) ────────────────────────────────────────────────────────────
function expectThrow(label, fn, code, needle, problems) {
  try {
    fn();
    problems.push(`${label}: expected ${code}, nothing was thrown (this gate cannot be tripped)`);
  } catch (e) {
    if (!(e instanceof GridError)) return problems.push(`${label}: wrong error class ${e.name}: ${e.message}`);
    if (e.code !== code) problems.push(`${label}: expected ${code}, got ${e.code} (${e.message})`);
    if (needle && !e.message.includes(needle)) problems.push(`${label}: message lacks "${needle}": ${e.message}`);
  }
  return problems;
}

function selftest() {
  const problems = [];
  const W = 4;
  const H = 4;
  const legend = { ".": null, "#": "#171310", k: "#c89060" };
  const good = [
    "// cel-ascii dump test",
    "// legend",
    "//   .  transparent",
    "//   #  #171310  idx1  n=4",
    "//   k  #c89060  idx2  n=2",
    "....  0",
    ".##.  1",
    ".#k.  2",
    "....  3",
  ].join("\n");

  // positive
  const p = parseGrid(good, { width: W, height: H, legend, label: "good" });
  if (p.rows.length !== H) problems.push(`good: ${p.rows.length} rows`);
  if (p.rows[2] !== ".#k.") problems.push(`good: row 2 parsed as "${p.rows[2]}"`);
  if (p.used.size !== 3) problems.push(`good: used=${[...p.used]} (expect .,#,k)`);

  // negatives — each gate must be trippable
  expectThrow("E_ROW_SHORT", () => parseGrid(good.replace(".##.  1", ".#.  1"), { width: W, height: H, legend, label: "short" }),
    "E_ROW_SHORT", "canvas is 4 wide", problems);
  expectThrow("E_UNKNOWN_LETTER", () => parseGrid(good.replace(".#k.  2", ".#z.  2"), { width: W, height: H, legend, label: "unk" }),
    "E_UNKNOWN_LETTER", '"z" at column 2', problems);
  expectThrow("E_ROW_COUNT", () => parseGrid(good.split("\n").slice(0, -1).join("\n"), { width: W, height: H, legend, label: "count" }),
    "E_ROW_COUNT", "found 3", problems);
  expectThrow("E_NO_SEPARATOR", () => parseGrid(good.replace(".##.  1", ".##.k 1"), { width: W, height: H, legend, label: "sep" }),
    "E_NO_SEPARATOR", "separator", problems);
  expectThrow("E_LEGEND_DUPLICATE", () => parseGrid("//   #  #171310\n//   #  #ffffff\n....\n....\n....\n....", { width: W, height: H, legend: null, label: "dup" }),
    "E_LEGEND_DUPLICATE", "twice", problems);

  // a colour outside a fixed legend must still parse (letter is legal) ...
  const parses = parseGrid("....\n.##.\n.##.\n....", { width: W, height: H, legend, label: "noheader" });
  if (parses.rows[1] !== ".##.") problems.push("noheader: row 1 wrong");

  // ... but an undeclared letter must not
  expectThrow("undeclared", () => parseGrid("....\n.kk.\n.##.\n....", { width: W, height: H, legend: { ".": null, "#": "#171310" }, label: "undeclared" }),
    "E_UNKNOWN_LETTER", '"k" at column 1', problems);

  // hex rows are letter-independent -> the round-trip comparison is real
  const hexRows = rowsToHex(p.rows, p.letters);
  const wantMiddle = [null, "#171310", "#c89060", null];
  if (JSON.stringify(hexRows[2]) !== JSON.stringify(wantMiddle)) {
    problems.push(`rowsToHex: ${JSON.stringify(hexRows[2])}`);
  }

  // dump's own header must be re-readable (the emit->parse contract)
  const fromHeader = parseGrid(good, { width: W, height: H, legend: null, label: "hdr" });
  if (!fromHeader.letters.has("#")) problems.push("header legend was not recovered from the comment block");
  if (!fromHeader.letters.has(TRANSPARENT)) problems.push("transparent slot was not recovered");

  // --help-shaped guard: a bogus width must be refused, not guessed
  expectThrow("E_NO_DIMENSIONS", () => parseGrid(good, { width: NaN, height: H }), "E_NO_DIMENSIONS", "required", problems);

  console.log(`selftest: ${problems.length ? "FAIL" : "PASS"} (${W}x${H} fixture, 7 negative gates)`);
  if (problems.length) {
    for (const x of problems) console.error("  x " + x);
    process.exit(1);
  }
  return problems;
}

// ── selftest-live ──────────────────────────────────────────────────────────────
async function selftestLive(opts) {
  const work = resolve(opts.work ?? join(process.env.ASEPRITE_MCP_TMP || process.cwd(), ".cel-ascii-selftest"));
  rmSync(work, { recursive: true, force: true });
  mkdirSync(work, { recursive: true });
  const fixture = join(work, "fixture.aseprite");
  const outFile = join(work, "applied.aseprite");
  const problems = [];

  const mk = await runLua(`
local spr = Sprite(4, 4, ColorMode.INDEXED)
local pal = spr.palettes[1]
pal:resize(4)
pal:setColor(1, Color(23, 19, 16, 255))
pal:setColor(2, Color(255, 0, 0, 255))
pal:setColor(3, Color(0, 255, 0, 255))
local img = spr.cels[1].image
img:putPixel(1, 1, 1) img:putPixel(2, 1, 1)
img:putPixel(1, 2, 1) img:putPixel(2, 2, 1)
spr:saveCopyAs(${luaStr(fixture)})
io.write("__RESULT__" .. json.encode({ success = true, data = { indexed = (spr.colorMode == ColorMode.INDEXED) } }))
`, { scratchDir: work });
  if (!mk.success) { console.error("fixture creation failed: " + mk.error); process.exit(2); }

  const info = await probe(fixture, work);
  if (!info.indexed) problems.push("fixture is not INDEXED");
  if (info.palette.length !== 4) problems.push(`fixture palette size ${info.palette.length} != 4`);

  // 1) dump the fixture and check the colours, letter-independently
  const cel = await readCel(fixture, { frame: 1 }, work);
  const { letters } = assignLetters(cel.colors, null);
  const hexOf = (x, y) => {
    const c = cel.colors[cel.cells[y * cel.width + x] - 1];
    return c.a === 0 ? TRANSPARENT : hex(c.r, c.g, c.b, c.a);
  };
  const want = [
    [".", ".", ".", "."],
    [".", "#171310", "#171310", "."],
    [".", "#171310", "#171310", "."],
    [".", ".", ".", "."],
  ];
  for (let y = 0; y < 4; y++) {
    for (let x = 0; x < 4; x++) {
      if (hexOf(x, y) !== want[y][x]) problems.push(`fixture pixel ${x},${y} = ${hexOf(x, y)} want ${want[y][x]}`);
    }
  }
  if ([...letters.values()].filter((v) => !v.transparent).length !== 1) {
    problems.push(`fixture should have exactly 1 opaque colour, dump found ${[...letters.values()].filter((v) => !v.transparent).length}`);
  }

  // 2) edit the grid (letters only) and apply it back
  const gridFile = join(work, "edited.txt");
  writeFileSync(gridFile, [
    "// hand-edited: paint the green entry (idx3) at 2,1",
    "//   .  transparent",
    "//   #  #171310",
    "//   a  #00ff00",
    "....",
    ".#a.",
    ".##.",
    "....",
  ].join("\n") + "\n", "utf-8");
  try {
    await apply({ src: fixture, grid: gridFile, out: outFile, layer: null, frame: 1 });
  } catch (e) {
    problems.push(`apply round trip threw: ${e.message}`);
  }
  if (!existsSync(outFile)) problems.push("apply produced no output file");
  else {
    const after = await readCel(outFile, { frame: 1 }, work);
    const at = (x, y) => {
      const c = after.colors[after.cells[y * after.width + x] - 1];
      return c.a === 0 ? TRANSPARENT : hex(c.r, c.g, c.b, c.a);
    };
    if (at(2, 1) !== "#00ff00") problems.push(`applied pixel 2,1 = ${at(2, 1)} want #00ff00`);
    if (at(1, 1) !== "#171310") problems.push(`applied pixel 1,1 = ${at(1, 1)} want #171310`);
    if (at(0, 0) !== TRANSPARENT) problems.push(`applied pixel 0,0 = ${at(0, 0)} want transparent`);
    if (after.cells.length !== 16) problems.push(`applied cel is ${after.cells.length} cells`);
  }

  // 3) NEGATIVE: a colour outside the palette must be refused AND must not write
  const badFile = join(work, "bad.txt");
  const badOut = join(work, "must-not-exist.aseprite");
  writeFileSync(badFile, [
    "//   .  transparent",
    "//   z  #123456",
    "....",
    ".zz.",
    ".zz.",
    "....",
  ].join("\n") + "\n", "utf-8");
  try {
    await apply({ src: fixture, grid: badFile, out: badOut, layer: null, frame: 1 });
    problems.push("out-of-palette grid was accepted (板外色 gate cannot be tripped)");
  } catch (e) {
    if (e.code !== "E_OUT_OF_PALETTE") problems.push(`out-of-palette gave ${e.code}: ${e.message}`);
  }
  if (existsSync(badOut)) problems.push("a refused apply still wrote its output file");

  // 4) NEGATIVE: a short row must be refused before any mutation
  const shortFile = join(work, "short.txt");
  writeFileSync(shortFile, ["//   .  transparent", "//   #  #171310", "....", ".#.", ".##.", "...."].join("\n") + "\n", "utf-8");
  try {
    await apply({ src: fixture, grid: shortFile, out: join(work, "nope.aseprite"), layer: null, frame: 1 });
    problems.push("short row was accepted (row-length assertion cannot be tripped)");
  } catch (e) {
    if (e.code !== "E_ROW_SHORT") problems.push(`short row gave ${e.code}: ${e.message}`);
  }

  console.log(`selftest-live: ${problems.length ? "FAIL" : "PASS"}  work=${work}`);
  if (problems.length) {
    for (const p of problems) console.error("  x " + p);
    process.exit(1);
  }
}

// ── dispatch ───────────────────────────────────────────────────────────────────
// Guarded so the module can be imported (parseGrid / rowsToHex are exported and are
// reused by the project's own gates) without the CLI running on import.
const isEntry =
  process.argv[1] != null && fileURLToPath(import.meta.url) === resolve(process.argv[1]);

if (!isEntry) {
  // imported as a library
} else if (has("--selftest")) {
  selftest();
} else if (has("--selftest-live")) {
  await selftestLive({ work: val("--work") });
} else {
  const cmd = argv[0];
  const src = argv[1];
  if ((cmd !== "dump" && cmd !== "apply") || !src) {
    console.error(readFileSync(new URL(import.meta.url), "utf-8").split("\n").slice(2, 29).map((l) => l.replace(/^\/\/ ?/, "")).join("\n"));
    process.exit(2);
  }
  let legend = null;
  if (val("--legend")) {
    if (!existsSync(val("--legend"))) { console.error(`legend not found: ${val("--legend")}`); process.exit(2); }
    legend = JSON.parse(readFileSync(val("--legend"), "utf-8"));
  }
  try {
    if (cmd === "dump") {
      await dump({ src, layer: val("--layer"), frame: Number(val("--frame", "1")), legend, legendOut: val("--legend-out"), out: val("--out") });
    } else {
      const out = val("--out");
      const grid = val("--grid");
      if (!out || !grid) { console.error("apply needs --grid <file> and --out <file>"); process.exit(2); }
      await apply({ src, grid, out, layer: val("--layer"), frame: Number(val("--frame", "1")), legend, dryRun: has("--dry-run") });
    }
  } catch (e) {
    if (e instanceof GridError) {
      console.error(`${e.code}: ${e.message}`);
      process.exit(1);
    }
    console.error(`ERROR: ${e.message}`);
    process.exit(2);
  }
}
