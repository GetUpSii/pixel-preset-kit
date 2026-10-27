#!/usr/bin/env python
# style-palette.py — 从「风格画像」构建调色盘（ramp 连续索引板）
#
# 定位：这是"风格设置"的执行端。输入 style-profile.py 产出的画像，
#   输出可直接 load_palette 进 Aseprite 的板子 + ramp 索引结构 + 色卡。
#
# 与 palette-forge.py 的分工：
#   palette-forge = 从**一张图自身的颜色**重铸板（median-cut → ramp）
#   style-palette = 从**参考风格的曲线**生成板（跨图迁移风格：把参考的
#                   彩度-明度关系套到新题材上，即使新题材色彩完全不同）
#
# 硬约束（像素画真源，见 SKILL.md）：
#   * ramp 连续索引：索引 ±1 = 明暗一阶；板子按 ramp 组织，不是按用量排序
#   * **索引 0 是 Aseprite 的 transparentColor 槽**，不能当颜色用 ⇒ 留占位，真实色从 1 开始
#   * 超色域降彩度（Lab -> sRGB 出界就降 C*，而不是裁剪产生偏色）
#
# 用法：
#   python style-palette.py --profile out/style/ref.style.json --ramps 4 \
#       --out-gpl out/style/ref.gpl --out-json out/style/ref.palette.json \
#       --out-swatch out/style/ref.swatch.png
#   python style-palette.py --profile P.json --image target.png --union 8   # 与目标图内容色并集
#   python style-palette.py --selftest
#
# stdout 只出 ASCII；报告走 UTF-8 JSON。
import argparse
import importlib.util
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from _vlib import (save_json, write_gpl, read_gpl, hexc, parse_hex, _font,  # noqa: E402
                   nearest_index, nearest_error, err_stats, load_rgba, median_cut)


def _mod(basename, alias):
    path = os.path.join(HERE, basename)
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PF = _mod("palette-forge.py", "pf_pal")
RF = _mod("ramp-from-template.py", "rf_pal")


# ---------------------------------------------------------------- 曲线与色阶

def interp_curve(curve, L, fallback):
    """族内 C*(L*) 曲线线性插值；曲线为空则退回该族中位彩度。"""
    if not curve:
        return float(fallback)
    xs = np.array([p[0] for p in curve], float)
    ys = np.array([p[1] for p in curve], float)
    order = np.argsort(xs)
    return float(np.interp(float(L), xs[order], ys[order]))


def lab_to_rgb_in_gamut(L, C, hue_deg, max_iter=160):
    """Lab -> sRGB；超色域就降 C*（保留色相与明度），返回 (rgb, C_used, clipped_original)。"""
    theta = np.radians(hue_deg)
    c = float(C)
    clipped0 = False
    for i in range(max_iter):
        a, b = c * np.cos(theta), c * np.sin(theta)
        rgb, clipped = RF.lab_to_srgb(float(L), a, b, report_clip=True)
        if i == 0:
            clipped0 = bool(clipped)
        if not clipped:
            return [int(round(v)) for v in rgb], c, clipped0
        c *= 0.97
        if c < 0.3:
            break
    return [int(round(v)) for v in rgb], c, clipped0


def build_family_ramp(fam, steps, c_scale=1.0, min_span=8.0,
                      L_floor=2.0, L_ceil=98.0, inset=0.08):
    """一个色相族 -> steps 个色阶（按 L* 升序 = ramp 连续索引顺序）。"""
    lo, hi = float(fam.get("L_min", 0.0)), float(fam.get("L_max", 100.0))
    if hi - lo < min_span:
        center = 0.5 * (lo + hi)
        lo, hi = center - min_span / 2.0, center + min_span / 2.0
    lo = max(L_floor, lo)
    hi = min(L_ceil, hi)
    if hi <= lo:
        hi = min(L_ceil, lo + min_span)
    span = hi - lo
    Ls = np.linspace(lo + inset * span, hi - inset * span, int(max(2, steps)))
    out = []
    for L in Ls:
        C = interp_curve(fam.get("curve", []), float(L), fam.get("C_p50", 30.0)) * c_scale
        rgb, c_used, was_clipped = lab_to_rgb_in_gamut(float(L), C, fam.get("hue_deg_median", 0.0))
        out.append({"L": round(float(L), 1), "C_target": round(float(C), 2),
                    "C_used": round(float(c_used), 2), "rgb": rgb, "hex": hexc(rgb),
                    "gamut_clipped": was_clipped})
    return out


def neutral_ramp(n, lo=6.0, hi=96.0):
    """中性结构色（灰阶）。像素画的黑描边/白高光不能靠彩度曲线生成，单独给。"""
    if n <= 0:
        return []
    Ls = np.linspace(lo, hi, n) if n > 1 else [0.5 * (lo + hi)]
    out = []
    for L in Ls:
        rgb, c_used, _ = lab_to_rgb_in_gamut(float(L), 0.0, 0.0)
        out.append({"L": round(float(L), 1), "C_target": 0.0, "C_used": c_used,
                    "rgb": rgb, "hex": hexc(rgb), "gamut_clipped": False})
    return out


# ---------------------------------------------------------------- 板构建

def build_palette(prof, steps=0, families=0, c_scale=1.0, neutral_n=3,
                  min_share=0.05, colors_cap=0, slot_color=(0, 0, 0),
                  transparent_slot=True):
    fams = [f for f in prof.get("hue_families", []) if f["share"] >= min_share]
    if families > 0:
        fams = fams[:families]
    if steps <= 0:
        steps = int(min(5, max(3, prof.get("ramp", {}).get("steps_est", 4))))

    ramps = {}
    if neutral_n > 0:
        ramps["00_neutral"] = neutral_ramp(neutral_n)
    for fam in fams:
        ramps["%02d_%s" % (fam["family"], fam["name"])] = build_family_ramp(
            fam, steps, c_scale=c_scale)

    # 色数上限：从**最小占比**的族开始丢（保留主色调）
    if colors_cap > 0:
        def total():
            return sum(len(v) for v in ramps.values()) + (1 if transparent_slot else 0)
        keys = [k for k in ramps.keys() if k != "00_neutral"]
        keys.sort(key=lambda k: next((f["share"] for f in fams
                                      if k.endswith(f["name"])), 1.0))
        for k in keys:
            if total() <= colors_cap:
                break
            ramps.pop(k, None)

    palette, tags, index_rams = [], [], {}
    if transparent_slot:
        palette.append(list(slot_color))
        tags.append("index0_transparent_slot")
    for name, entries in ramps.items():
        idxs = []
        for e in entries:
            idxs.append(len(palette))
            palette.append(e["rgb"])
            tags.append("%s_L%.0fC%.0f" % (name, e["L"], e["C_used"]))
        index_rams[name] = idxs
    return {
        "palette": [list(map(int, c)) for c in palette],
        "tags": tags,
        "ramps": index_rams,
        "ramp_entries": ramps,
        "steps_used": steps,
        "transparent_slot": bool(transparent_slot),
        "families_used": [f["name"] for f in fams],
    }


def report_target_fit(pal_rgb, image_path, union_n=0, union_thr=60.0):
    """板 -> 目标图的映射误差（并集判据：误差大的像素就是"板里没有的材质色"）。"""
    rgba, _ = load_rgba(image_path)
    opaque = rgba[:, :, 3] > 127
    px = rgba[:, :, :3][opaque].reshape(-1, 3)
    if len(px) == 0:
        return None, None
    pal = np.asarray(pal_rgb, np.int32)
    err = nearest_error(px, pal)
    rep = {"image": os.path.basename(image_path), "pixels": int(len(px)),
           "palette_size": int(len(pal)), **err_stats(err),
           "coverage_le30": round(float((err <= 30).mean()), 4)}
    extra = None
    if union_n > 0:
        resid = px[err > union_thr]
        if len(resid) >= 16:
            extra = [[int(v) for v in c] for c in median_cut(resid, union_n)]
            rep["residual_px_gt_%.0f" % union_thr] = int(len(resid))
            rep["extra_colors"] = len(extra)
        else:
            rep["residual_px_gt_%.0f" % union_thr] = int(len(resid))
            rep["extra_colors"] = 0
    return rep, extra


def append_extra(prof_pal, extra, ramp_name="zz_extra"):
    """把并集补色追加成一条独立 ramp（不打断已有 ramp 的连续索引）。"""
    if not extra:
        return
    idxs = []
    for c in extra:
        idxs.append(len(prof_pal["palette"]))
        prof_pal["palette"].append(list(map(int, c)))
        prof_pal["tags"].append(ramp_name)
    prof_pal["ramps"][ramp_name] = idxs


# ---------------------------------------------------------------- 可视化

def render_swatch(result, path, prof_name="", cell=46):
    ramps = result["ramp_entries"]
    if not ramps:
        ramps = {"(empty)": []}
    cols = max(len(v) for v in ramps.values()) if ramps else 1
    W = 190 + max(1, cols) * cell
    H = 34 + len(ramps) * (cell + 22)
    im = Image.new("RGB", (W, H), (26, 26, 30))
    dr = ImageDraw.Draw(im)
    fb, fs = _font(13), _font(10)
    dr.text((8, 6), "style palette from '%s'   %d colors   ramp steps=%d"
            % (prof_name, len(result["palette"]), result["steps_used"]),
            fill=(235, 235, 240), font=fb)
    if result["transparent_slot"]:
        dr.text((8, 20), "index 0 = transparent slot (not a usable color)",
                fill=(220, 170, 140), font=fs)
    for i, (name, entries) in enumerate(ramps.items()):
        y = 40 + i * (cell + 22)
        dr.text((8, y + cell // 2 - 6), name, fill=(225, 225, 232), font=fs)
        for j, e in enumerate(entries):
            x = 190 + j * cell
            dr.rectangle([x + 1, y + 1, x + cell - 3, y + cell - 3],
                         fill=tuple(e["rgb"]), outline=(80, 80, 88))
            dr.text((x + 2, y + cell), "L%.0f C%.0f" % (e["L"], e["C_used"]),
                    fill=(200, 200, 206), font=fs)
    im.save(path)
    return path


# ---------------------------------------------------------------- 自检

def _fake_profile(fams, steps_est=4):
    """构造一个受控的风格画像（测构建器本身，不依赖真实图）。"""
    env = [[20.0, 30.0], [40.0, 55.0], [60.0, 70.0], [80.0, 40.0]]
    return {
        "kind": "pixel-style-profile", "version": 1, "name": "fake", "chroma_cut": 5.0,
        "totals": {"sources": 1, "opaque_px": 1000, "effective_colors_max": 4,
                   "measured_px": 1000, "chroma_px": 1000, "neutral_px": 0,
                   "chroma_share": 1.0, "alpha_non_binary_px": 0},
        "lightness": {"p5": 20.0, "p25": 30.0, "median": 50.0, "p75": 70.0, "p95": 80.0},
        "chroma_envelope": {"bins": [{"i": i, "L_center": e[0], "C_p50": e[1], "C_p90": e[1] * 1.2,
                                      "share": 0.25, "L_lo": e[0] - 10, "L_hi": e[0] + 10,
                                      "C_mean": e[1]} for i, e in enumerate(env)],
                            "peak_bin_index": 2, "peak_L": 60.0, "peak_C": 70.0,
                            "peak_at": 0.67, "peak_prominence": 1.75,
                            "dark_over_peak": 0.43, "lit_over_peak": 0.57},
        "hue_families": fams, "neutral": None,
        "ramp": {"steps_est": steps_est, "levels": [20, 40, 60, 80], "main_family": fams[0]["name"],
                 "main_family_index": fams[0]["family"]},
        "shadow_lit_shift": {"dA_dark_minus_lit": -5.0, "dB_dark_minus_lit": -6.0,
                             "reads_as": "shadow bluer/cooler, shadow greener"},
        "dither": {"checker_cell_ratio": 0.0, "checker_cells": 0, "isolated4_px": 0,
                   "isolated4_ratio": 0.0, "alt_h_ratio": 0.1},
        "style_tags": ["color-count:ultra-low(<=8)", "dither:none",
                       "chroma-peak:terminator(middle)", "lit-side:keeps-color",
                       "hue-dominant:" + fams[0]["name"]],
    }


def _fam(name, idx, hue, curve, share=1.0, c50=50.0):
    return {"family": idx, "name": name, "hue_lo": idx * 30, "hue_hi": idx * 30 + 30,
            "share": share, "n_px": int(share * 1000), "C_p50": c50, "C_p90": c50 * 1.2,
            "C_max": c50 * 1.3, "L_p50": 45.0, "L_min": 20.0, "L_max": 80.0,
            "hue_deg_median": hue, "a_p50": c50 * np.cos(np.radians(hue)),
            "b_p50": c50 * np.sin(np.radians(hue)), "curve": curve,
            "rep_rgb": [200, 60, 50], "rep_hex": "#c83c32"}


def selftest():
    checks = []

    def ck(name, got, want):
        checks.append((name, got, want, got == want))

    curve = [[20.0, 30.0], [40.0, 55.0], [60.0, 70.0], [80.0, 40.0]]
    prof = _fake_profile([_fam("orange", 1, 45.0, curve, share=1.0, c50=55.0)])

    # --- 正例：4 阶 ramp，色阶应重现"彩度峰值在中间"的风格
    res = build_palette(prof, steps=4, neutral_n=0, transparent_slot=True)
    pal = res["palette"]
    ck("pal.index0_is_slot", res["tags"][0], "index0_transparent_slot")
    ck("pal.total_colors", len(pal), 5)              # 1 slot + 4 ramp
    ck("pal.ramp_len", len(res["ramps"]["01_orange"]), 4)
    idxs = res["ramps"]["01_orange"]
    ck("pal.ramp_indices_contiguous", idxs, list(range(1, 5)))
    ck("pal.slot_not_referenced", 0 in idxs, False)

    lab = PF.srgb_to_lab(np.array([pal[i] for i in idxs], np.int32))
    Ls = lab[:, 0]
    Cs = np.hypot(lab[:, 1], lab[:, 2])
    ck("ramp.L_strictly_increasing", bool(np.all(np.diff(Ls) > 0)), True)
    ck("ramp.C_peak_in_middle", int(np.argmax(Cs)), 2)
    ck("ramp.dark_less_saturated_than_peak", bool(Cs[0] < Cs[2]), True)

    # --- 负对照 1：sat-scale 0.5 必须真的把彩度打下来（参数有没有牙）
    res_half = build_palette(prof, steps=4, neutral_n=0, c_scale=0.5, transparent_slot=True)
    lab_h = PF.srgb_to_lab(np.array([res_half["palette"][i] for i in res_half["ramps"]["01_orange"]], np.int32))
    Cs_h = np.hypot(lab_h[:, 1], lab_h[:, 2])
    ck("satscale.halved_peak_lt_60pct", bool(Cs_h.max() < 0.6 * Cs.max()), True)

    # --- 负对照 2：sat-scale 0 -> 板必须变成灰阶（色相信息真的丢了）
    res_zero = build_palette(prof, steps=4, neutral_n=0, c_scale=0.0, transparent_slot=True)
    lab_z = PF.srgb_to_lab(np.array([res_zero["palette"][i] for i in res_zero["ramps"]["01_orange"]], np.int32))
    Cs_z = np.hypot(lab_z[:, 1], lab_z[:, 2])
    ck("satscale.zero_max_C_lt_3", bool(Cs_z.max() < 3.0), True)
    ck("satscale.zero_keeps_L_order", bool(np.all(np.diff(lab_z[:, 0]) > 0)), True)

    # --- 多族：色相真的分开（不是同一条 ramp 复制）
    prof2 = _fake_profile([_fam("orange", 1, 45.0, curve, share=0.6, c50=55.0),
                           _fam("teal", 6, 195.0, curve, share=0.4, c50=35.0)])
    res2 = build_palette(prof2, steps=4, neutral_n=3, transparent_slot=True)
    o = res2["palette"][res2["ramps"]["01_orange"][-1]]
    t = res2["palette"][res2["ramps"]["06_teal"][-1]]
    ck("multifam.hues_differ", bool(o[0] > o[2] and t[2] >= t[0]), True)
    ck("multifam.neutral_present", len(res2["ramps"]["00_neutral"]), 3)
    ck("multifam.total", len(res2["palette"]), 1 + 3 + 4 + 4)

    # --- 色数上限：超了要从最小族开始丢
    res3 = build_palette(prof2, steps=4, neutral_n=0, colors_cap=5, transparent_slot=True)
    ck("cap.respects_limit", len(res3["palette"]) <= 5, True)
    ck("cap.keeps_main_family", "01_orange" in res3["ramps"], True)

    # --- 板能被读回（GPL 往返是交付链路，必须验）
    tmp = os.path.join(HERE, "_selftest_tmp", "stylepal")
    os.makedirs(tmp, exist_ok=True)
    gpl = os.path.join(tmp, "roundtrip.gpl")
    write_gpl(res["palette"], gpl, name="selftest", tags=res["tags"])
    back = read_gpl(gpl)
    ck("gpl.roundtrip_len", len(back), len(pal))
    ck("gpl.roundtrip_equal", bool((back == np.array(pal, np.int32)).all()), True)

    # --- 目标图拟合报告：板与图完全一致时误差必须为 0（量尺自证）
    tgt = os.path.join(tmp, "exact.png")
    arr = np.zeros((16, 16, 4), np.uint8)
    arr[:, :, 3] = 255
    arr[:8, :, :3] = pal[1]
    arr[8:, :, :3] = pal[2]
    Image.fromarray(arr, "RGBA").save(tgt)
    rep, extra = report_target_fit(res["palette"][1:], tgt)
    ck("fit.exact_mean0", rep["mean"], 0.0)
    ck("fit.exact_p95_0", rep["p95"], 0.0)
    ck("fit.no_extra_when_exact", extra, None)

    # --- 负对照 3：板与图完全不匹配 -> 误差必须报得出来（不是永远说"很好"）
    #     多色残差：union 请求 3 个补色，残差里正好有 3 种色
    bad = np.zeros((24, 24, 4), np.uint8)
    bad[:, :, 3] = 255
    bad[0:8, :, :3] = [10, 200, 40]
    bad[8:16, :, :3] = [30, 80, 200]
    bad[16:24, :, :3] = [240, 220, 30]
    tgt2 = os.path.join(tmp, "mismatch.png")
    Image.fromarray(bad, "RGBA").save(tgt2)
    rep2, extra2 = report_target_fit(res["palette"][1:], tgt2, union_n=3, union_thr=60.0)
    ck("fit.mismatch_mean_gt_30", rep2["mean"] > 30, True)
    ck("fit.mismatch_extra_3", len(extra2 or []), 3)

    # --- 降级语义：残差唯一色比请求少时，只能补更少（这是正确行为，不是缺陷）
    single = np.zeros((16, 16, 4), np.uint8)
    single[:, :, 3] = 255
    single[:, :, :3] = [10, 200, 40]
    tgt3 = os.path.join(tmp, "mismatch_single.png")
    Image.fromarray(single, "RGBA").save(tgt3)
    _, extra3 = report_target_fit(res["palette"][1:], tgt3, union_n=4, union_thr=60.0)
    ck("fit.single_residual_degrades", 0 < len(extra3 or []) < 4, True)

    bad_checks = [c for c in checks if not c[3]]
    for name, got, want, ok in checks:
        print("  [%s] %s: got=%s want=%s" % ("ok" if ok else "FAIL", name, got, want))
    print("SELFTEST=%s checks=%d/%d" % ("PASS" if not bad_checks else "FAIL",
                                        len(checks) - len(bad_checks), len(checks)))
    return 0 if not bad_checks else 1


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="", help="style-profile.py 产出的画像 JSON")
    ap.add_argument("--image", default="", help="目标图（用于拟合报告与并集补色）")
    ap.add_argument("--steps", type=int, default=0, help="每条 ramp 的阶数（0=用画像估计）")
    ap.add_argument("--families", type=int, default=0, help="取前 N 个色相族（0=全部）")
    ap.add_argument("--min-share", type=float, default=0.05)
    ap.add_argument("--sat-scale", type=float, default=1.0, help="整体彩度缩放（1.0=忠实风格）")
    ap.add_argument("--neutral", type=int, default=3, help="中性结构色个数（黑/灰/白）")
    ap.add_argument("--colors", type=int, default=0, help="总色数上限（0=不限）")
    ap.add_argument("--union", type=int, default=0,
                    help="从目标图残差追加**最多** N 个补色（残差唯一色不足时更少）")
    ap.add_argument("--union-thr", type=float, default=60.0)
    ap.add_argument("--no-transparent-slot", action="store_true",
                    help="不加索引 0 占位（**仅**用于不出 Aseprite 的场合）")
    ap.add_argument("--slot-color", default="#000000")
    ap.add_argument("--out-gpl", default="")
    ap.add_argument("--out-json", default="")
    ap.add_argument("--out-swatch", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        sys.exit(selftest())

    if not a.profile or not os.path.exists(a.profile):
        print("ERROR: --profile required")
        sys.exit(2)
    import json
    with open(a.profile, "r", encoding="utf-8") as fh:
        prof = json.load(fh)

    slot = parse_hex(a.slot_color)
    result = build_palette(prof, steps=a.steps, families=a.families, c_scale=a.sat_scale,
                           neutral_n=a.neutral, min_share=a.min_share,
                           colors_cap=a.colors, slot_color=slot,
                           transparent_slot=not a.no_transparent_slot)

    fit = None
    if a.image and os.path.exists(a.image):
        fit, extra = report_target_fit(result["palette"][1:] if result["transparent_slot"]
                                       else result["palette"], a.image,
                                       union_n=a.union, union_thr=a.union_thr)
        append_extra(result, extra)

    print("STYLE=%s  colors=%d  steps=%d  families=%s"
          % (prof.get("name", "?"), len(result["palette"]), result["steps_used"],
             ",".join(result["families_used"]) or "(none)"))
    if result["transparent_slot"]:
        print("NOTE: index 0 = transparent slot (Aseprite transparentColor); usable colors = 1..%d"
              % (len(result["palette"]) - 1))
    for name, idxs in result["ramps"].items():
        entries = result["ramp_entries"].get(name, [])
        chain = "  ".join("%s(L%.0f)" % (e["hex"], e["L"]) for e in entries)
        print("  RAMP %-14s idx=%d..%d  %s" % (name, idxs[0], idxs[-1], chain))
    if fit:
        print("FIT %s: mean=%.2f p95=%.2f max=%.2f coverage(<=30)=%.1f%%"
              % (fit["image"], fit["mean"], fit["p95"], fit["max"], fit["coverage_le30"] * 100))
        if fit.get("extra_colors"):
            print("  added %d residual colors (target-specific materials)" % fit["extra_colors"])

    if a.out_gpl:
        write_gpl(result["palette"], a.out_gpl, name="style-" + prof.get("name", "palette"),
                  tags=result["tags"])
        print("GPL=" + a.out_gpl)
    if a.out_json:
        payload = {"kind": "pixel-palette", "version": 1,
                   "style_profile": a.profile, "style_name": prof.get("name", ""),
                   "palette": result["palette"], "tags": result["tags"],
                   "ramps": result["ramps"], "ramp_entries": result["ramp_entries"],
                   "transparent_slot": result["transparent_slot"],
                   "steps_used": result["steps_used"],
                   "sat_scale": a.sat_scale, "target_fit": fit}
        save_json(payload, a.out_json)
        print("JSON=" + a.out_json)
    if a.out_swatch:
        print("SWATCH=" + render_swatch(result, a.out_swatch, prof_name=prof.get("name", "")))


if __name__ == "__main__":
    main()
