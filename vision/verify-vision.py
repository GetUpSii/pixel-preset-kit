# verify-vision.py — 一条命令复现「视觉回路原语在真产物上的证据」（缺验证的结论不写进文档）
#
# 断言都来自 PIXEL-PIPELINE.md / README.md 里既有的实测结论，且**两个独立代码路径互相校核**：
#   1) 白边测厚：刚生成的道具表应报 铁锅 2px、其余 1px（defringe-props.py 的同口径）；
#   2) 清洗后再测应归零（证明腐蚀真的发生了，而不是判据失灵）；
#   3) 图差独立复算：image-diff(原图, 清洗后, include-alpha) 的 changed_pixels
#      应恰好等于 region-stats 报的 fringe_removed_px —— 两条路算同一件事，数字必须一致；
#   4) 调色板：道具硬套场景 48 色误差高、并集 64 色后大幅下降（59.3 -> 13.2 的同方向复现）。
import importlib.util
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _vlib as V  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SC = os.path.abspath(os.path.join(HERE, "..", "out", "scene"))
OUT = os.path.abspath(os.path.join(HERE, "..", "out", "vision", "verify"))

CHECKS = []


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print("[%s] %s%s" % ("ok" if cond else "FAIL", name, ("  " + detail) if detail else ""), flush=True)


def main():
    if not os.path.isdir(SC):
        print("MISSING real artifacts: %s" % SC)
        return 2
    rs = load(os.path.join(HERE, "region-stats.py"), "region_stats")
    hg = load(os.path.join(HERE, "histogram.py"), "histogram")
    idiff = load(os.path.join(HERE, "image-diff.py"), "image_diff")

    raw_dir = os.path.join(SC, "props")
    clean_dir = os.path.join(SC, "props_clean")
    report = {}
    fringe_by_file = {}
    for fn in sorted(f for f in os.listdir(raw_dir) if f.endswith(".png")):
        rep = rs.run(os.path.join(raw_dir, fn), mode="alpha", out_dir=OUT, tag="v_" + fn[:-4],
                     fringe=True, contour=False, boundary=False, top=1)
        fringe_by_file[fn] = rep["regions"][0]["fringe_px"] if rep["regions"] else None
    report["fringe_by_file"] = fringe_by_file

    cauldron = "prop_01_cauldron.png"
    check("fringe_cauldron_is_2px", fringe_by_file.get(cauldron) == 2,
          "prop_01_cauldron=%s px (README: 铁锅 2px)" % fringe_by_file.get(cauldron))
    cat = "prop_03_sleeping_cat.png"
    check("fringe_cat_not_white", fringe_by_file.get(cat) == 0,
          "prop_03_sleeping_cat=%s px (README: 最外层白占比 0.45 < 0.5 判非白边)" % fringe_by_file.get(cat))
    affected = sorted(k for k, v in fringe_by_file.items() if v)
    check("fringe_affected_6_of_12", len(affected) == 6,
          "affected=%d/12 %s (README: 12 件里 6 件有白边)" % (len(affected), [k.split('_', 2)[2][:-4] for k in affected]))
    check("fringe_thickness_values", set(v for v in fringe_by_file.values() if v) <= {1, 2},
          "distinct thickness=%s (README: 铁锅 2px、其余 1px)" % sorted(set(v for v in fringe_by_file.values() if v)))

    # 清洗后再测必须归零（判据仍有效 -> 归零是腐蚀的结果，不是判据失灵）
    clean_rep = rs.run(os.path.join(clean_dir, cauldron), mode="alpha", out_dir=OUT, tag="v_clean_cauldron",
                       fringe=True, contour=False, boundary=False, top=1)
    check("fringe_clean_is_0px", clean_rep["regions"][0]["fringe_px"] == 0,
          "props_clean/prop_01_cauldron fringe=%s px" % clean_rep["regions"][0]["fringe_px"])

    # 两条独立路径互校：腐蚀掉多少像素
    raw_rep = rs.run(os.path.join(raw_dir, cauldron), mode="alpha", out_dir=OUT, tag="v_raw_cauldron",
                     fringe=True, contour=False, boundary=False, top=1, write_defringed=True)
    removed = raw_rep["regions"][0].get("fringe_removed_px")
    before = raw_rep["mask"]["coverage_px"]
    after = clean_rep["mask"]["coverage_px"]
    check("cross_check_removed_vs_clean_delta", removed == before - after,
          "fringe_removed_px=%s  raw_coverage-after_clean=%s" % (removed, before - after))
    drep = idiff.run(os.path.join(raw_dir, cauldron), os.path.join(clean_dir, cauldron), out_dir=OUT,
                     tag="v_diff_cauldron", threshold=8, include_alpha=True, top=3)
    check("cross_check_image_diff_matches", drep["changed_pixels"] == removed,
          "image-diff(include-alpha).changed=%d vs region-stats.fringe_removed=%s (两条路必须同数)"
          % (drep["changed_pixels"], removed))
    check("cross_check_diff_regions_pointwise", drep["max_delta"] == 255,
          "alpha 0<->255 -> max_delta=%d (threshold 8)" % drep["max_delta"])
    report["cross_check"] = {"fringe_removed_px": removed, "image_diff_changed_px": drep["changed_pixels"],
                             "diff_fingerprint": drep["diff_heat_fingerprint"]}

    # 素材表：全表用**全局**背景色必然失败（暗角/噪声让背景自己成一块大区域），
    # 必须逐格用自己的四边环估背景色 —— 这条坑在本原语下被机制化复现，而不是靠记忆。
    sheet = os.path.join(SC, "gen_propsheet_2k.png")
    if os.path.exists(sheet):
        srep = rs.run(sheet, mode="bg", out_dir=OUT, tag="v_sheet", min_area=200, boundary=False,
                      contour=False, top=3)
        w, h = srep["size"]
        # 全局估背景色 => 暗角被当成前景：出现 bbox == 整图的区域（文档里的坑，这里是机制化复现）
        full_box = [r for r in srep["regions"] if r["bbox"] == [0, 0, w - 1, h - 1]]
        check("sheet_global_bg_captures_background", srep["region_count"] == 14 and len(full_box) == 1
              and full_box[0]["area"] == 92244,
              "global bg -> %d regions; whole-image-bbox region=%s (expect 14 regions, exactly 1 x 92244 px)"
              % (srep["region_count"], [(r["id"], r["area"]) for r in full_box]))
        cw, ch = w // 4, h // 3
        counts, propgrade, bgpatch, inner, per_cell = [], [], [], [], []
        for r in range(3):
            for c in range(4):
                roi = "%d,%d,%d,%d" % (c * cw, r * ch, (c + 1) * cw - 1, (r + 1) * ch - 1)
                cr = rs.run(sheet, mode="bg", roi=roi, out_dir=OUT, tag="v_cell%d%d" % (r, c),
                            min_area=200, boundary=False, contour=False, top=1)
                big = [x for x in cr["regions"] if x["area"] >= 50000]
                small = [x for x in cr["regions"] if x["area"] < 50000]
                counts.append(cr["region_count"])
                propgrade.append(len(big))
                for x in small:
                    inside = bool(big) and x["bbox"][0] >= big[0]["bbox"][0] and x["bbox"][2] <= big[0]["bbox"][2] \
                        and x["bbox"][1] >= big[0]["bbox"][1] and x["bbox"][3] <= big[0]["bbox"][3]
                    (inner if inside else bgpatch).append({"roi": roi, "area": x["area"], "bbox": x["bbox"]})
                per_cell.append({"roi": roi, "n": cr["region_count"], "big": len(big), "small": len(small)})
        check("sheet_per_cell_one_propgrade_region", propgrade == [1] * 12,
              "per-cell regions with area>=50000 = %s (expect one prop per cell)" % propgrade)
        check("sheet_per_cell_extras_classified", len(bgpatch) == 11 and len(inner) == 2,
              "background-vignette blocks bbox==whole-cell: %d (expect 11); fragments INSIDE the prop bbox: %d "
              "(expect 2) %s -> 计数必须先分类再数" % (len(bgpatch), len(inner), [(i["roi"], i["area"]) for i in inner]))
        report["sheet"] = {"global_regions": srep["region_count"],
                           "global_background_region": [(r["id"], r["area"]) for r in full_box],
                           "cell_counts": counts, "per_cell": per_cell,
                           "background_patches": len(bgpatch), "inner_fragments": inner}

    # 调色板：硬套 vs 并集
    gpl = os.path.join(SC, "apothecary48.gpl")
    base = hg.run([os.path.join(clean_dir, cat)], palette=gpl, out_dir=OUT, tag="v_cat_base", palette_max=256)
    uni = hg.run([os.path.join(clean_dir, cat)], palette=gpl, union=16, out_dir=OUT, tag="v_cat_union")
    b = base["sources"][0]["error"]
    u = uni["sources"][0]["error_with_union"]
    check("palette_union_cuts_error", u["mean"] < b["mean"] * 0.35,
          "mean %.1f -> %.1f (%.0f%% lower; README 59.3 -> 13.2 同方向)" % (b["mean"], u["mean"],
                                                                           100 * (1 - u["mean"] / b["mean"])))
    check("palette_union_raises_coverage", uni["sources"][0]["coverage_pct_with_union"] > base["sources"][0]["coverage_pct"] * 3,
          "coverage(<=30) %.1f%% -> %.1f%%" % (base["sources"][0]["coverage_pct"],
                                               uni["sources"][0]["coverage_pct_with_union"]))
    check("palette_union_size", uni["union"]["palette_size"] == 48 + uni["union"]["extra_n"],
          "48 + %d = %d colours" % (uni["union"]["extra_n"], uni["union"]["palette_size"]))
    report["palette"] = {"base_mean": b["mean"], "base_coverage": base["sources"][0]["coverage_pct"],
                         "union_mean": u["mean"], "union_coverage": uni["sources"][0]["coverage_pct_with_union"],
                         "union_size": uni["union"]["palette_size"]}

    report["checks"] = [{"name": n, "ok": c, "detail": d} for n, c, d in CHECKS]
    ok = sum(1 for _, c, _ in CHECKS if c)
    report["verdict"] = "PASS" if ok == len(CHECKS) else "FAIL"
    os.makedirs(OUT, exist_ok=True)
    jp = os.path.join(OUT, "verify-vision.json")
    V.save_json(report, jp)
    for nm, c, dt in CHECKS:
        if not c:
            print("FAILED: %s  %s" % (nm, dt))
    print("report=%s" % jp)
    print("VERIFY=%s checks=%d/%d" % (report["verdict"], ok, len(CHECKS)))
    return 0 if ok == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
