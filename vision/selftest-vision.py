# selftest-vision.py — 视觉回路原语自测：合成样例 + 已知答案断言（不依赖任何外部资产）
#
# 每条断言都对应一个具体失效模式（PIXEL-PIPELINE.md 里的坑），不是"跑通就算过"：
#   连通域 8/4 邻域语义、边缘环周长、白边分层测厚、int16 溢出回归、图差定位精度、
#   调色板残差+并集（59.3 -> 13.2 的机制）、软阈值背景估计、CLI 端到端。
import importlib.util
import os
import subprocess
import sys

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, "_selftest_tmp")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


V = load(os.path.join(HERE, "_vlib.py"), "vlib")

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print("[%s] %s%s" % ("ok" if cond else "FAIL", name, ("  " + detail) if detail else ""), flush=True)


def approx(a, b, tol=0.01):
    return abs(float(a) - float(b)) <= tol


def save_rgba(arr, name):
    p = os.path.join(TMP, name)
    Image.fromarray(np.asarray(arr, dtype=np.uint8), "RGBA").save(p)
    return p


def cli_checks():
    """CLI 端到端：stdout 重定向到**文件**（沙箱禁 piped stdio，用文件句柄绕开）。"""
    py = sys.executable
    # 1) image-diff 端到端
    a = np.full((20, 20, 4), 100, np.uint8)
    a[:, :, 3] = 255
    b = a.copy()
    b[3:6, 4:10, 1] = 140                       # 3x6 块
    pa = save_rgba(a, "cli_a.png")
    pb = save_rgba(b, "cli_b.png")
    log = os.path.join(TMP, "cli_diff.log")
    outd = os.path.join(TMP, "cli_diff")
    with open(log, "w", encoding="utf-8") as fh:
        rc = subprocess.run([py, os.path.join(HERE, "image-diff.py"), "--a", pa, "--b", pb,
                             "--threshold", "10", "--out-dir", outd, "--tag", "cli"],
                            stdout=fh, stderr=subprocess.STDOUT).returncode
    txt = open(log, encoding="utf-8", errors="replace").read()
    check("CLI_image_diff_exit0", rc == 0, "rc=%d log=%s" % (rc, txt.strip().splitlines()[-1] if txt.strip() else ""))
    check("CLI_image_diff_found_block", "regions=1" in txt and "bbox=[   4,   3,   9,   5]" in txt,
          "stdout has regions=1 and exact bbox")
    check("CLI_image_diff_artifacts", all(os.path.exists(os.path.join(outd, s))
                                          for s in ("cli_heat.png", "cli_overlay.png", "cli_mask.png", "cli.json")),
          "heat/overlay/mask/json written")
    jr = __import__("json").load(open(os.path.join(outd, "cli.json"), encoding="utf-8"))
    check("CLI_image_diff_json_region", jr["region_count"] == 1 and jr["regions"][0]["bbox"] == [4, 3, 9, 5]
          and jr["outputs"]["json"].endswith("cli.json"), "region_count=%d bbox=%s" % (jr["region_count"], jr["regions"][0]["bbox"]))

    # 2) region-stats 端到端：2px 白边的合成图 -> fringe=2
    fc = fringe_case([1, 2])
    pf = save_rgba(fc, "cli_fringe.png")
    log2 = os.path.join(TMP, "cli_regions.log")
    outd2 = os.path.join(TMP, "cli_regions")
    with open(log2, "w", encoding="utf-8") as fh:
        rc2 = subprocess.run([py, os.path.join(HERE, "region-stats.py"), "--image", pf,
                              "--mode", "alpha", "--fringe-white", "--contour", "--crop-out",
                              "--out-dir", outd2, "--tag", "cli"], stdout=fh, stderr=subprocess.STDOUT).returncode
    txt2 = open(log2, encoding="utf-8", errors="replace").read()
    jr2 = __import__("json").load(open(os.path.join(outd2, "cli.json"), encoding="utf-8")) if rc2 == 0 else {}
    check("CLI_region_stats_exit0", rc2 == 0, "rc=%d tail=%s" % (rc2, txt2.strip().splitlines()[-1] if txt2.strip() else ""))
    check("CLI_region_stats_fringe_from_disk", jr2.get("regions") and jr2["regions"][0]["fringe_px"] == 2,
          "fringe_px(read back from json)=%s" % (jr2.get("regions", [{}])[0].get("fringe_px")))
    check("CLI_region_stats_artifacts", all(os.path.exists(os.path.join(outd2, "cli_" + s))
                                            for s in ("mask.png", "boundary.png", "edges.txt", "contours.txt")),
          "mask/boundary/edges/contours written")
    check("CLI_region_stats_crop", bool(jr2.get("regions")) and os.path.exists(jr2["regions"][0].get("crop", "")),
          "crop=%s" % (jr2.get("regions", [{}])[0].get("crop")))

    # 3) histogram 端到端：精确色板 -> 误差 0 / 覆盖 100%
    cols = np.array([[10, 20, 30], [200, 30, 40], [40, 200, 60]], np.uint8)
    im = np.zeros((30, 30, 4), np.uint8)
    im[0:10, :, :3] = cols[0]
    im[10:20, :, :3] = cols[1]
    im[20:30, :, :3] = cols[2]
    im[:, :, 3] = 255
    pim = save_rgba(im, "cli_hist.png")
    gpl = os.path.join(TMP, "cli_pal.gpl")
    V.write_gpl(cols.astype(np.int32), gpl, name="exact3")
    log3 = os.path.join(TMP, "cli_hist.log")
    outd3 = os.path.join(TMP, "cli_hist")
    with open(log3, "w", encoding="utf-8") as fh:
        rc3 = subprocess.run([py, os.path.join(HERE, "histogram.py"), "--image", pim, "--palette", gpl,
                              "--union", "4", "--out-dir", outd3, "--tag", "cli"], stdout=fh, stderr=subprocess.STDOUT).returncode
    txt3 = open(log3, encoding="utf-8", errors="replace").read()
    jr3 = __import__("json").load(open(os.path.join(outd3, "cli.json"), encoding="utf-8")) if rc3 == 0 else {}
    check("CLI_histogram_exit0", rc3 == 0, "rc=%d tail=%s" % (rc3, txt3.strip().splitlines()[-1] if txt3.strip() else ""))
    check("CLI_histogram_exact_zero_error", jr3.get("sources") and jr3["sources"][0]["error"]["mean"] == 0.0
          and jr3["sources"][0]["coverage_pct"] == 100.0,
          "mean=%s coverage=%s" % (jr3.get("sources", [{}])[0].get("error", {}).get("mean"),
                                   jr3.get("sources", [{}])[0].get("coverage_pct")))
    check("CLI_histogram_union_noop_when_exact", jr3.get("union") and jr3["union"]["resid_px"] == 0,
          "resid_px=%s (nothing to add when palette already exact)" % jr3.get("union", {}).get("resid_px"))
    check("CLI_histogram_gpl_written", os.path.exists(os.path.join(outd3, "cli.gpl")), "gpl=%s" % os.path.join(outd3, "cli.gpl"))


def fringe_case(white_layers, size=40, blk=24, off=8):
    """合成"外层 n 圈纯白描边"的 RGBA 样例（不透明块 24x24，白层按 distance_bands 刷）。"""
    a = np.zeros((size, size, 4), np.uint8)
    a[off:off + blk, off:off + blk, :3] = (100, 80, 60)
    a[off:off + blk, off:off + blk, 3] = 255
    op = a[:, :, 3] > 127
    bands = V.distance_bands(op, 6)
    for d in white_layers:
        a[bands[d], 0:3] = (255, 255, 255)
    return a


def main():
    os.makedirs(TMP, exist_ok=True)

    # ---------- 1. 连通域语义：8 邻域把对角像素算连通，4 邻域不算 ----------
    mask = np.zeros((60, 60), bool)
    mask[5:15, 5:15] = True              # 10x10 实心块 = 100
    mask[20:23, 30:34] = True            # 3x4 = 12
    mask[30, 40] = True                  # 对角对 (30,40)-(31,41)
    mask[31, 41] = True
    labels8, n8 = V.label_mask(mask, conn=8)
    labels4, n4 = V.label_mask(mask, conn=4)
    check("CCL_8conn_regions", n8 == 3, "n=%d (expect 3: block+rect+diagpair)" % n8)
    check("CCL_4conn_regions", n4 == 4, "n=%d (expect 4: diagonal splits)" % n4)
    check("CCL_raster_order_id1", labels8[5, 5] == 1 and labels8[20, 30] == 2,
          "id1=%d id2=%d (expect 1,2 = top-left first)" % (labels8[5, 5], labels8[20, 30]))
    recs = V.region_records(mask, conn=8, min_area=1)
    areas = sorted(r["area"] for r in recs)
    check("CCL_areas", areas == [2, 12, 100], "areas=%s" % areas)
    diag = [r for r in recs if r["area"] == 2][0]
    check("CCL_diag_bbox", diag["bbox"] == [40, 30, 41, 31], "bbox=%s" % diag["bbox"])
    big = [r for r in recs if r["area"] == 100][0]
    check("CCL_bbox_closed_interval", big["bbox"] == [5, 5, 14, 14] and big["w"] == 10 and big["h"] == 10,
          "bbox=%s w=%d h=%d" % (big["bbox"], big["w"], big["h"]))
    check("CCL_centroid", approx(big["centroid"][0], 9.5) and approx(big["centroid"][1], 9.5),
          "centroid=%s" % big["centroid"])

    # ---------- 2. 边缘环 + 外轮廓 ----------
    block = np.zeros((30, 30), bool)
    block[4:14, 4:14] = True
    ring = V.edge_ring(block)
    check("edge_ring_perimeter_10x10", int(ring.sum()) == 36, "ring=%d (expect 2*10+2*8=36)" % int(ring.sum()))
    cont = V.trace_contour(block)
    cs = set(cont)
    cx = [p[0] for p in cs]
    cy = [p[1] for p in cs]
    check("contour_all_on_ring", cs.issubset({(int(x), int(y)) for y, x in np.argwhere(ring)}),
          "n_contour=%d n_ring=%d" % (len(cs), int(ring.sum())))
    check("contour_spans_bbox", (min(cx), min(cy), max(cx), max(cy)) == (4, 4, 13, 13),
          "contour bbox=%s" % str((min(cx), min(cy), max(cx), max(cy))))
    check("contour_dense", len(cs) >= 30, "n_contour=%d (>=30)" % len(cs))

    # ---------- 3. 白边分层测厚（defringe-props.py 的同一判据） ----------
    a2 = fringe_case([1, 2])
    th2, frac2, cnt2 = V.fringe_thickness(a2[:, :, 3] > 127, a2[:, :, :3])
    check("fringe_two_layers", th2 == 2, "th=%d frac=%s" % (th2, frac2))
    a1 = fringe_case([1])
    th1, _, _ = V.fringe_thickness(a1[:, :, 3] > 127, a1[:, :, :3])
    check("fringe_one_layer", th1 == 1, "th=%d" % th1)
    a3 = fringe_case([3])                     # 白只在第 3 层：外层不白 -> 不判白边
    th3, frac3, _ = V.fringe_thickness(a3[:, :, 3] > 127, a3[:, :, :3])
    check("fringe_inner_white_ignored", th3 == 0, "th=%d frac1=%.2f frac3=%.2f" % (th3, frac3.get(1, 0), frac3.get(3, 0)))
    op = a2[:, :, 3] > 127
    check("fringe_erode_two_layers", int(V.erode_outer(op, 2).sum()) == (24 - 4) ** 2,
          "opaque=%d (expect 20*20=400)" % int(V.erode_outer(op, 2).sum()))
    check("fringe_erode_inner_untouched", int(V.erode_outer(op, 0).sum()) == 24 * 24,
          "th=0 must not touch anything")

    # ---------- 4. int16 溢出回归（255**2 溢出 -> NaN 的那个坑） ----------
    idx, err = V.nearest_index(np.array([[255, 255, 255], [0, 0, 0]]), np.array([[0, 0, 0], [255, 255, 255]]))
    e = V.nearest_error(np.array([[255, 255, 255]], np.uint8), np.array([[0, 0, 0]], np.uint8))
    check("color_distance_no_overflow", approx(e[0], 441.67, 0.05) and not np.isnan(e[0]),
          "err(white->black)=%.2f (expect 441.67, int16 would NaN)" % e[0])
    check("nearest_index_exact", list(idx) == [1, 0], "idx=%s" % list(idx))

    # ---------- 5. 图差定位精度 ----------
    base = np.full((24, 24, 4), 128, np.uint8)
    base[:, :, 3] = 255
    other = base.copy()
    other[4:7, 5:9, 0] = 168                    # 3 行 x 4 列，R +40
    other[18, 20, 0] = 255                      # 单像素强变化 R +127
    d = V.diff_maps(base, other, metric="rgb")
    check("diff_max_magnitude", int(d.max()) == 127, "max=%d (expect 127)" % int(d.max()))
    check("diff_zero_where_equal", int((d == 0).sum()) == 24 * 24 - 13, "unchanged=%d" % int((d == 0).sum()))
    recs = V.region_records(d >= 10, conn=8, weight=d, min_area=1)
    check("diff_region_count", len(recs) == 2, "regions=%d" % len(recs))
    main_r = [r for r in recs if r["area"] == 12]
    check("diff_bbox_exact", main_r and main_r[0]["bbox"] == [5, 4, 8, 6],
          "bbox=%s (expect [5,4,8,6])" % (main_r[0]["bbox"] if main_r else None))
    check("diff_region_weight_max", main_r and approx(main_r[0]["weight_max"], 40), 
          "weight_max=%s" % (main_r[0]["weight_max"] if main_r else None))
    recs2 = V.region_records(d >= 10, conn=8, min_area=2)
    check("diff_min_area_filter", len(recs2) == 1, "regions(min_area=2)=%d" % len(recs2))
    # alpha 差异：RGB 相同、只有 alpha 变
    t1 = base.copy()
    t2 = base.copy()
    t2[3, 3, 3] = 0
    check("diff_ignores_alpha_by_default", int(V.diff_maps(t1, t2).max()) == 0, "max=%d" % int(V.diff_maps(t1, t2).max()))
    check("diff_counts_alpha_when_asked", int(V.diff_maps(t1, t2, include_alpha=True).max()) == 255,
          "max=%d" % int(V.diff_maps(t1, t2, include_alpha=True).max()))
    sy, sx = V.roi_slice("5,4,8,6", 24, 24)
    check("roi_slice_closed_interval", (sx.start, sx.stop, sy.start, sy.stop) == (5, 9, 4, 7),
          "%s %s" % (sx, sy))
    try:
        V.diff_maps(np.zeros((4, 4, 4), np.uint8), np.zeros((5, 5, 4), np.uint8))
        check("diff_size_mismatch_raises", False, "no exception raised")
    except ValueError:
        check("diff_size_mismatch_raises", True, "ValueError as designed")

    # ---------- 6. 调色板：残差 + 并集（59.3 -> 13.2 的机制） ----------
    rng = np.random.default_rng(20261012)
    scene_cols = np.array([[40, 60, 30], [90, 70, 50], [30, 40, 80]], np.uint8)     # "场景 3 色"
    cat_cols = np.array([[230, 140, 60], [240, 225, 190]], np.uint8)               # "道具特有橘/米"
    px = np.vstack([np.tile(c, (400, 1)) for c in np.vstack([scene_cols, cat_cols])]).astype(np.int16)
    px = np.clip(px + rng.integers(-3, 4, px.shape), 0, 255).astype(np.uint8)       # 轻微噪声
    img = px.reshape(50, 40, 3)
    uq, top = V.unique_colors(img)
    check("unique_colors_count", uq >= 5 and len(top) >= 5, "unique=%d top=%d" % (uq, len(top)))
    shares = [t["share"] for t in top]
    counts = [t["count"] for t in top]
    check("unique_colors_ranked", shares == sorted(shares, reverse=True) and counts == sorted(counts, reverse=True)
          and counts[0] == int(np.bincount(((img[:, :, 0].astype(np.int64) << 16) | (img[:, :, 1].astype(np.int64) << 8)
                                            | img[:, :, 2]).ravel()).max()),
          "top1=%d/%d px share=%.4f (descending, top1 == global max)" % (counts[0], len(px), shares[0]))
    flat3 = np.zeros((30, 20, 3), np.uint8)
    flat3[0:20] = (10, 20, 30)                      # 400 px 最多
    flat3[20:27] = (200, 10, 10)                    # 140 px
    flat3[27:30] = (10, 200, 10)                    # 60 px
    uq3, top3 = V.unique_colors(flat3, counts_top=5)
    check("unique_colors_dominant_first", uq3 == 3 and top3[0]["hex"] == "#0a141e" and top3[0]["count"] == 400,
          "unique=%d top1=%s n=%d (expect 3 / #0a141e / 400)" % (uq3, top3[0]["hex"], top3[0]["count"]))
    e_scene = V.nearest_error(img.reshape(-1, 3), scene_cols)
    resid = img.reshape(-1, 3)[e_scene > 30]
    extra = V.median_cut(resid, 4)
    union = np.vstack([scene_cols, extra]).astype(np.int32)
    e_union = V.nearest_error(img.reshape(-1, 3), union)
    check("palette_residual_improves", e_union.mean() < e_scene.mean() * 0.5,
          "mean err scene=%.1f -> union(%d)=%.1f" % (e_scene.mean(), len(union), e_union.mean()))
    check("palette_exact_covers", approx(V.nearest_error(px, np.unique(px, axis=0)).max(), 0, 1e-9),
          "exact palette -> err 0")
    cov = float((e_scene <= 30).mean()) * 100
    check("palette_coverage_report", 0 < cov < 100, "coverage(scene, tol=30)=%.1f%%" % cov)
    st = V.err_stats(e_scene)
    check("palette_err_stats_keys", set(["mean", "p95", "p99", "max"]) <= set(st.keys()), "stats=%s" % st)

    # ---------- 7. 背景估计：用四边环，不被中央暗块污染 ----------
    bgim = np.full((40, 40, 3), 210, np.uint8)
    bgim[10:30, 10:30] = (20, 20, 20)
    est = V.estimate_bg_ring(bgim, 6)
    check("bg_ring_median", all(approx(v, 210, 0.6) for v in est), "est=%s" % list(est))
    dist, strong = V.mask_from_bg(bgim, est, 18.0, 45.0)
    ys, xs = np.nonzero(strong)
    check("bg_soft_threshold_bbox", (xs.min(), ys.min(), xs.max(), ys.max()) == (10, 10, 29, 29),
          "strong bbox=%s" % str((xs.min(), ys.min(), xs.max(), ys.max())))

    # ---------- 8. 资源渲染产物可写回 ----------
    p = save_rgba(np.dstack([np.full((8, 8), 200, np.uint8)] * 3 + [np.full((8, 8), 255, np.uint8)]), "px.png")
    V.swatch_sheet(np.array([[255, 0, 0], [0, 255, 0], [0, 0, 255]], np.uint8), os.path.join(TMP, "sw.png"))
    V.checker_overlay(np.asarray(Image.open(p).convert("RGBA")), os.path.join(TMP, "chk.png"))
    check("render_swatch_and_checker", os.path.exists(os.path.join(TMP, "sw.png")) and os.path.exists(os.path.join(TMP, "chk.png")))

    # ---------- 9. JSON 报告可回读（agent 侧的消费接口） ----------
    jp = V.save_json({"regions": recs, "note": "中文 UTF-8 回读"}, os.path.join(TMP, "r.json"))
    import json as _json
    with open(jp, "r", encoding="utf-8") as f:
        back = _json.load(f)
    check("json_utf8_roundtrip", back["note"] == "中文 UTF-8 回读" and len(back["regions"]) == 2,
          "utf8 non-ascii preserved=%s regions=%d" % (back["note"] == "中文 UTF-8 回读", len(back["regions"])))

    # ---------- 10. CLI 端到端（子进程 + 文件重定向 stdout） ----------
    cli_checks()

    ok = sum(1 for _, c, _ in CHECKS if c)
    print("")
    for nm, c, dt in CHECKS:
        if not c:
            print("FAILED: %s  %s" % (nm, dt))
    print("SELFTEST=%s checks=%d/%d" % ("PASS" if ok == len(CHECKS) else "FAIL", ok, len(CHECKS)))
    return 0 if ok == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
