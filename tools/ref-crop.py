# -*- coding: utf-8 -*-
"""ref-crop.py —— 参考图局部裁切 + 放大 + 标注拼图（"动手前先看真源"）

为什么需要：规格要按参考图落点，而整图缩略看不见像素。本工具按 objects.json 的
px 范围裁切指定物件、四周留 --pad、NEAREST 放大 --zoom 倍，横向拼成一张带标题的对照图。

用法:
  python ref-crop.py --ref <ref.png> --spec <objects.json> --out <out.png>
                     --ids fence_top,fence_left,hedge_top --zoom 6 --pad 16
  python ref-crop.py --selftest
"""
import argparse
import json
import os
import sys

from PIL import Image, ImageDraw

FONT = None


def label(draw, xy, text, fill=(255, 240, 120, 255)):
    draw.text(xy, text, fill=fill)


def selftest():
    """量尺自证：合成 64x64 图，在 (10,10)-(20,20) 放纯红，裁该框+pad=2、zoom=2，
    断言输出宽 = (10+2*2)*2 = 28，且红块左上角像素在输出里的位置正确。"""
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 255))
    for y in range(10, 21):
        for x in range(10, 21):
            im.putpixel((x, y), (255, 0, 0, 255))
    box = (10, 10, 21, 21)  # PIL 右开区间
    pad, zoom = 2, 2
    c = im.crop((box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad))
    assert c.size == (11 + 4, 11 + 4), c.size
    z = c.resize((c.size[0] * zoom, c.size[1] * zoom), Image.NEAREST)
    assert z.size == (30, 30), z.size
    assert z.getpixel((2 * 2, 2 * 2))[:3] == (255, 0, 0)
    assert z.getpixel((0, 0))[:3] == (0, 0, 0)
    print("selftest OK (4 assertions)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref")
    ap.add_argument("--spec")
    ap.add_argument("--out")
    ap.add_argument("--ids", default="")
    ap.add_argument("--zoom", type=int, default=6)
    ap.add_argument("--pad", type=int, default=16)
    ap.add_argument("--cols", type=int, default=3)
    ap.add_argument("--note", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    for k in ("ref", "spec", "out"):
        if not getattr(a, k):
            ap.error("--%s required" % k)

    ref = Image.open(a.ref).convert("RGBA")
    spec = json.load(open(a.spec, encoding="utf-8"))
    by_id = {o["id"]: o for o in spec["objects"]}
    # 允许用 "name:x0,y0,x1,y1" 这种临时框
    items = []
    # --ids 用 ';' 分隔条目（条目内的坐标用 ','）—— 两种分隔符必须不同，否则 1,2,3,4 会被当条目切碎
    for tok in [t for t in a.ids.split(";") if t.strip()]:
        tok = tok.strip()
        if tok in by_id:
            o = by_id[tok]
            items.append((tok, tuple(o["px"])))
        elif ":" in tok:
            nm, box = tok.split(":")
            items.append((nm, tuple(int(v) for v in box.split(","))))
        else:
            print("skip unknown id %s" % tok, file=sys.stderr)

    tiles = []
    for nm, (x0, y0, x1, y1) in items:
        cx0, cy0 = max(0, x0 - a.pad), max(0, y0 - a.pad)
        cx1, cy1 = min(ref.size[0], x1 + a.pad), min(ref.size[1], y1 + a.pad)
        c = ref.crop((cx0, cy0, cx1, cy1))
        c = c.resize((c.size[0] * a.zoom, c.size[1] * a.zoom), Image.NEAREST)
        tiles.append(("%s  box=%d,%d,%d,%d  %dx%d  x%d" % (nm, x0, y0, x1, y1, x1 - x0, y1 - y0, a.zoom), c))

    gap = 8
    head = 16
    cols = max(1, min(a.cols, len(tiles)))
    rows = (len(tiles) + cols - 1) // cols
    colw = []
    for c in range(cols):
        colw.append(max([t[1].size[0] for i, t in enumerate(tiles) if i % cols == c] or [1]))
    rowh = []
    for r in range(rows):
        rowh.append(max([t[1].size[1] for i, t in enumerate(tiles) if i // cols == r] or [1]))
    W = sum(colw) + gap * (cols + 1)
    H = sum(rowh) + (head + gap) * rows + gap
    out = Image.new("RGBA", (W, H), (20, 20, 24, 255))
    d = ImageDraw.Draw(out)
    y = gap
    for r in range(rows):
        x = gap
        for c in range(cols):
            i = r * cols + c
            if i < len(tiles):
                lab, img = tiles[i]
                label(d, (x, y), lab)
                out.alpha_composite(img, (x, y + head))
            x += colw[c] + gap
        y += rowh[r] + head + gap
    if a.note:
        d.text((gap, H - 14), a.note, fill=(160, 220, 255, 255))
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    out.save(a.out)
    print("items=%d -> %s (%dx%d)" % (len(tiles), a.out, out.size[0], out.size[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
