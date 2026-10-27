#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""素材合集打包（通用）—— 把目录里的 PNG 装进一张「格子对齐」的大图。

    python atlas-pack.py --src <素材目录> --tile 32 --maxw 1024 --out farm02_atlas
    python atlas-pack.py --src out --pattern "hand_*.png" --tile 16 --maxw 512 --out small
    python atlas-pack.py --src out --pattern "hand_*.png" --tile 32 --cols 8 --out grid8

校验（独立闸门，别只信打包脚本自己）：
    python atlas-check.py --index <outdir>/<out>.json          # 对齐/越界/重叠/逐件像素一致
    python atlas-check.py --selftest                           # 先证明闸门能判负

产出：
    <outdir>/<out>.png    合集图（每件占一个 tile 整数倍格子，行式装箱，按高度降序）
    <outdir>/<out>.json   坐标索引（id/x/y/w/h/gw/gh），引擎可直接切图
    <outdir>/<out>.lua    Aseprite 组装脚本（每件一层，层名 = 件名）—— 交给 run_script
"""
import argparse
import glob
import json
import os
import sys

from PIL import Image


def pack(src, pattern, tile, maxw, outdir, out, lua=True, id_prefix="", exclude="",
         ids_file="", cols=0):
    files = sorted(glob.glob(os.path.join(src, pattern)))
    ex = [e.strip() for e in exclude.split(",") if e.strip()]
    if ex:
        files = [p for p in files if not any(e in os.path.basename(p) for e in ex)]
    if ids_file:
        # 按清单精确取件（最稳：目录里混着预览图时不靠排除词猜）
        raw = json.load(open(ids_file, encoding="utf-8"))
        want = {m["id"] if isinstance(m, dict) else str(m) for m in raw}
        keep = []
        for p in files:
            base = os.path.splitext(os.path.basename(p))[0]
            oid = base[len(id_prefix):] if id_prefix and base.startswith(id_prefix) else base
            if oid in want:
                keep.append(p)
        files = keep
    if not files:
        print("no files matched: %s/%s" % (src, pattern))
        return 1
    items = []
    for p in files:
        base = os.path.basename(p)
        oid = os.path.splitext(base)[0]
        if id_prefix and oid.startswith(id_prefix):
            oid = oid[len(id_prefix):]
        im = Image.open(p).convert("RGBA")
        w, h = im.size
        gw = -(-w // tile) * tile          # 每件必须落在 tile 整数倍格子里
        gh = -(-h // tile) * tile
        items.append({"id": oid, "file": p, "w": w, "h": h, "gw": gw, "gh": gh})
    items.sort(key=lambda d: (-d["gh"], -d["gw"], d["id"]))

    x = y = rowH = 0
    for idx, it in enumerate(items):
        if idx > 0:
            if cols:
                wrap = (idx % cols == 0)          # 固定每行 cols 件
            else:
                wrap = (x + it["gw"] > maxw and x > 0)   # 按最大宽度折行
            if wrap:
                x = 0
                y += rowH
                rowH = 0
        it["x"], it["y"] = x, y
        x += it["gw"]
        rowH = max(rowH, it["gh"])
    aw = -(-max(it["x"] + it["gw"] for it in items) // tile) * tile
    ah = -(-(y + rowH) // tile) * tile

    atlas = Image.new("RGBA", (aw, ah), (0, 0, 0, 0))
    for it in items:
        atlas.paste(Image.open(it["file"]).convert("RGBA"), (it["x"], it["y"]))

    os.makedirs(outdir, exist_ok=True)
    png = os.path.join(outdir, out + ".png")
    atlas.save(png)
    rows = []
    for it in items:
        row = {k: it[k] for k in ("id", "x", "y", "w", "h", "gw", "gh")}
        row["file"] = os.path.basename(it["file"])   # 自描述：索引能反查源件（给校验器用）
        rows.append(row)
    pack_json = {"atlas": [aw, ah], "tile": tile, "count": len(items),
                 "src": src, "pattern": pattern, "cols": cols, "items": rows}
    pj = os.path.join(outdir, out + ".json")
    json.dump(pack_json, open(pj, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    lua_path = None
    if lua:
        lua_path = os.path.join(outdir, out + ".lua")
        lines = [
            "-- 由 atlas-pack.py 生成：按坐标索引组装 Aseprite 合集（每件一层，层名=件名）",
            'local ROOT = "%s/"' % os.path.dirname(os.path.abspath(png)).replace("\\", "/"),
            'local pack = json.decode(io.open(ROOT .. "%s.json", "r"):read("*a"))' % out,
            "local W, H = pack.atlas[1], pack.atlas[2]",
            "local spr = Sprite(W, H, ColorMode.RGB)",
            "local failed = {}",
            "for i, it in ipairs(pack.items) do",
            "  local layer = spr.layers[i] or spr:newLayer()",
            "  layer.name = it.id",
            '  local src = app.open(ROOT .. "src_" .. it.id .. ".png")',
            "  if src then",
            "    local cel = spr:newCel(layer, 1)",
            "    cel.image:drawImage(src.cels[1].image, it.x, it.y)",
            "    src:close()",
            "  else failed[#failed+1] = it.id end",
            "end",
            'spr:saveAs(ROOT .. "%s.aseprite")' % out,
            'spr:saveCopyAs(ROOT .. "%s.png")' % out,
            'io.write("__RESULT__" .. json.encode({success=true, data={',
            "  size = {W, H}, layers = #spr.layers, failed = failed}}))",
        ]
        open(lua_path, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
        # 同时把源图按 id 复制成 src_<id>.png，方便 Lua 侧直接读（避免路径转义问题）
        for it in items:
            Image.open(it["file"]).convert("RGBA").save(
                os.path.join(outdir, "src_%s.png" % it["id"]))

    print("atlas %dx%d  items=%d  tile=%d  %s" %
          (aw, ah, len(items), tile, ("cols=%d" % cols) if cols else ("maxw=%d" % maxw)))
    print("  png : %s" % png)
    print("  json: %s" % pj)
    if lua_path:
        print("  lua : %s   (交给 mcp__aseprite__run_script 执行)" % lua_path)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="素材目录")
    ap.add_argument("--pattern", default="*.png")
    ap.add_argument("--tile", type=int, default=32, help="格子对齐（16/32/64…）")
    ap.add_argument("--maxw", type=int, default=1024, help="合集最大宽度（按宽度折行）")
    ap.add_argument("--cols", type=int, default=0,
                    help="固定每行件数（如 8/10）——给了就按件数排列，忽略 --maxw")
    ap.add_argument("--outdir", default=None, help="输出目录（默认 = src）")
    ap.add_argument("--out", default="atlas")
    ap.add_argument("--id-prefix", default="")
    ap.add_argument("--exclude", default="", help="逗号分隔的排除子串（如 strip,prev）")
    ap.add_argument("--ids-file", default="",
                    help="只打这些件：一个 JSON（[{\"id\":...}, ...] 或 [\"id\", ...]）")
    ap.add_argument("--no-lua", action="store_true")
    a = ap.parse_args()
    return pack(a.src, a.pattern, a.tile, a.maxw,
                a.outdir or a.src, a.out, not a.no_lua, a.id_prefix, a.exclude,
                a.ids_file, a.cols)


if __name__ == "__main__":
    sys.exit(main())
