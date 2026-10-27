// Nearest-neighbour upscale, so a human can actually see a 16x16..64x64 sprite.
//
// Usage: node preview.mjs <src.(aseprite|png)> <out.png> [scale=8] [--legacy-read]
//
// The agent never judges the artwork itself: this exists so the finished image can be
// presented to the user at a legible size.
//
// 2026-10-15 FIX (farm02): the previous version read pixels with
//     local c = Color(srcImg:getPixel(x, y));  if c.alpha > 0 then ...
// which is only correct for RGB sources. In an INDEXED sprite getPixel returns the
// PALETTE INDEX, and Color(index) decodes it as a packed 0xAABBGGRR value whose alpha
// byte is 0 for every realistic index => every pixel looked transparent => the script
// wrote a fully TRANSPARENT preview and still reported success. Two previews in farm02
// (`out/char_player_v3_idle_x8.png`, `out/char_player_v3_side_e_walk_x8.png`) were
// delivered blank this way (measured: 0 opaque pixels, 2125 / 1617 bytes).
//
// Two independent changes, because a silent wrong answer is worse than a crash:
//   1. INDEXED-aware read: palette index + sprite.transparentColor.
//   2. A gate with teeth: the upscale is a pure NEAREST blow-up, so
//      out_opaque MUST equal in_opaque * scale^2. If not, nothing is saved and the
//      script exits non-zero with the measured numbers.
//      `--legacy-read` reproduces the old buggy read path on purpose, so the gate can be
//      shown to actually fire (negative control) instead of being taken on faith.
import { existsSync, statSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { runLua } from "./aseprite-lua.mjs";

const argv = process.argv.slice(2);
const legacyRead = argv.includes("--legacy-read");
const [srcArg, dstArg, scaleArg] = argv.filter((a) => !a.startsWith("--"));
if (!srcArg || !dstArg) {
  console.error("usage: node preview.mjs <src.(aseprite|png)> <out.png> [scale=8] [--legacy-read]");
  process.exit(2);
}
const scale = Math.max(1, Math.min(32, Number(scaleArg || 8) || 8));
const src = resolve(srcArg).replace(/\\/g, "/");
const dst = resolve(dstArg).replace(/\\/g, "/");

if (!existsSync(srcArg)) {
  console.error(`source not found: ${srcArg}`);
  process.exit(2);
}

const lua = `
local s = ${scale}
local legacyRead = ${legacyRead ? "true" : "false"}
local src = app.open("${src}")
if not src then
  io.write("__RESULT__" .. json.encode({ success = false, error = "cannot open source" }))
  return
end
local sw, sh = src.width, src.height
local srcImg = src.cels[1].image

-- INDEXED or not: decide once, from measured state (never from assumption).
local isIndexed = (tostring(src.colorMode) == tostring(ColorMode.INDEXED))
local pal = src.palettes[1]
local transIdx = src.transparentColor

-- Read one pixel as (opaque, Color). Single source of truth for both passes.
local function readPixel(x, y)
  local v = srcImg:getPixel(x, y)
  if isIndexed and not legacyRead then
    if v == transIdx then return false, nil end
    local c = pal:getColor(v)
    if c.alpha == 0 then return false, nil end
    return true, c
  else
    local c = Color(v)          -- legacy path: WRONG for INDEXED (kept as a negative control)
    if c.alpha > 0 then return true, c end
    return false, nil
  end
end

-- Pass 1: count source opaque pixels.
local inOpaque = 0
for y = 0, sh - 1 do
  for x = 0, sw - 1 do
    local ok = readPixel(x, y)
    if ok then inOpaque = inOpaque + 1 end
  end
end

local expected = inOpaque * s * s
if expected == 0 then
  io.write("__RESULT__" .. json.encode({ success = false, error = "source has 0 opaque pixels (nothing to preview)", data = { src = "${src}", mode = tostring(src.colorMode) } }))
  return
end

-- Pass 2: blow up, counting what actually landed.
local out = Sprite(sw * s, sh * s, ColorMode.RGB)
local dstImg = out.cels[1].image
local outOpaque = 0
for y = 0, sh - 1 do
  for x = 0, sw - 1 do
    local ok, c = readPixel(x, y)
    if ok then
      for dy = 0, s - 1 do
        for dx = 0, s - 1 do
          dstImg:putPixel(x * s + dx, y * s + dy, c)
        end
      end
      outOpaque = outOpaque + s * s
    end
  end
end

-- Gate: a NEAREST blow-up is lossless in coverage, so these must match exactly.
if outOpaque ~= expected then
  io.write("__RESULT__" .. json.encode({ success = false, error = "PREVIEW GATE FAILED: upscaled opaque pixels != source opaque * scale^2 (nothing saved)", data = {
    src = "${src}", mode = tostring(src.colorMode), indexed = isIndexed, legacyRead = legacyRead,
    srcOpaque = inOpaque, scale = s, expected = expected, got = outOpaque
  }}))
  return
end

out:saveCopyAs("${dst}")
io.write("__RESULT__" .. json.encode({ success = true, data = {
  src = "${src}", dst = "${dst}",
  size = tostring(sw) .. "x" .. tostring(sh),
  previewSize = tostring(out.width) .. "x" .. tostring(out.height),
  scale = s, indexed = isIndexed, legacyRead = legacyRead,
  srcOpaque = inOpaque, outOpaque = outOpaque, expected = expected,
  gate = "out==in*scale^2 PASS"
}}))
`;

const res = await runLua(lua, { scratchDir: dirname(resolve(dstArg)) });
if (!res.success) {
  console.error("FAILED: " + res.error);
  if (res.data) console.error(JSON.stringify(res.data, null, 2));
  process.exit(1);
}
console.log(JSON.stringify(res.data ?? res, null, 2));
console.log("out_bytes=" + (existsSync(dstArg) ? statSync(dstArg).size : -1));
