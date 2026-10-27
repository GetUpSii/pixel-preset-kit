# image-diff.py — 原语 1：把"两张图的差异"量化出来（热图 + 连通区域 bbox），不再靠肉眼估坐标
#
# 用法：
#   python image-diff.py --a props/prop_01_cauldron.png --b props_clean/prop_01_cauldron.png
#   python image-diff.py --a a.png --b b.png --threshold 6 --include-alpha --roi 10,10,200,150
#
# 产物（默认写 out/vision/）：
#   <tag>_heat.png     差异热图（黑=相同，蓝->青->黄->红 = 幅度递增）
#   <tag>_overlay.png  压暗的底图 + 差异像素按幅度着色（一眼看出"改在哪儿"）
#   <tag>_mask.png     二值差异掩膜（可直接喂 region-stats.py / 做掩膜编辑）
#   <tag>.json         连通区域清单：area / bbox / centroid / 幅度 mean,max + 全图统计 + 指纹
import argparse
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _vlib as V  # noqa: E402


def run(a_path, b_path, out_dir=None, tag=None, threshold=8, metric="rgb", include_alpha=False,
        roi=None, min_area=1, conn=8, render=True, top=10):
    a, size_a = V.load_rgba(a_path)
    b, size_b = V.load_rgba(b_path)
    if size_a != size_b:
        raise ValueError("image size mismatch: %s=%s vs %s=%s (resize/crop first, no silent fallback)"
                         % (os.path.basename(a_path), size_a, os.path.basename(b_path), size_b))
    h, w = size_a[1], size_a[0]
    sy, sx = V.roi_slice(roi, w, h)
    a_s, b_s = a[sy, sx], b[sy, sx]

    d = V.diff_maps(a_s, b_s, metric=metric, include_alpha=include_alpha)
    mask = d >= int(threshold)
    recs = V.region_records(mask, conn=conn, weight=d, min_area=min_area)
    changed = int(mask.sum())
    total = int(mask.size)

    if tag is None:
        tag = "diff_%s_vs_%s" % (os.path.splitext(os.path.basename(a_path))[0],
                                 os.path.splitext(os.path.basename(b_path))[0])
    out_dir = out_dir or V.default_out_dir()
    os.makedirs(out_dir, exist_ok=True)
    paths = {}
    if render:
        heat = V.heat_rgb(d)
        paths["heat"] = os.path.join(out_dir, tag + "_heat.png")
        V.save_rgb(heat, paths["heat"])
        paths["overlay"] = os.path.join(out_dir, tag + "_overlay.png")
        V.save_rgb(V.overlay_rgb(a_s, d, threshold), paths["overlay"])
        paths["mask"] = os.path.join(out_dir, tag + "_mask.png")
        Image.fromarray(np.where(mask, 255, 0).astype(np.uint8), "L").save(paths["mask"])
        heat_fp = V.fingerprint_rgb(heat)
    else:
        heat_fp = None

    report = {
        "kind": "image-diff",
        "a": os.path.abspath(a_path), "b": os.path.abspath(b_path),
        "size": [w, h], "roi": roi, "threshold": int(threshold), "metric": metric,
        "include_alpha": bool(include_alpha), "conn": conn, "min_area": int(min_area),
        "total_pixels": total, "changed_pixels": changed,
        "changed_share": round(changed / total, 5) if total else 0.0,
        "max_delta": int(d.max()) if d.size else 0,
        "mean_delta_over_changed": round(float(d[mask].mean()), 2) if changed else 0.0,
        "region_count": len(recs),
        "regions": recs,
        "diff_heat_fingerprint": heat_fp,
        "outputs": paths,
    }
    jp = os.path.join(out_dir, tag + ".json")
    paths["json"] = jp                      # 必须在保存前赋值：report["outputs"] 与 paths 是同一个 dict
    V.save_json(report, jp)

    print("image-diff  a=%s  b=%s  %dx%d  metric=%s%s%s"
          % (os.path.basename(a_path), os.path.basename(b_path), w, h, metric,
             " +alpha" if include_alpha else "", ("  roi=" + str(roi)) if roi else ""))
    print("threshold=%d  changed=%d/%d (%.3f%%)  max_delta=%d  regions=%d"
          % (threshold, changed, total, 100.0 * changed / total if total else 0.0, report["max_delta"], len(recs)))
    for r in recs[:top]:
        print("  r%-3d area=%-7d bbox=[%4d,%4d,%4d,%4d]  %dx%d  centroid=(%.1f,%.1f)  delta mean=%.1f max=%.0f"
              % (r["id"], r["area"], r["bbox"][0], r["bbox"][1], r["bbox"][2], r["bbox"][3],
                 r["w"], r["h"], r["centroid"][0], r["centroid"][1], r.get("weight_mean", 0), r.get("weight_max", 0)))
    if len(recs) > top:
        print("  ... %d more regions (see json)" % (len(recs) - top))
    print("json=%s" % jp)
    if render:
        print("heat=%s  overlay=%s  mask=%s  fingerprint=%d"
              % (paths["heat"], paths["overlay"], paths["mask"], heat_fp))
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description="visual diff -> heatmap + connected regions (P0 primitive)")
    ap.add_argument("--a", required=True)
    ap.add_argument("--b", required=True)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--tag", default=None)
    ap.add_argument("--threshold", type=int, default=8, help="per-pixel magnitude to count as changed (0..255)")
    ap.add_argument("--metric", choices=["rgb", "luma"], default="rgb")
    ap.add_argument("--include-alpha", action="store_true", help="count alpha deltas (transparent<->opaque)")
    ap.add_argument("--roi", default=None, help="x0,y0,x1,y1 inclusive; default whole image")
    ap.add_argument("--min-area", type=int, default=1)
    ap.add_argument("--conn", type=int, choices=[4, 8], default=8)
    ap.add_argument("--no-render", action="store_true", help="skip png outputs, json only")
    ap.add_argument("--top", type=int, default=10)
    a = ap.parse_args(argv)
    run(a.a, a.b, a.out_dir, a.tag, a.threshold, a.metric, a.include_alpha, a.roi,
        a.min_area, a.conn, not a.no_render, a.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
