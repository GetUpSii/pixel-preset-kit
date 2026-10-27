# artifact-probe.py — 机器几何体复检：找出"轴对齐 + 平涂 + 硬边"的规则矩形块
# 判据（可复现、非主观）：rectangularity = area/bbox_area，border_cov = bbox 周界被该色填满的比例。
#   实心矩形: rectangularity=1.0, border_cov=1.0
#   内切圆  : rectangularity=pi/4≈0.785, border_cov≈0.785
#   有机形状: 两者都低
# 命中 = rectangularity>=0.85 且 border_cov>=0.90（架构/木板的直边也会命中 → 需人判语义）
import os
import sys
import argparse

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _vlib import load_rgba, label_mask, save_json  # noqa: E402


def luma(rgb):
    return 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]


def machine_rects(rgb, min_area=36, rect_thr=0.85, cov_thr=0.90, max_regions=400):
    h, w, _ = rgb.shape
    flat = rgb.reshape(-1, 3)
    cols, counts = np.unique(flat, axis=0, return_counts=True)
    out = []
    for c, n in zip(cols, counts):
        if n < min_area:
            continue
        mask = np.all(rgb == c[None, None, :], axis=-1)
        lab, nreg = label_mask(mask, conn=8)
        for rid in range(1, nreg + 1):
            ys, xs = np.nonzero(lab == rid)
            area = len(xs)
            if area < min_area:
                continue
            y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
            bw, bh = x1 - x0, y1 - y0
            if bw < 3 or bh < 3:
                continue
            bb = area / float(bw * bh)
            per = 2 * bw + 2 * bh - 4
            block = (lab[y0:y1, x0:x1] == rid)
            border = np.zeros_like(block)
            border[0, :] = True; border[-1, :] = True
            border[:, 0] = True; border[:, -1] = True
            cov = float((block & border).sum()) / float(per)
            if bb >= rect_thr and cov >= cov_thr:
                out.append({
                    "color": "#%02x%02x%02x" % (int(c[0]), int(c[1]), int(c[2])),
                    "area": int(area), "bbox": [int(x0), int(y0), int(bw), int(bh)],
                    "rectangularity": round(bb, 4), "border_cov": round(cov, 4),
                    "luma": round(float(luma(np.array(c, float).reshape(1, 1, 3))[0, 0]), 1),
                })
                if len(out) >= max_regions:
                    return out
    out.sort(key=lambda r: -r["area"])
    return out


def bright_clusters(rgb, thr, roi=None, min_area=4):
    """亮块聚类：找出 luma>=thr 的连通块（暗场景里的泛白/过曝块 = 生成伪影候选）。

    对抖动区也有效（不要求同色，只看亮度），补 machine_rects 在抖动图上的盲区。
    """
    L = luma(rgb)
    mask = L >= thr
    if roi:
        x0, y0, x1, y1 = roi
        keep = np.zeros_like(mask)
        keep[max(0, y0):y1, max(0, x0):x1] = True
        mask = mask & keep
    if not mask.any():
        return []
    lab, nreg = label_mask(mask, conn=8)
    out = []
    for rid in range(1, nreg + 1):
        ys, xs = np.nonzero(lab == rid)
        if len(xs) < min_area:
            continue
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        bw, bh = x1 - x0, y1 - y0
        sub = rgb[y0:y1, x0:x1][lab[y0:y1, x0:x1] == rid]
        out.append({
            "area": int(len(xs)), "bbox": [int(x0), int(y0), int(bw), int(bh)],
            "fill_ratio": round(len(xs) / float(bw * bh), 3),
            "luma_mean": round(float(L[lab == rid].mean()), 1),
            "colors": int(len(np.unique(sub, axis=0))),
        })
    out.sort(key=lambda r: -r["area"])
    return out


def roi_context(rgb, x0, y0, x1, y1, ring=6):
    """ROI 内部 vs 外圈环：色数 / 亮度标准差 / 平均亮度（判"平涂块压在有机场上"）。"""
    h, w, _ = rgb.shape
    L = luma(rgb)
    ix0, iy0, ix1, iy1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
    inner = rgb[iy0:iy1, ix0:ix1]
    ox0, oy0 = max(0, x0 - ring), max(0, y0 - ring)
    ox1, oy1 = min(w, x1 + ring), min(h, y1 + ring)
    outer = rgb[oy0:oy1, ox0:ox1]
    om = np.ones(outer.shape[:2], bool)
    om[iy0 - oy0:iy1 - oy0, ix0 - ox0:ix1 - ix0] = False
    rec = {
        "roi": [x0, y0, x1, y1],
        "inner_unique_colors": int(len(np.unique(inner.reshape(-1, 3), axis=0))),
        "inner_luma_mean": round(float(L[iy0:iy1, ix0:ix1].mean()), 1),
        "inner_luma_std": round(float(L[iy0:iy1, ix0:ix1].std()), 1),
        "ring_unique_colors": int(len(np.unique(outer[om].reshape(-1, 3), axis=0))),
        "ring_luma_mean": round(float(L[oy0:oy1, ox0:ox1][om].mean()), 1),
        "ring_luma_std": round(float(L[oy0:oy1, ox0:ox1][om].std()), 1),
    }
    rec["luma_contrast"] = round(abs(rec["inner_luma_mean"] - rec["ring_luma_mean"]), 1)
    rec["std_ratio"] = round(rec["inner_luma_std"] / (rec["ring_luma_std"] + 1e-6), 3)
    return rec


def selftest():
    checks = []

    def ck(name, got, want):
        checks.append((name, got, want, got == want))

    img = np.full((60, 60, 3), 40, np.int32)
    img[10:30, 12:40] = 200                      # 实心轴对齐矩形 20x28
    yy, xx = np.mgrid[0:60, 0:60]
    circ = (yy - 45) ** 2 + (xx - 45) ** 2 <= 100  # 半径 10 圆
    img[circ] = 150
    img[5, 55] = 90                               # 孤立小点（面积不足，不该命中）
    rects = machine_rects(img, min_area=36)
    boxes = [tuple(r["bbox"]) for r in rects]
    ck("rect.detected", (12, 10, 28, 20) in boxes, True)
    ck("circle.not_rect", any(b[0] == 35 and b[1] == 35 for b in boxes), False)
    ck("dot.too_small", any(r["area"] < 36 for r in rects), False)

    ctx = roi_context(img, 12, 10, 40, 30, ring=6)
    ck("ctx.flat_inner", ctx["inner_unique_colors"] == 1, True)
    ck("ctx.inner_brighter", ctx["inner_luma_mean"] > ctx["ring_luma_mean"], True)

    bad = [c for c in checks if not c[3]]
    for n, g, w, ok in checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {n}: got={g} want={w}")
    print(f"SELFTEST={'PASS' if not bad else 'FAIL'} checks={len(checks) - len(bad)}/{len(checks)}")
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="")
    ap.add_argument("--roi", default="", help="x0,y0,x1,y1（只报与该区域相交的命中）")
    ap.add_argument("--all", action="store_true", help="列出全部命中")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--bright", type=float, default=0.0,
                    help="改为：列出 luma>=该值的连通亮块（对抖动区有效）")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if not a.image:
        ap.error("--image required")
    rgba, _ = load_rgba(a.image)
    rgb = rgba[:, :, :3].astype(np.int32)
    roi = None
    if a.roi:
        roi = [int(v) for v in a.roi.split(",")]
    if a.bright > 0:
        cl = bright_clusters(rgb, a.bright, roi)
        print(f"IMAGE={os.path.basename(a.image)} bright_thr={a.bright} clusters={len(cl)}"
              + (f" roi={roi}" if roi else ""))
        for r in cl[:20]:
            print(f"  area={r['area']:5d} bbox={r['bbox']} fill={r['fill_ratio']:.3f} "
                  f"luma={r['luma_mean']} colors={r['colors']}")
        if a.json_out:
            save_json({"image": a.image, "bright_thr": a.bright, "clusters": cl}, a.json_out)
            print(f"JSON={a.json_out}")
        return
    rects = machine_rects(rgb)
    roi = None
    if a.roi:
        x0, y0, x1, y1 = (int(v) for v in a.roi.split(","))
        roi = [x0, y0, x1, y1]
        rects = [r for r in rects if not (
            r["bbox"][0] + r["bbox"][2] <= x0 or r["bbox"][0] >= x1 or
            r["bbox"][1] + r["bbox"][3] <= y0 or r["bbox"][1] >= y1)]
    print(f"IMAGE={os.path.basename(a.image)} hits={len(rects)}" + (f" roi={roi}" if roi else ""))
    for r in (rects if a.all or roi else rects[:12]):
        print(f"  {r['color']} area={r['area']:5d} bbox={r['bbox']} rect={r['rectangularity']:.3f} "
              f"bordercov={r['border_cov']:.3f} luma={r['luma']}")
    payload = {"image": a.image, "hits": len(rects), "rects": rects}
    if roi:
        payload["context"] = roi_context(rgb, *roi)
        c = payload["context"]
        print(f"  CONTEXT inner_cols={c['inner_unique_colors']} inner_luma={c['inner_luma_mean']} "
              f"std={c['inner_luma_std']} | ring_cols={c['ring_unique_colors']} ring_luma={c['ring_luma_mean']} "
              f"std={c['ring_luma_std']} | contrast={c['luma_contrast']} std_ratio={c['std_ratio']}")
    if a.json_out:
        save_json(payload, a.json_out)
        print(f"JSON={a.json_out}")


if __name__ == "__main__":
    main()
