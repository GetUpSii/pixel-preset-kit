# region-stats.py — 原语 2：把一个 mask 变成可用的数字（面积 / bbox / 质心 / 边界坐标 / 白边厚度）
#
# 输入可以是：带 alpha 的成品图（--mode alpha）、纯色背景上的素材表（--mode bg）、
#            某个颜色的选区（--mode color）、或另一条命令产出的 mask.png（--mode alpha & 全白不透）。
# 用法：
#   python region-stats.py --image prop_01_cauldron.png --fringe-white
#   python region-stats.py --image gen_propsheet_2k.png --mode bg --roi 0,0,512,341 --crop-out --soft-alpha
#   python region-stats.py --image scene/props/prop_01_cauldron.png --fringe-white --write-defringed
#
# 产物（默认 out/vision/）：
#   <tag>.json          逐区域：area/bbox/质心/边界点数/白边厚度与逐层白占比
#   <tag>_mask.png      掩膜；<tag>_boundary.png 边界叠加在原图上（品红）
#   <tag>_edges.txt     每个区域"有邻居在区域外"的边界像素坐标（严格集合）
#   <tag>_contours.txt  每个区域的 8 连通有序外轮廓（best-effort 多边形逼近）
#   <tag>_crops/*.png   逐区域裁切（--crop-out；配 --soft-alpha 即 cut-props.py 的等效做法）
#   <tag>_defringed.png 按实测白边厚度外侧腐蚀后的 RGBA（--fringe-white --write-defringed）
import argparse
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _vlib as V  # noqa: E402


def build_mask(rgba, mode="auto", bg="#auto", t0=18.0, t1=45.0, color=None, tol=40.0,
               alpha_thr=127, bg_px=6):
    """-> (mask, meta, dist)。meta 必须是 JSON 可序列化的（ndarray 一律不进 meta，否则报告写不出来）。"""
    rgb = rgba[:, :, :3]
    meta = {}
    dist = None
    if mode == "auto":
        mode = "alpha" if bool((rgba[:, :, 3] < alpha_thr).any()) else "bg"
    if mode == "alpha":
        mask = rgba[:, :, 3] > alpha_thr
        meta = {"mode": "alpha", "alpha_thr": alpha_thr}
    elif mode == "bg":
        bgv = str(bg if bg is not None else "").strip().lstrip("#").lower()
        if bgv in ("", "auto"):                 # argparse 默认是 "#auto"，两种写法都接受
            b = V.estimate_bg_ring(rgb, bg_px)
            meta["bg_source"] = "ring_median(%dpx)" % bg_px
        else:
            b = np.array(V.parse_hex(bg), np.float64)
            meta["bg_source"] = "explicit"
        dist = np.sqrt(((rgb.astype(np.int32) - b.astype(np.int32)) ** 2).sum(-1).astype(np.int32))
        mask = dist >= t1
        meta.update({"mode": "bg", "bg": [float(v) for v in b], "t0": t0, "t1": t1})
    elif mode == "color":
        if not color:
            raise ValueError("--mode color needs --color #rrggbb")
        c = np.array(V.parse_hex(color), np.int32)
        dist = np.sqrt(((rgb.astype(np.int32) - c) ** 2).sum(-1).astype(np.int32))
        mask = dist <= tol
        meta = {"mode": "color", "color": V.hexc(c), "tol": tol}
    else:
        raise ValueError("unknown mode %r" % mode)
    meta["coverage_px"] = int(mask.sum())
    meta["coverage_share"] = round(float(mask.sum()) / mask.size, 5)
    return mask, meta, dist


def run(image, mode="auto", out_dir=None, tag=None, roi=None, bg="#auto", t0=18.0, t1=45.0, color=None,
        tol=40.0, alpha_thr=127, bg_px=6, min_area=1, conn=8, fringe=False, near_white=232,
        maxd=6, contour=False, boundary=True, dump_regions=20, crop_out=False, soft_alpha=False,
        crop_pad=3, write_defringed=False, top=15):
    rgba_full, (w0, h0) = V.load_rgba(image)
    # --roi：只在窗口内做一切计算（素材表逐格抠图的正解）；输出坐标一律换算回**原图绝对坐标**
    sy, sx = V.roi_slice(roi, w0, h0)
    rgba = np.ascontiguousarray(rgba_full[sy, sx])
    x_off, y_off = int(sx.start), int(sy.start)
    h, w = rgba.shape[0], rgba.shape[1]
    mask, meta, dist = build_mask(rgba, mode, bg, t0, t1, color, tol, alpha_thr, bg_px)
    recs = V.region_records(mask, conn=conn, min_area=min_area)

    out_dir = out_dir or V.default_out_dir()
    os.makedirs(out_dir, exist_ok=True)
    if tag is None:
        tag = "regions_" + os.path.splitext(os.path.basename(image))[0]
    paths = {}

    # 边界可视化：严格边缘环（品红）叠在压暗的原图上
    ring_all = V.edge_ring(mask)
    if boundary:
        base = rgba[:, :, :3].astype(np.float64) * 0.45 + 14.0
        paint = np.zeros_like(base)
        paint[...] = (255, 0, 220)
        out = np.where(ring_all[:, :, None], paint, base)
        paths["boundary_png"] = os.path.join(out_dir, tag + "_boundary.png")
        V.save_rgb(np.clip(out, 0, 255).astype(np.uint8), paths["boundary_png"])
    paths["mask_png"] = os.path.join(out_dir, tag + "_mask.png")
    Image.fromarray(np.where(mask, 255, 0).astype(np.uint8), "L").save(paths["mask_png"])
    if meta.get("mode") == "alpha" or bool((rgba[:, :, 3] < alpha_thr).any()):
        paths["checker_png"] = os.path.join(out_dir, tag + "_check.png")
        V.checker_overlay(rgba, paths["checker_png"])

    labels, _ = V.label_mask(mask, conn=conn)

    # 逐区域白边测厚（区域 mask **在整图上**算层距：按 bbox 裁切会让"到透明区的距离"失去上下文，
    # 层距恒为空 => 永远报 0px，等于漏检白边。相邻区域算作"区域外"，正是 defringe 想要的几何语义。）
    if fringe:
        rgb = rgba[:, :, :3]
        out_a = rgba[:, :, 3].copy()
        for r in recs:
            sub = (labels == r["id"])
            th, frac, cnt = V.fringe_thickness(sub, rgb, maxd=maxd, near_white=near_white)
            r["fringe_px"] = int(th)
            r["fringe_layer_white_fraction"] = {str(k): frac[k] for k in sorted(frac)}
            r["fringe_layer_px"] = {str(k): cnt[k] for k in sorted(cnt)}
            if th > 0:
                keep = V.erode_outer(sub, th)
                x0, y0, x1, y1 = r["bbox"]
                tile = keep[y0:y1 + 1, x0:x1 + 1]
                r["fringe_removed_px"] = int((sub[y0:y1 + 1, x0:x1 + 1] & ~tile).sum())
                out_a[y0:y1 + 1, x0:x1 + 1] = np.where(tile, out_a[y0:y1 + 1, x0:x1 + 1], 0)
        if write_defringed:
            d = rgba.copy()
            d[:, :, 3] = out_a
            paths["defringed_png"] = os.path.join(out_dir, tag + "_defringed.png")
            V.save_rgba(d, paths["defringed_png"])
            meta["defringed_opaque_px"] = int((out_a > alpha_thr).sum())
            meta["opaque_px_before"] = int((rgba[:, :, 3] > alpha_thr).sum())

    # 边界坐标 / 轮廓导出（只导面积最大的前 N 个区域，避免 txt 爆炸；如实报告截断）
    dump = [r for r in recs[:dump_regions]]
    if boundary:
        lines = []
        for r in dump:
            x0, y0, x1, y1 = r["bbox"]
            sub = (labels[y0:y1 + 1, x0:x1 + 1] == r["id"])
            ys, xs = np.nonzero(V.edge_ring(sub))
            r["edge_pixels"] = int(len(xs))
            lines.append("# region %d area=%d bbox=[%d,%d,%d,%d] edges=%d"
                         % (r["id"], r["area"], x0 + x_off, y0 + y_off, x1 + x_off, y1 + y_off, len(xs)))
            lines += ["%d,%d" % (int(x) + x0 + x_off, int(y) + y0 + y_off) for x, y in zip(xs, ys)]
        paths["edges_txt"] = os.path.join(out_dir, tag + "_edges.txt")
        V.write_lines(lines, paths["edges_txt"])
    if contour:
        lines = []
        for r in dump:
            x0, y0, x1, y1 = r["bbox"]
            sub = (labels[y0:y1 + 1, x0:x1 + 1] == r["id"])
            pts = V.trace_contour(sub)
            r["contour_points"] = len(pts)
            lines.append("# region %d area=%d contour=%d" % (r["id"], r["area"], len(pts)))
            lines += ["%d,%d" % (int(x) + x0 + x_off, int(y) + y0 + y_off) for x, y in pts]
        paths["contours_txt"] = os.path.join(out_dir, tag + "_contours.txt")
        V.write_lines(lines, paths["contours_txt"])

    # 逐区域裁切（配 --soft-alpha 即 cut-props.py 的等效做法：纯色底软阈值抠 alpha）
    if crop_out:
        cdir = os.path.join(out_dir, tag + "_crops")
        os.makedirs(cdir, exist_ok=True)
        made = []
        for r in recs[:dump_regions]:
            x0, y0, x1, y1 = r["bbox"]
            x0 = max(0, x0 - crop_pad); y0 = max(0, y0 - crop_pad)
            x1 = min(w - 1, x1 + crop_pad); y1 = min(h - 1, y1 + crop_pad)
            sub = rgba[y0:y1 + 1, x0:x1 + 1].copy()
            if soft_alpha and dist is not None:
                dd = dist[y0:y1 + 1, x0:x1 + 1]
                sub[:, :, 3] = (np.clip((dd - t0) / max(1e-6, t1 - t0), 0, 1) * 255).astype(np.uint8)
            p = os.path.join(cdir, "r%03d_%dx%d.png" % (r["id"], x1 - x0 + 1, y1 - y0 + 1))
            V.save_rgba(sub, p)
            r["crop"] = p
            made.append(p)
        paths["crops_dir"] = cdir
        paths["crops"] = made

    # 坐标换算：内部一律用 ROI 局部坐标，写出去时换成原图绝对坐标（免得逐格结果对不上整图）
    if x_off or y_off:
        for r in recs:
            r["bbox"] = [r["bbox"][0] + x_off, r["bbox"][1] + y_off, r["bbox"][2] + x_off, r["bbox"][3] + y_off]
            r["centroid"] = [round(r["centroid"][0] + x_off, 2), round(r["centroid"][1] + y_off, 2)]

    report = {
        "kind": "region-stats", "image": os.path.abspath(image), "size": [w0, h0],
        "roi": roi, "roi_size": [w, h], "coordinate_space": "absolute (original image)",
        "png_outputs_space": "roi-local" if (x_off or y_off) else "same as image",
        "mask": meta, "conn": conn, "min_area": int(min_area),
        "boundary_px_total": int(ring_all.sum()),
        "region_count": len(recs),
        "regions_dumped_edges": len(dump) if boundary else 0,
        "regions_dumped_contours": len(dump) if contour else 0,
        "regions_truncated": bool(len(recs) > len(dump)),
        "regions": recs,
        "mask_fingerprint": V.fingerprint_rgb(np.where(mask, 255, 0).astype(np.uint8)),
        "outputs": paths,
    }
    jp = os.path.join(out_dir, tag + ".json")
    paths["json"] = jp                      # 必须在保存前赋值：report["outputs"] 与 paths 是同一个 dict
    V.save_json(report, jp)

    print("region-stats  %s  %dx%d  mode=%s  %s" % (os.path.basename(image), w, h, meta["mode"],
          ("bg=%s" % meta.get("bg")) if meta.get("bg") else ""))
    print("coverage=%d px (%.2f%%)  regions=%d (min_area=%d, conn=%d)  boundary_px=%d"
          % (meta["coverage_px"], 100.0 * meta["coverage_share"], len(recs), min_area, conn, int(ring_all.sum())))
    for r in recs[:top]:
        extra = ""
        if fringe:
            extra = "  fringe=%dpx layers=%s" % (r.get("fringe_px", 0),
                                                 [r["fringe_layer_white_fraction"][k] for k in
                                                  sorted(r["fringe_layer_white_fraction"], key=int)][:4])
        print("  r%-3d area=%-7d bbox=[%4d,%4d,%4d,%4d]  %dx%d  centroid=(%.1f,%.1f)%s"
              % (r["id"], r["area"], r["bbox"][0], r["bbox"][1], r["bbox"][2], r["bbox"][3],
                 r["w"], r["h"], r["centroid"][0], r["centroid"][1], extra))
    if len(recs) > top:
        print("  ... %d more regions (see json)" % (len(recs) - top))
    if regions_truncated_note := (report["regions_truncated"]):
        print("edges/contours/crops dumped for top %d regions only (--dump-regions)" % len(dump))
    if fringe and meta.get("opaque_px_before") is not None:
        print("defringe: opaque %d -> %d px" % (meta["opaque_px_before"], meta["defringed_opaque_px"]))
    print("json=%s" % jp)
    for k in ("mask_png", "boundary_png", "checker_png", "edges_txt", "contours_txt", "defringed_png", "crops_dir"):
        if k in paths:
            print("%s=%s" % (k, paths[k]))
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description="mask -> areas/bboxes/boundary/fringe stats (P0 primitive)")
    ap.add_argument("--image", required=True)
    ap.add_argument("--roi", default=None, help="x0,y0,x1,y1 inclusive; per-cell work on a sheet. coords in output stay absolute")
    ap.add_argument("--mode", choices=["auto", "alpha", "bg", "color"], default="auto")
    ap.add_argument("--bg", default="#auto", help="#rrggbb or auto (per-image 4-edge ring median)")
    ap.add_argument("--t0", type=float, default=18.0)
    ap.add_argument("--t1", type=float, default=45.0)
    ap.add_argument("--color", default=None)
    ap.add_argument("--tol", type=float, default=40.0)
    ap.add_argument("--alpha-thr", type=int, default=127)
    ap.add_argument("--bg-px", type=int, default=6)
    ap.add_argument("--min-area", type=int, default=1)
    ap.add_argument("--conn", type=int, choices=[4, 8], default=8)
    ap.add_argument("--fringe-white", action="store_true", help="measure white-fringe thickness per region")
    ap.add_argument("--near-white", type=int, default=232)
    ap.add_argument("--maxd", type=int, default=6)
    ap.add_argument("--write-defringed", action="store_true")
    ap.add_argument("--contour", action="store_true")
    ap.add_argument("--no-boundary", action="store_true")
    ap.add_argument("--dump-regions", type=int, default=20)
    ap.add_argument("--crop-out", action="store_true")
    ap.add_argument("--soft-alpha", action="store_true", help="with --crop-out: soft bg-distance alpha (cut-props equivalent)")
    ap.add_argument("--crop-pad", type=int, default=3)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--tag", default=None)
    ap.add_argument("--top", type=int, default=15)
    a = ap.parse_args(argv)
    run(a.image, a.mode, a.out_dir, a.tag, a.roi, a.bg, a.t0, a.t1, a.color, a.tol, a.alpha_thr, a.bg_px,
        a.min_area, a.conn, a.fringe_white, a.near_white, a.maxd, a.contour, not a.no_boundary,
        a.dump_regions, a.crop_out, a.soft_alpha, a.crop_pad, a.write_defringed, a.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
