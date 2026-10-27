# -*- coding: utf-8 -*-
"""grid-overlay.py —— 参考图叠 32px 网格 + 行列号 + 物件框 + （可选）手工地形图

用途：地形落格必须"按示意图读"，不能靠颜色自动判（实测 ECO-FARM 的绿招牌 G 族 0.99、
废料堆 O+N 0.87，都会把"草/路"判错）。本工具把网格与物件框叠上去，让人（或 AI）能逐格读。

用法:
  python grid-overlay.py --ref <jpg> --out o.png [--scale 2] [--map-file cells.txt] [--spec objects.json]
  python grid-overlay.py --selftest
（--ref/--spec 也可用环境变量 PIXEL_GRID_REF / PIXEL_GRID_SPEC 提供默认值）
"""
import argparse
import json
import os
import sys

from PIL import Image, ImageDraw

# Defaults come from the environment so this script carries no machine-specific paths.
#   PIXEL_GRID_REF   reference image to overlay the grid on
#   PIXEL_GRID_SPEC  optional objects.json for object boxes
REF = os.environ.get("PIXEL_GRID_REF", "")
SPEC = os.environ.get("PIXEL_GRID_SPEC", "")
T = 32
W, H = 1024, 576
COLOR = {".": (255, 255, 255, 40), "G": (60, 220, 90, 110), "R": (90, 160, 255, 130)}


def selftest():
    """量尺自证：4×4 棋盘上叠 2px 网格，断言网格线落在 x=0,2 且格内像素未被覆盖。"""
    im = Image.new("RGBA", (4, 4), (0, 0, 0, 255))
    d = ImageDraw.Draw(im, "RGBA")
    d.line([(2, 0), (2, 3)], fill=(255, 0, 0, 255))
    assert im.getpixel((2, 1))[:3] == (255, 0, 0)
    assert im.getpixel((1, 1))[:3] == (0, 0, 0)
    print("selftest OK (2 assertions)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default=REF)
    ap.add_argument("--spec", default=SPEC)
    ap.add_argument("--out")
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--map-file", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.out:
        ap.error("--out required")
    if not a.ref:
        ap.error("--ref required (or set the PIXEL_GRID_REF environment variable)")

    src = Image.open(a.ref).convert("RGB")
    canvas = Image.new("RGB", (W, H), (196, 157, 104))
    canvas.paste(src.crop((0, 0, min(W, src.width), min(H, src.height))), (0, 0))
    if src.height < H:
        strip = canvas.crop((0, src.height - 1, W, src.height)).resize((W, H - src.height))
        canvas.paste(strip, (0, src.height))
    canvas = canvas.convert("RGBA")

    # 手工地形图着色（. = 土 / G = 草 / R = 路）
    if a.map_file and os.path.exists(a.map_file):
        lines = [ln.rstrip("\n") for ln in open(a.map_file, encoding="utf-8") if ln.strip()]
        d = ImageDraw.Draw(canvas, "RGBA")
        for gy, ln in enumerate(lines[:H // T]):
            for gx, ch in enumerate(ln[:W // T]):
                if ch in ("G", "R"):
                    d.rectangle([gx * T, gy * T, gx * T + T - 1, gy * T + T - 1], fill=COLOR[ch])

    d = ImageDraw.Draw(canvas, "RGBA")
    for gx in range(W // T + 1):
        d.line([(gx * T, 0), (gx * T, H)], fill=(255, 255, 255, 90))
    for gy in range(H // T + 1):
        d.line([(0, gy * T), (W, gy * T)], fill=(255, 255, 255, 90))

    # 物件框（objects.json），便于看清"路/草"与建物的相对位置
    try:
        spec = json.load(open(a.spec, encoding="utf-8"))
        for o in spec["objects"]:
            if o.get("cat") in ("ground", "road"):
                continue
            x0, y0, x1, y1 = o["px"]
            d.rectangle([x0, y0, x1 - 1, y1 - 1], outline=(255, 220, 0, 200))
    except Exception as e:                                  # 只读辅助，失败不该毁掉出图
        print("spec overlay skipped: %s" % e)

    s = a.scale
    big = canvas.convert("RGB").resize((W * s, H * s), Image.NEAREST)
    d2 = ImageDraw.Draw(big)
    for gx in range(W // T):
        d2.text((gx * T * s + 3, 2), str(gx), fill=(255, 80, 80))
    for gy in range(H // T):
        d2.text((2, gy * T * s + 3), str(gy), fill=(80, 255, 80))
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    big.save(a.out)
    print("overlay -> %s (%dx%d)" % (a.out, big.size[0], big.size[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
