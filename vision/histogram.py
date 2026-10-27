# histogram.py — 原语 3：调色板候选 + 色域覆盖 + 映射误差（"这个色板够不够用"用数字回答）
#
# 直接对应实测过的坑：道具硬套场景 48 色 => 误差 59.3 / 最差 370；并集板 64 色 => 13.2 / 79.4。
# 现在这是一条命令，而不是一段手写脚本。
#
# 用法：
#   python histogram.py --image prop_03_sleeping_cat.png --palette apothecary48.gpl
#   python histogram.py --image prop_03_sleeping_cat.png --palette apothecary48.gpl --union 16
#   python histogram.py --image scene.png --colors 48 --export-gpl out/pal48.gpl
#
# 产物（默认 out/vision/）：<tag>.json（全部数字）、<tag>.gpl（可选 GIMP 调色板）、
#   <tag>_palette.png（色卡）、<tag>_palette_union.png（并集色卡，标出追加色）
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _vlib as V  # noqa: E402


def load_palette(path, palette_max=256):
    """.gpl 直读；图片则取自身唯一色，超过上限就 median-cut 到上限。"""
    low = path.lower()
    if low.endswith(".gpl") or low.endswith(".pal"):
        pal = V.read_gpl(path)
        return pal, {"source": os.path.abspath(path), "method": "gpl", "size": int(len(pal))}
    rgba, size = V.load_rgba(path)
    px = V.visible_pixels(rgba)
    uniq = np.unique(px.reshape(-1, 3), axis=0)
    if len(uniq) > palette_max:
        pal = V.median_cut(px, palette_max)
        meta = {"source": os.path.abspath(path), "method": "median_cut(%d) of %d unique" % (palette_max, len(uniq)),
                "size": int(len(pal))}
    else:
        pal = uniq.astype(np.int32)
        meta = {"source": os.path.abspath(path), "method": "unique_colors", "size": int(len(pal))}
    meta["image_size"] = list(size)
    return pal, meta


def run(images, palette=None, colors=None, union=0, resid_tol=30.0, tol=30.0, alpha_thr=127,
        out_dir=None, tag=None, top=24, bins=16, palette_max=256, export_gpl=None):
    srcs = []
    pooled = []
    for p in images:
        rgba, size = V.load_rgba(p)
        px = V.visible_pixels(rgba, alpha_thr)
        pooled.append(px)
        uq, topc = V.unique_colors(px, top)
        srcs.append({"path": os.path.abspath(p), "size": list(size),
                     "visible_px": int(len(px)), "unique_colors": uq,
                     "top_colors": topc, "channels": V.channel_report(px, bins)})
    pool = np.concatenate(pooled) if pooled else np.zeros((0, 3), np.uint8)

    pal, pmeta = (None, None)
    if palette:
        pal, pmeta = load_palette(palette, palette_max)
    elif colors:
        pal = V.median_cut(pool, colors)
        pmeta = {"source": "median_cut", "method": "median_cut(%d) of pooled %d px" % (colors, len(pool)),
                 "size": int(len(pal))}

    # 映射误差 + 覆盖率
    def measure(p):
        if pal is None or len(pal) == 0 or len(p) == 0:
            return None
        e = V.nearest_error(p, pal)
        return {"error": V.err_stats(e), "coverage_pct": round(float((e <= tol).mean()) * 100, 2), "tol": tol}

    for s, px in zip(srcs, pooled):
        m = measure(px)
        if m:
            s.update(m)

    union_info = None
    pal_union = pal
    if union and pal is not None and len(pool):
        e = V.nearest_error(pool, pal)
        resid = pool[e > resid_tol]
        extra = V.median_cut(resid, union) if len(resid) else np.zeros((0, 3), np.uint8)
        pal_union = np.vstack([pal, extra]).astype(np.int32) if len(extra) else pal
        per_src = []
        for s, px in zip(srcs, pooled):
            eu = V.nearest_error(px, pal_union)
            per_src.append({"path": s["path"], "error": V.err_stats(eu),
                            "coverage_pct": round(float((eu <= tol).mean()) * 100, 2)})
        union_info = {"extra_n": int(len(extra)), "resid_px": int(len(resid)), "resid_tol": resid_tol,
                      "palette_size": int(len(pal_union)), "sources": per_src}
        for s, u in zip(srcs, per_src):
            s["error_with_union"] = u["error"]
            s["coverage_pct_with_union"] = u["coverage_pct"]

    out_dir = out_dir or V.default_out_dir()
    os.makedirs(out_dir, exist_ok=True)
    if tag is None:
        tag = "hist_" + "_".join(os.path.splitext(os.path.basename(p))[0] for p in images[:2])
    paths = {}
    final = pal_union if (union_info and pal_union is not None) else pal
    if final is not None and len(final):
        gpl_path = export_gpl or os.path.join(out_dir, tag + ".gpl")
        tags = None
        if union_info:
            tags = ["base-%02d" % i for i in range(len(pal))] + ["extra-%02d" % i for i in range(len(final) - len(pal))]
        V.write_gpl(final, gpl_path, name="%s (%d colours)" % (tag, len(final)), tags=tags)
        paths["gpl"] = os.path.abspath(gpl_path)
        paths["swatch_png"] = os.path.join(out_dir, tag + "_palette.png")
        V.swatch_sheet(final, paths["swatch_png"], title="%s  %d colours" % (tag, len(final)))
        if union_info:
            paths["swatch_union_png"] = os.path.join(out_dir, tag + "_palette_union.png")
            V.swatch_sheet(final, paths["swatch_union_png"],
                           title="union: %d base + %d extra = %d" % (len(pal), union_info["extra_n"], len(final)),
                           tags=tags)

    report = {"kind": "histogram", "sources": srcs, "palette": pmeta, "pooled_visible_px": int(len(pool)),
              "pooled_unique_colors": int(len(np.unique(pool.reshape(-1, 3), axis=0))) if len(pool) else 0,
              "union": union_info, "coverage_tol": tol, "outputs": paths,
              "palette_fingerprint": V.fingerprint_rgb(final[:, None, :].astype(np.uint8)) if final is not None and len(final) else None}
    jp = os.path.join(out_dir, tag + ".json")
    paths["json"] = jp                      # 必须在保存前赋值：report["outputs"] 与 paths 是同一个 dict
    V.save_json(report, jp)

    print("histogram  sources=%d  pooled_visible=%d px  unique=%d" % (len(srcs), len(pool), report["pooled_unique_colors"]))
    for s in srcs:
        print("  %s  %dx%d  visible=%d  unique=%d" % (os.path.basename(s["path"]), s["size"][0], s["size"][1],
                                                      s["visible_px"], s["unique_colors"]))
        if s.get("error"):
            print("     top colours: %s" % ", ".join("%s x%d" % (t["hex"], t["count"]) for t in s["top_colors"][:6]))
            print("     channel ranges  r[%d..%d] g[%d..%d] b[%d..%d]"
                  % (s["channels"]["r"]["min"], s["channels"]["r"]["max"], s["channels"]["g"]["min"],
                     s["channels"]["g"]["max"], s["channels"]["b"]["min"], s["channels"]["b"]["max"]))
    if pmeta:
        print("palette: %d colours  (%s)" % (pmeta["size"], pmeta["method"]))
    for s in srcs:
        if s.get("error"):
            e = s["error"]
            print("  map %-32s  mean=%6.1f p95=%6.1f max=%6.1f  coverage(<=%g)=%5.1f%%"
                  % (os.path.basename(s["path"]), e["mean"], e["p95"], e["max"], tol, s["coverage_pct"]))
    if union_info:
        print("union: base %d + extra %d (resid>%g px=%d) = %d colours"
              % (len(pal), union_info["extra_n"], resid_tol, union_info["resid_px"], union_info["palette_size"]))
        for s, u in zip(srcs, union_info["sources"]):
            e = u["error"]
            print("  map %-32s  mean=%6.1f p95=%6.1f max=%6.1f  coverage(<=%g)=%5.1f%%  [union]"
                  % (os.path.basename(s["path"]), e["mean"], e["p95"], e["max"], tol, u["coverage_pct"]))
    print("json=%s" % jp)
    for k in ("gpl", "swatch_png", "swatch_union_png"):
        if k in paths:
            print("%s=%s" % (k, paths[k]))
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description="palette candidates + gamut coverage + mapping error (P0 primitive)")
    ap.add_argument("--image", action="append", required=True, help="repeatable")
    ap.add_argument("--palette", default=None, help=".gpl/.pal or an image to derive a palette from")
    ap.add_argument("--colors", type=int, default=None, help="median-cut N colours from the image itself")
    ap.add_argument("--union", type=int, default=0, help="append N colours from residual pixels (needs --palette)")
    ap.add_argument("--resid-tol", type=float, default=30.0)
    ap.add_argument("--tol", type=float, default=30.0, help="coverage tolerance in RGB euclidean distance")
    ap.add_argument("--alpha-thr", type=int, default=127)
    ap.add_argument("--palette-max", type=int, default=256)
    ap.add_argument("--top", type=int, default=24)
    ap.add_argument("--bins", type=int, default=16)
    ap.add_argument("--export-gpl", default=None)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--tag", default=None)
    a = ap.parse_args(argv)
    run(a.image, a.palette, a.colors, a.union, a.resid_tol, a.tol, a.alpha_thr, a.out_dir, a.tag,
        a.top, a.bins, a.palette_max, a.export_gpl)
    return 0


if __name__ == "__main__":
    sys.exit(main())
