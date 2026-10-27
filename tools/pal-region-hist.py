# -*- coding: utf-8 -*-
"""pal-region-hist.py —— 参考图某个矩形区域 → 调色板索引直方图（占比排序）

为什么需要：定 ramp 阶不能靠眼看。要问"这块土到底用的是板上哪几号色、各占多少"，
只有把区域逐像素吸附到 .gpl 再数占比才能回答。距离用 CIE Lab（sRGB→XYZ→Lab，D65），
不用裸 RGB——RGB 欧氏距离在暗部会把色阶判错。

用法:
  python pal-region-hist.py --image ref.jpg --palette p.gpl --roi 60,380,280,560 [--roi ...] [--top 10] [--label soil]
  python pal-region-hist.py --selftest
"""
import argparse
import sys

import numpy as np
from PIL import Image

# palette 索引 -> 名称（--gpl 里读）


def srgb_to_lab(rgb):
    """rgb: (...,3) uint8 -> Lab float"""
    c = rgb.astype(np.float64) / 255.0
    m = c > 0.04045
    c = np.where(m, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    M = np.array([[0.4124564, 0.3575761, 0.1804375],
                  [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    xyz = c @ M.T
    xyz = xyz / np.array([0.95047, 1.0, 1.08883])
    d = 6.0 / 29.0
    f = np.where(xyz > d ** 3, np.cbrt(xyz), xyz / (3 * d * d) + 4.0 / 29.0)
    L = 116 * f[..., 1] - 16
    a = 500 * (f[..., 0] - f[..., 1])
    b = 200 * (f[..., 1] - f[..., 2])
    return np.stack([L, a, b], -1)


def read_gpl(path):
    names, cols = [], []
    for ln in open(path, encoding="utf-8", errors="replace"):
        ln = ln.rstrip("\n")
        if not ln or ln[0].isalpha() or ln.startswith("#"):
            continue
        p = ln.split(None, 3)
        if len(p) >= 3 and all(x.lstrip("-").isdigit() for x in p[:3]):
            cols.append([int(p[0]), int(p[1]), int(p[2])])
            names.append(p[3] if len(p) > 3 else "")
    return np.array(cols, np.uint8), names


def nearest_idx(lab_px, lab_pal):
    """分块算最近邻居，避免 N*P 内存爆"""
    out = np.empty(len(lab_px), np.int32)
    step = 20000
    for i in range(0, len(lab_px), step):
        chunk = lab_px[i:i + step]
        d = ((chunk[:, None, :] - lab_pal[None, :, :]) ** 2).sum(-1)
        out[i:i + step] = d.argmin(1)
    return out


def selftest():
    """量尺自证：造 3 色小图 + 同 3 色板，断言 100% 命中且索引正确；
    再造一个"比 A 更接近 B"的偏色，断言它被判到 B 而不是 A。"""
    pal = np.array([[200, 150, 100], [100, 160, 90], [30, 30, 30]], np.uint8)
    lp = srgb_to_lab(pal)
    img = np.zeros((3, 10, 3), np.uint8)
    img[0, :] = (200, 150, 100)
    img[1, :] = (100, 160, 90)
    img[2, :] = (30, 30, 30)
    idx = nearest_idx(srgb_to_lab(img.reshape(-1, 3)), lp)
    assert list(idx[:10]) == [0] * 10, idx[:10]
    assert list(idx[10:20]) == [1] * 10
    assert list(idx[20:]) == [2] * 10
    # 偏色：把 1 号色压暗 10% 后仍应判回 1 号（"更接近谁"必须由构造保证，不能靠猜）
    probe = srgb_to_lab((pal[1].astype(np.float64) * 0.9).astype(np.uint8)[None, :])
    assert nearest_idx(probe, lp)[0] == 1, "darkened palette colour must map back to itself"
    print("selftest OK (4 assertions)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image")
    ap.add_argument("--palette")
    ap.add_argument("--roi", action="append", default=[])
    ap.add_argument("--label", action="append", default=[])
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.image or not a.palette or not a.roi:
        ap.error("--image/--palette/--roi required")

    im = Image.open(a.image).convert("RGB")
    pal, names = read_gpl(a.palette)
    lab_pal = srgb_to_lab(pal)
    for i, roi in enumerate(a.roi):
        x0, y0, x1, y1 = (int(v) for v in roi.split(","))
        arr = np.asarray(im.crop((x0, y0, x1, y1))).reshape(-1, 3)
        idx = nearest_idx(srgb_to_lab(arr), lab_pal)
        n = len(idx)
        cnt = np.bincount(idx, minlength=len(pal))
        order = np.argsort(-cnt)
        lbl = a.label[i] if i < len(a.label) else "roi%d" % i
        print("=== %s  roi=%s  px=%d ===" % (lbl, roi, n))
        for j in order[:a.top]:
            if cnt[j] == 0:
                continue
            r, g, b = pal[j]
            print("  idx=%-3d %6.2f%%  #%02x%02x%02x  %s" % (j, 100.0 * cnt[j] / n, r, g, b, names[j]))
        # 未上板量（最近距离 p90）
        d = np.sqrt(((srgb_to_lab(arr) - lab_pal[idx]) ** 2).sum(-1))
        print("  resid: mean=%.2f p90=%.2f max=%.2f (Lab dE)" % (d.mean(), np.percentile(d, 90), d.max()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
