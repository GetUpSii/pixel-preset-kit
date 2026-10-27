// Structural QA for pixel art: the "pixel lines" property, checked in code rather
// than by eye. Exit 0 = clean, 1 = violation, 2 = usage/IO error.
//
// Usage: node check-thin.mjs <sprite.(aseprite|png)>
//
// Rule enforced: no fully-filled 2x2 block of non-transparent pixels anywhere.
// That is the classic pixel-perfect violation (a doubled corner on a diagonal).
// NOTE: "two pixels side by side in one row" is NOT a defect — a shallow Bresenham
// line legitimately has several pixels per row.
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { runLua } from "./aseprite-lua.mjs";

const target = process.argv[2];
if (!target) {
  console.error("usage: node check-thin.mjs <sprite.(aseprite|png)>");
  process.exit(2);
}
if (!existsSync(target)) {
  console.error(`not found: ${target}`);
  process.exit(2);
}
const p = resolve(target).replace(/\\/g, "/");

const lua = `
local spr = app.open("${p}")
if not spr then
  io.write("__RESULT__" .. json.encode({ success = false, error = "cannot open" }))
  return
end
local img = spr.cels[1].image
local w, h = img.width, img.height
local rows = {}
local filled = 0
for y = 0, h - 1 do
  local cells = {}
  for x = 0, w - 1 do
    local c = Color(img:getPixel(x, y))
    if c.alpha > 0 then
      filled = filled + 1
      cells[#cells + 1] = "1"
    else
      cells[#cells + 1] = "0"
    end
  end
  rows[#rows + 1] = table.concat(cells)
end
io.write("__RESULT__" .. json.encode({ success = true, data = {
  width = w, height = h, layers = #spr.layers, frames = #spr.frames,
  filledPixels = filled, rows = rows
}}))
`;

const res = await runLua(lua, { scratchDir: dirname(resolve(target)) });
if (!res.success) {
  console.error("FAILED: " + res.error);
  process.exit(2);
}

const { width, height, filledPixels, rows, layers, frames } = res.data;
const blocks = [];
for (let y = 0; y + 1 < height; y++) {
  for (let x = 0; x + 1 < width; x++) {
    if (rows[y][x] === "1" && rows[y][x + 1] === "1" && rows[y + 1][x] === "1" && rows[y + 1][x + 1] === "1") {
      blocks.push(`${x},${y}`);
    }
  }
}

console.log(`size=${width}x${height} layers=${layers} frames=${frames} filledPixels=${filledPixels}`);
if (blocks.length) {
  console.log(`THIN_CHECK=FAIL  ${blocks.length} full 2x2 block(s) at: ${blocks.slice(0, 40).join(" | ")}`);
  console.log("-> a doubled corner: a pixel-perfect line must never fill a full 2x2 square");
  process.exit(1);
}
console.log("THIN_CHECK=PASS  no full 2x2 blocks");
