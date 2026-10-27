# make-evidence.py — 出"放大证据图"：任意区域裁切 + 整数倍最近邻放大（供人眼判断）
# 用法：
#   python make-evidence.py --src A.png --out A_x8.png --zoom 8
#   python make-evidence.py --src A.png --out A_roi_x8.png --roi 80,300,144,364 --zoom 8
import os
import sys
import argparse

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _vlib import load_rgba  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--zoom", type=int, default=8)
    ap.add_argument("--roi", default="", help="x0,y0,x1,y1（原图绝对坐标，右/下开区间）")
    ap.add_argument("--pair", default="", help="第二张图；与 --src 水平并排（before/after 证据）")
    ap.add_argument("--gap", type=int, default=8)
    a = ap.parse_args()

    rgba, _ = load_rgba(a.src)
    if a.roi:
        x0, y0, x1, y1 = (int(v) for v in a.roi.split(","))
        rgba = rgba[y0:y1, x0:x1]
    if a.pair:
        b, _bs = load_rgba(a.pair)
        if a.roi:
            x0, y0, x1, y1 = (int(v) for v in a.roi.split(","))
            b = b[y0:y1, x0:x1]
        h = max(rgba.shape[0], b.shape[0])
        sheet = np.zeros((h, rgba.shape[1] + a.gap + b.shape[1], 4), np.uint8)
        sheet[:, :, 3] = 255
        sheet[: rgba.shape[0], : rgba.shape[1]] = rgba
        sheet[: b.shape[0], rgba.shape[1] + a.gap:] = b
        rgba = sheet
    z = max(1, a.zoom)
    big = np.repeat(np.repeat(rgba, z, axis=0), z, axis=1)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    Image.fromarray(big, "RGBA").save(a.out)
    print(f"OUT={a.out} src_roi={rgba.shape[1]}x{rgba.shape[0]} zoom={z} -> {big.shape[1]}x{big.shape[0]}")


if __name__ == "__main__":
    main()
