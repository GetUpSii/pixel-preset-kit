#!/usr/bin/env python
# style-profile.py — 参考图 -> 风格画像（style profile）
#
# 定位：把"风格"从口头描述变成可复算的数字，作为构建/修改调色盘的输入。
# 为什么不是直接 median-cut 取色：median-cut 只回答"这张图有哪些色"，
#   回答不了"这张图的风格是什么"——彩度峰值落在哪个明度、几阶 ramp、
#   抖不抖、冷暖偏向、高光是冲淡还是保色。这些才是"风格"。
#
# 复用（**同一度量**铁律：不重写已有量尺）：
#   palette-forge.srgb_to_lab / chroma_hue   → 颜色空间换算
#   discipline-metrics.bayer4_energy / isolated4 / alt_energy → 抖动度量
#
# 用法：
#   python style-profile.py --image ref.png --name apothecary \
#       --out out/style/apothecary.style.json --swatch out/style/apothecary.style.png
#   python style-profile.py --glob "refs/*.png" --name mixed --out m.json
#   python style-profile.py --selftest
#
# stdout 只出 ASCII（宿主 pwsh 是 PS5.1 + OEM936）；报告走 UTF-8 JSON。
import argparse
import glob as globmod
import importlib.util
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from _vlib import load_rgba, save_json, hexc, parse_hex, _font  # noqa: E402


def _mod(basename, alias):
    path = os.path.join(HERE, basename)
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PF = _mod("palette-forge.py", "pf_style")
DM = _mod("discipline-metrics.py", "dm_style")

HUE_FAMILY_NAMES = ["red", "orange", "yellow", "yellowgreen", "green", "green-teal",
                    "teal", "cyan-blue", "blue", "violet", "magenta", "pink-red"]

DEFAULT_MAX_PIXELS = 200000


# ---------------------------------------------------------------- 单个来源的测量

def measure(path, max_pixels=DEFAULT_MAX_PIXELS):
    """单张图的风格测量。返回 dict 或 None（图太小/全透明）。"""
    rgba, _size = load_rgba(path)
    alpha = rgba[:, :, 3]
    opaque = alpha > 127
    n_op = int(opaque.sum())
    if n_op < 64:
        return None
    rgb = rgba[:, :, :3].astype(np.int32)
    px = rgb[opaque].reshape(-1, 3)

    # 唯一色数：全图像素打包后去重（O(N)，比 unique(axis=0) 快一个量级）
    packed = (px[:, 0].astype(np.int64) << 16) | (px[:, 1].astype(np.int64) << 8) | px[:, 2]
    uniq_packed = np.unique(packed)
    # 有效色数：5bit/通道后仍不同的色数（人眼在像素画尺度的"可辨色数"）
    px5 = px >> 3
    p5 = (px5[:, 0].astype(np.int64) << 12) | (px5[:, 1].astype(np.int64) << 6) | px5[:, 2]
    eff = int(len(np.unique(p5)))

    # Lab 统计：大图采样（唯一色数仍按全图算）
    rng = np.random.default_rng(7)
    if len(px) > max_pixels:
        sel = rng.choice(len(px), size=max_pixels, replace=False)
        sample = px[sel]
        sampled = True
    else:
        sample = px
        sampled = False
    lab = PF.srgb_to_lab(sample)
    L = lab[:, 0]
    C, hue_rad = PF.chroma_hue(lab)
    hue_deg = np.degrees(hue_rad) % 360.0

    checker, cells = DM.bayer4_energy(rgb, opaque)
    iso = DM.isolated4(rgb, opaque)
    alt = DM.alt_energy(rgb, opaque)

    return {
        "path": path,
        "file": os.path.basename(path),
        "w": int(rgba.shape[1]), "h": int(rgba.shape[0]),
        "opaque_px": n_op,
        "unique_colors": int(len(uniq_packed)),
        "effective_colors": eff,
        "alpha_non_binary_px": int(((alpha != 0) & (alpha != 255)).sum()),
        "cells": cells,
        "checker_cells": checker, "checker_cell_ratio": round(checker / cells, 4) if cells else 0.0,
        "isolated4_px": iso, "isolated4_ratio": round(iso / n_op, 4),
        "alt": alt,
        "sampled": sampled,
        "_L": L, "_C": C, "_hue": hue_deg, "_rgb": sample,
    }


# ---------------------------------------------------------------- 汇总成风格画像

def _hist(values, lo, hi, bins):
    h, _ = np.histogram(values, bins=bins, range=(lo, hi))
    return [int(v) for v in h]


def _hue_family(hue_deg):
    return (np.floor(hue_deg / 30.0).astype(np.int32)) % 12


def chroma_envelope(L, C, bins=10, min_share=0.005):
    """彩度随明度的包络：按 L* 等宽分箱，每箱给 C* 的 p50 / p90。"""
    edges = np.linspace(0.0, 100.0, bins + 1)
    rows = []
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        m = (L >= lo) & (L < hi) if i < bins - 1 else (L >= lo) & (L <= hi)
        n = int(m.sum())
        if n == 0 or n < len(L) * min_share:
            continue
        rows.append({
            "i": i, "L_lo": round(float(lo), 1), "L_hi": round(float(hi), 1),
            "share": round(n / len(L), 4),
            "C_p50": round(float(np.percentile(C[m], 50)), 2),
            "C_p90": round(float(np.percentile(C[m], 90)), 2),
            "C_mean": round(float(C[m].mean()), 2),
            "L_center": round(float((lo + hi) / 2), 1),
        })
    return rows


def hue_families(L, C, lab, rgb, min_share=0.01, curve_bins=6):
    """按 Lab 色相角 30° 分 12 族，逐族给出：占比、彩度分位、代表色、**逐族 C*(L*) 曲线**。

    逐族曲线是构建调色盘的必需输入：多族图（如橙伞盖 + 黄高光 + 红暗部）共用一条
    全局包络会把别的族的彩度关系套进来。曲线点 [L_center, C_p50] 可直接插值。
    """
    hue_deg = np.degrees(np.arctan2(lab[:, 2], lab[:, 1])) % 360.0
    a_ch, b_ch = lab[:, 1], lab[:, 2]
    fam = _hue_family(hue_deg)
    total = len(fam)
    out = []
    for f in range(12):
        m = fam == f
        n = int(m.sum())
        if n < total * min_share:
            continue
        c50 = float(np.percentile(C[m], 50))
        l50 = float(np.percentile(L[m], 50))
        # 代表色：族内最接近 (L50, C50) 的真实像素（保证是图上真有的颜色，不是均值幻影）
        d = np.abs(L[m] - l50) + np.abs(C[m] - c50)
        rep = rgb[m][int(np.argmin(d))]
        rows = chroma_envelope(L[m], C[m], bins=curve_bins, min_share=0.04)
        curve = [[r["L_center"], r["C_p50"]] for r in rows]
        out.append({
            "family": f, "name": HUE_FAMILY_NAMES[f],
            "hue_lo": f * 30, "hue_hi": f * 30 + 30,
            "share": round(n / total, 4), "n_px": n,
            "C_p50": round(c50, 2), "C_p90": round(float(np.percentile(C[m], 90)), 2),
            "C_max": round(float(C[m].max()), 2),
            "L_p50": round(l50, 1),
            "L_min": round(float(L[m].min()), 1), "L_max": round(float(L[m].max()), 1),
            "hue_deg_median": round(float(np.median(hue_deg[m])), 1),
            "a_p50": round(float(np.median(a_ch[m])), 2),
            "b_p50": round(float(np.median(b_ch[m])), 2),
            "curve": curve,
            "rep_rgb": [int(v) for v in rep], "rep_hex": hexc(rep),
        })
    out.sort(key=lambda r: -r["share"])
    return out


def ramp_levels(L, families):
    """明度阶估计：把 L* 聚成「阶」（1.5 单位内合并），统计有效阶数。

    用**全部**像素（含黑描边/白高光）——ramp 阶数是"这张画有几个明度层次"，
    与彩度无关；若只看彩色像素，灰阶稿会误报 0 阶。
    """
    step = 1.5
    keys = np.round(L / step).astype(np.int32)
    uniq, cnt = np.unique(keys, return_counts=True)
    keep = cnt >= max(2, int(0.02 * len(L)))
    levels = sorted({round(float(k * step), 1) for k, c in zip(uniq[keep], cnt[keep])})
    main = families[0] if families else None
    return {"steps_est": int(min(12, len(levels))), "levels": levels,
            "main_family": main["name"] if main else None,
            "main_family_index": main["family"] if main else None}


def neutral_summary(L, C, rgb, c_min):
    """中性结构色（黑描边/白高光/灰面）单独统计——**不许混进色相族**。

    实测坑（complex_tilemap_16px.png，13 色真像素画）：纯黑 C*=0 ⇒ 色相角
    arctan2(0,0)=0° 被归进 "red" 族并占 61.6%，把 dark/peak 压成 0.000，
    于是"彩度随明度"曲线整条失真。中性色是结构，不是风格饱和度。
    """
    m = C < c_min
    if not m.any():
        return None
    l50 = float(np.percentile(L[m], 50))
    d = np.abs(L[m] - l50)
    rep = rgb[m][int(np.argmin(d))]
    return {"share": round(float(m.mean()), 4), "n_px": int(m.sum()),
            "C_p50": round(float(np.percentile(C[m], 50)), 2),
            "L_p50": round(l50, 1),
            "rep_rgb": [int(v) for v in rep], "rep_hex": hexc(rep)}


def shadow_lit_shift(L, a, b):
    """最暗 20% 与最亮 20% 的 (Δa*, Δb*) —— 与 material-light-templates 同口径。"""
    lo_thr, hi_thr = np.percentile(L, 20), np.percentile(L, 80)
    dm, lm = L <= lo_thr, L >= hi_thr
    if not dm.any() or not lm.any():
        return None
    dA = float(a[dm].mean() - a[lm].mean())
    dB = float(b[dm].mean() - b[lm].mean())
    reads = []
    if dA < -3:
        reads.append("shadow greener")
    elif dA > 3:
        reads.append("shadow redder")
    if dB < -3:
        reads.append("shadow bluer/cooler")
    elif dB > 3:
        reads.append("shadow warmer/yellower")
    return {"dA_dark_minus_lit": round(dA, 2), "dB_dark_minus_lit": round(dB, 2),
            "reads_as": ", ".join(reads) if reads else "neutral shift (<3)"}


def classify(uniq_eff, checker_ratio, chroma_rows, families, prominence=None):
    tags = []
    if uniq_eff <= 8:
        tags.append("color-count:ultra-low(<=8)")
    elif uniq_eff <= 16:
        tags.append("color-count:low(<=16)")
    elif uniq_eff <= 32:
        tags.append("color-count:medium(<=32)")
    elif uniq_eff <= 64:
        tags.append("color-count:high(<=64)")
    else:
        tags.append("color-count:very-high(>64)")
    if checker_ratio < 0.01:
        tags.append("dither:none")
    elif checker_ratio <= 0.03:
        tags.append("dither:subtle(<=3%)")
    else:
        tags.append("dither:heavy(>3%)")
    if chroma_rows:
        cs = [r["C_p50"] for r in chroma_rows]
        peak_i = int(np.argmax(cs))
        span = len(cs) - 1
        # 峰值必须有**显著性**：否则 8bit 量化噪声的 argmax 有 ~50% 概率落在中间，
        # 判据就成了"永远可能报 terminator"的假闸门（selftest flatc 负对照专门锁这条）。
        if prominence is not None and prominence < 1.15:
            tags.append("chroma-peak:flat(no significant peak)")
        elif span > 0 and 0.25 <= peak_i / span <= 0.75:
            tags.append("chroma-peak:terminator(middle)")
        elif peak_i == 0:
            tags.append("chroma-peak:dark-end")
        elif peak_i == span:
            tags.append("chroma-peak:lit-end")
        if cs[-1] / max(cs[peak_i], 1e-6) < 0.5:
            tags.append("lit-side:washed-out")
        else:
            tags.append("lit-side:keeps-color")
    if not families or families[0]["C_p50"] < 6:
        tags.append("hue:neutral / unreadable")
    else:
        tags.append("hue-dominant:" + families[0]["name"])
    return tags


def build_profile(paths, name="", bins=10, max_pixels=DEFAULT_MAX_PIXELS, c_min=5.0):
    per = []
    for p in paths:
        m = measure(p, max_pixels=max_pixels)
        if m is not None:
            per.append(m)
    if not per:
        return None

    L = np.concatenate([m["_L"] for m in per])
    C = np.concatenate([m["_C"] for m in per])
    hue = np.concatenate([m["_hue"] for m in per])
    rgb = np.concatenate([m["_rgb"] for m in per])
    lab = PF.srgb_to_lab(rgb)

    # 中性结构色（C* < c_min：黑描边/白高光/灰面）**不参与**风格饱和度曲线与色相族。
    # 它们由 neutral_summary 单独报；理由是几何结构不是风格（见该函数注释里的实测坑）。
    chroma_mask = C >= c_min
    n_chroma = int(chroma_mask.sum())
    enough = n_chroma >= 32
    Lc, Cc = L[chroma_mask], C[chroma_mask]
    rgbc, labc = rgb[chroma_mask], lab[chroma_mask]

    env = chroma_envelope(Lc, Cc, bins=bins) if enough else []
    fams = hue_families(Lc, Cc, labc, rgbc) if enough else []
    ramp = ramp_levels(L, fams)
    shift = shadow_lit_shift(Lc, labc[:, 1], labc[:, 2]) if enough else None
    neutral = neutral_summary(L, C, rgb, c_min)
    # 与 discipline-metrics 同口径：棋盘格能量 = checker_cells / 2x2 单元总数
    # （**不能**拿 opaque_px 当分母——那是"每个像素"而不是"每个 2x2 单元"，会低估）
    tot_cells = int(sum(m["cells"] for m in per))
    checker_ratio = round(sum(m["checker_cells"] for m in per) / max(1, tot_cells), 6)
    tot_op = sum(m["opaque_px"] for m in per)

    peak_i = int(np.argmax([r["C_p50"] for r in env])) if env else -1
    peak_C = max([r["C_p50"] for r in env]) if env else 0.0
    dark_over_peak = round(env[0]["C_p50"] / peak_C, 3) if env and peak_C > 0 else None
    lit_over_peak = round(env[-1]["C_p50"] / peak_C, 3) if env and peak_C > 0 else None
    # 峰值显著性 = 峰值 / 两端较大者。≈1.0 表示"彩度基本不随明度变"（无风格曲线）
    ends = [r["C_p50"] for r in env[:1] + env[-1:]] if env else [0.0]
    prominence = round(peak_C / max(max(ends), 1e-6), 3) if env else None

    eff_union = max(m["effective_colors"] for m in per)
    prof = {
        "kind": "pixel-style-profile", "version": 1,
        "name": name or os.path.splitext(os.path.basename(per[0]["file"]))[0],
        "generated_by": "vision/style-profile.py",
        "chroma_cut": c_min,
        "sources": [{"path": m["path"], "file": m["file"], "size": f"{m['w']}x{m['h']}",
                     "opaque_px": m["opaque_px"], "unique_colors": m["unique_colors"],
                     "effective_colors": m["effective_colors"], "sampled": m["sampled"]}
                    for m in per],
        "totals": {
            "sources": len(per), "opaque_px": tot_op,
            "effective_colors_max": eff_union,
            "measured_px": int(len(C)),
            "chroma_px": n_chroma,
            "neutral_px": int(len(C) - n_chroma),
            "chroma_share": round(n_chroma / max(1, len(C)), 4),
            "alpha_non_binary_px": int(sum(m["alpha_non_binary_px"] for m in per)),
        },
        "lightness": {
            "p5": round(float(np.percentile(L, 5)), 1),
            "p25": round(float(np.percentile(L, 25)), 1),
            "median": round(float(np.percentile(L, 50)), 1),
            "p75": round(float(np.percentile(L, 75)), 1),
            "p95": round(float(np.percentile(L, 95)), 1),
            "hist20": _hist(L, 0, 100, 20),
        },
        "chroma_envelope": {
            "measured_on": "chroma pixels only (C* >= chroma_cut)",
            "bins": env,
            "peak_bin_index": peak_i,
            "peak_L": env[peak_i]["L_center"] if env else None,
            "peak_C": round(float(peak_C), 2),
            "peak_at": round(peak_i / max(1, len(env) - 1), 2) if env else None,
            "peak_prominence": prominence,
            "dark_over_peak": dark_over_peak,
            "lit_over_peak": lit_over_peak,
        },
        "hue_families": fams,
        "neutral": neutral,
        "ramp": ramp,
        "shadow_lit_shift": shift,
        "dither": {
            "checker_cell_ratio": checker_ratio,
            "checker_cells": int(sum(m["checker_cells"] for m in per)),
            "isolated4_px": int(sum(m["isolated4_px"] for m in per)),
            "isolated4_ratio": round(sum(m["isolated4_px"] for m in per) / max(1, tot_op), 4),
            "alt_h_ratio": round(float(np.mean([m["alt"]["h"]["ratio"] for m in per])), 4),
        },
        "style_tags": classify(eff_union, checker_ratio, env, fams, prominence),
    }
    return prof


# ---------------------------------------------------------------- 可视化（给人看）

def render_swatch(prof, path, cell=22):
    W, H = 980, 360
    im = Image.new("RGB", (W, H), (26, 26, 30))
    dr = ImageDraw.Draw(im)
    f11, f13 = _font(11), _font(13)
    dr.text((8, 6), "style profile: %s   (%d source(s), effective colors=%s)"
            % (prof["name"], prof["totals"]["sources"], prof["totals"]["effective_colors_max"]),
            fill=(235, 235, 240), font=f13)
    dr.text((8, 26), "tags: " + " | ".join(prof["style_tags"]), fill=(180, 220, 200), font=f11)

    # 左：色相族（条长 = share，颜色 = 代表色）
    x0, y0 = 10, 56
    dr.text((x0, y0 - 14), "hue families (share %, median C*)", fill=(210, 210, 220), font=f11)
    for i, fam in enumerate(prof["hue_families"][:12]):
        y = y0 + i * (cell - 4)
        w = int(160 * fam["share"])
        dr.rectangle([x0, y, x0 + max(2, w), y + cell - 8], fill=tuple(fam["rep_rgb"]), outline=(70, 70, 78))
        dr.text((x0 + 168, y + 2), "%-11s %5.1f%%  C%5.1f" % (fam["name"], fam["share"] * 100, fam["C_p50"]),
                fill=(205, 205, 212), font=f11)

    # 中：彩度-明度包络
    cx0, cy0, cx1, cy1 = 340, 70, 620, 300
    dr.rectangle([cx0, cy0, cx1, cy1], outline=(80, 80, 90))
    dr.text((cx0, cy0 - 16), "chroma envelope C*(L*)  p50 / p90", fill=(210, 210, 220), font=f11)
    env = prof["chroma_envelope"]["bins"]
    if env:
        cmax = max(r["C_p90"] for r in env) * 1.15 + 1
        def px(L, C):
            return (cx0 + (L / 100.0) * (cx1 - cx0), cy1 - (C / cmax) * (cy1 - cy0))
        pts50 = [px(r["L_center"], r["C_p50"]) for r in env]
        pts90 = [px(r["L_center"], r["C_p90"]) for r in env]
        if len(pts90) > 1:
            dr.line(pts90, fill=(90, 120, 160), width=2)
        if len(pts50) > 1:
            dr.line(pts50, fill=(255, 190, 60), width=3)
        for (x, y) in pts50:
            dr.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(255, 220, 120))
        pk = prof["chroma_envelope"]
        dr.text((cx0, cy1 + 6), "peak L*=%s  C*=%s  at=%.2f"
                % (pk["peak_L"], pk["peak_C"], pk["peak_at"] or 0), fill=(220, 220, 228), font=f11)
        dr.text((cx0, cy1 + 22), "dark/peak=%s  lit/peak=%s"
                % (pk["dark_over_peak"], pk["lit_over_peak"]), fill=(220, 220, 228), font=f11)

    # 右：明度直方图
    hx0, hy0, hx1, hy1 = 660, 70, 960, 300
    dr.rectangle([hx0, hy0, hx1, hy1], outline=(80, 80, 90))
    dr.text((hx0, hy0 - 16), "lightness histogram (L* 0..100, 20 bins)", fill=(210, 210, 220), font=f11)
    hist = prof["lightness"]["hist20"]
    hm = max(hist) if hist else 1
    bw = (hx1 - hx0) / len(hist)
    for i, v in enumerate(hist):
        bh = (v / hm) * (hy1 - hy0 - 4)
        x = hx0 + i * bw
        dr.rectangle([x + 1, hy1 - bh, x + bw - 1, hy1], fill=(120, 150, 190))
    dr.text((hx0, hy1 + 6), "p5=%s  median=%s  p95=%s   ramp steps=%s"
            % (prof["lightness"]["p5"], prof["lightness"]["median"], prof["lightness"]["p95"],
               prof["ramp"]["steps_est"]), fill=(220, 220, 228), font=f11)
    sh = prof["shadow_lit_shift"]
    if sh:
        dr.text((hx0, hy1 + 22), "shadow-lit: dA=%s dB=%s"
                % (sh["dA_dark_minus_lit"], sh["dB_dark_minus_lit"]),
                fill=(220, 200, 190), font=f11)
    ne = prof.get("neutral")
    if ne:
        dr.text((hx0, hy1 + 38), "neutral structural = %.1f%% (excluded from hue families)"
                % (ne["share"] * 100), fill=(200, 210, 220), font=f11)
    im.save(path)
    return path


# ---------------------------------------------------------------- 自检

def _synth_strip(colors, size=(32, 32)):
    """按行分成 len(colors) 条带的测试图（RGBA）。"""
    h, w = size
    im = np.zeros((h, w, 4), np.uint8)
    im[:, :, 3] = 255
    band = h // len(colors)
    for i, c in enumerate(colors):
        y0 = i * band
        y1 = h if i == len(colors) - 1 else (i + 1) * band
        im[y0:y1, :, :3] = c
    return im


def _synth_checker(c1, c2, size=(32, 32)):
    h, w = size
    im = np.zeros((h, w, 4), np.uint8)
    im[:, :, 3] = 255
    yy, xx = np.mgrid[0:h, 0:w]
    m = ((yy + xx) % 2 == 0)
    im[m, :3] = c1
    im[~m, :3] = c2
    return im


def selftest(tmpdir):
    checks = []

    def ck(name, got, want):
        checks.append((name, got, want, got == want))

    os.makedirs(tmpdir, exist_ok=True)
    RF = _mod("ramp-from-template.py", "rf_style")

    # --- 先验：Lab 换算本身能回来（否则后面全是错的）
    worst = 0.0
    for probe in ([200, 60, 50], [90, 45, 40], [245, 235, 222], [60, 80, 60], [180, 30, 30]):
        lab = PF.srgb_to_lab(np.array([probe], np.int32))[0]
        back = RF.lab_to_srgb(float(lab[0]), float(lab[1]), float(lab[2]))
        worst = max(worst, float(np.abs(np.asarray(back) - np.array(probe, float)).max()))
    ck("lab_roundtrip_maxerr_lt_3", worst < 3.0, True)
    print("    (sRGB->Lab->sRGB max err = %.2f)" % worst)

    # --- 正例：4 阶红橙 ramp，彩度峰值在中间（terminator）
    Ls = [20.0, 40.0, 60.0, 80.0]
    Cs = [30.0, 55.0, 70.0, 40.0]
    ang = np.radians(45.0)
    cols = []
    for Lv, Cv in zip(Ls, Cs):
        rgbv = RF.lab_to_srgb(Lv, Cv * np.cos(ang), Cv * np.sin(ang))
        cols.append([int(round(v)) for v in rgbv])
    p_good = os.path.join(tmpdir, "style_good.png")
    Image.fromarray(_synth_strip(cols), "RGBA").save(p_good)
    prof = build_profile([p_good], name="synth_good")
    ck("good.effective_colors", prof["totals"]["effective_colors_max"], 4)
    ck("good.main_family_share_gt_90", prof["hue_families"][0]["share"] > 0.90, True)
    ck("good.peak_at_middle", 0.25 <= prof["chroma_envelope"]["peak_at"] <= 0.75, True)
    ck("good.peak_C_near_70", abs(prof["chroma_envelope"]["peak_C"] - 70) < 6, True)
    ck("good.checker_zero", prof["dither"]["checker_cell_ratio"], 0.0)
    ck("good.tag_terminator", "chroma-peak:terminator(middle)" in prof["style_tags"], True)
    ck("good.prominence_gt_1_5", prof["chroma_envelope"]["peak_prominence"] > 1.5, True)

    # --- 负对照 1：纯平色 -> 必须报不出 ramp、报不出彩度峰值
    p_flat = os.path.join(tmpdir, "style_flat.png")
    Image.fromarray(_synth_strip([[128, 128, 128]] * 4), "RGBA").save(p_flat)
    pf = build_profile([p_flat], name="synth_flat")
    ck("flat.effective_colors", pf["totals"]["effective_colors_max"], 1)
    ck("flat.ramp_steps_le_1", pf["ramp"]["steps_est"] <= 1, True)
    ck("flat.tag_neutral", "hue:neutral / unreadable" in pf["style_tags"], True)

    # --- 负对照 2：棋盘格 -> 抖动度量必须有牙（改的是"测量真正依赖的量"）
    p_chk = os.path.join(tmpdir, "style_checker.png")
    Image.fromarray(_synth_checker([200, 60, 50], [150, 45, 38], (32, 32)), "RGBA").save(p_chk)
    pc = build_profile([p_chk], name="synth_checker")
    ck("checker.ratio_gt_50pct", pc["dither"]["checker_cell_ratio"] > 0.5, True)
    ck("checker.tag_heavy", "dither:heavy(>3%)" in pc["style_tags"], True)

    # --- 负对照 3：灰阶 3 阶 -> 彩度≈0，色相不可判定（但 ramp 仍可读）
    p_gray = os.path.join(tmpdir, "style_gray.png")
    Image.fromarray(_synth_strip([[40, 40, 40], [128, 128, 128], [210, 210, 210]]), "RGBA").save(p_gray)
    pg = build_profile([p_gray], name="synth_gray")
    ck("gray.no_chroma_family", len(pg["hue_families"]), 0)
    ck("gray.neutral_share_1", (pg["neutral"] or {}).get("share", 0) > 0.99, True)
    ck("gray.tag_neutral", "hue:neutral / unreadable" in pg["style_tags"], True)
    ck("gray.ramp_readable", pg["ramp"]["steps_est"] >= 3, True)

    # --- 负对照 4：彩度全平的图 -> 不应报 terminator 峰值
    p_flatc = os.path.join(tmpdir, "style_flatc.png")
    flat_cols = []
    for Lv in (20.0, 40.0, 60.0, 80.0):
        rgbv = RF.lab_to_srgb(Lv, 50.0 * np.cos(ang), 50.0 * np.sin(ang))
        flat_cols.append([int(round(v)) for v in rgbv])
    Image.fromarray(_synth_strip(flat_cols), "RGBA").save(p_flatc)
    pfc = build_profile([p_flatc], name="synth_flatc")
    ck("flatc.tag_terminator_absent", "chroma-peak:terminator(middle)" in pfc["style_tags"], False)
    ck("flatc.tag_flat", "chroma-peak:flat(no significant peak)" in pfc["style_tags"], True)
    ck("flatc.prominence_near_1", abs(pfc["chroma_envelope"]["peak_prominence"] - 1.0) < 0.15, True)

    # --- 负对照 5：黑描边 + 彩色块 -> 纯黑不许混进色相族（真资产 complex_tilemap 暴露的 bug）
    p_edge = os.path.join(tmpdir, "style_edge.png")
    eim = np.zeros((32, 32, 4), np.uint8)
    eim[:, :, 3] = 255
    eim[:, :, :3] = [0, 0, 0]
    eim[8:24, 8:24, :3] = [200, 60, 50]
    Image.fromarray(eim, "RGBA").save(p_edge)
    pe = build_profile([p_edge], name="synth_edge")
    ck("edge.neutral_share_gt_50", (pe["neutral"] or {}).get("share", 0) > 0.5, True)
    ck("edge.black_not_in_family", pe["hue_families"][0]["rep_hex"] != "#000000", True)
    ck("edge.family_is_red", pe["hue_families"][0]["name"] in ("red", "orange"), True)
    ck("edge.chroma_share_near_25pct", abs(pe["totals"]["chroma_share"] - 0.25) < 0.03, True)

    bad = [c for c in checks if not c[3]]
    for name, got, want, ok in checks:
        print("  [%s] %s: got=%s want=%s" % ("ok" if ok else "FAIL", name, got, want))
    print("SELFTEST=%s checks=%d/%d" % ("PASS" if not bad else "FAIL", len(checks) - len(bad), len(checks)))
    return 0 if not bad else 1


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", action="append", default=[], help="参考图，可重复")
    ap.add_argument("--glob", default="", help="glob 参考图（如 refs/*.png）")
    ap.add_argument("--name", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--swatch", default="")
    ap.add_argument("--bins", type=int, default=10)
    ap.add_argument("--c-min", type=float, default=5.0,
                    help="彩度下限：C* 低于它的像素算中性结构色，不参与色相族/彩度包络")
    ap.add_argument("--max-pixels", type=int, default=DEFAULT_MAX_PIXELS)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        tmp = os.path.join(HERE, "_selftest_tmp", "style")
        sys.exit(selftest(tmp))

    paths = list(a.image)
    if a.glob:
        paths += sorted(globmod.glob(a.glob))
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        print("ERROR: no input image (use --image or --glob)")
        sys.exit(2)

    prof = build_profile(paths, name=a.name, bins=a.bins, max_pixels=a.max_pixels, c_min=a.c_min)
    if prof is None:
        print("ERROR: no usable pixels in inputs")
        sys.exit(2)

    print("SOURCES=%d  effective_colors=%d  alpha_non_binary=%d"
          % (len(prof["sources"]), prof["totals"]["effective_colors_max"],
             prof["totals"]["alpha_non_binary_px"]))
    print("LIGHTNESS p5=%.1f median=%.1f p95=%.1f  ramp_steps=%d"
          % (prof["lightness"]["p5"], prof["lightness"]["median"],
             prof["lightness"]["p95"], prof["ramp"]["steps_est"]))
    print("CHROMA peak L*=%.1f C*=%.2f at=%.2f prom=%.3f  dark/peak=%.3f lit/peak=%.3f"
          % (prof["chroma_envelope"]["peak_L"] or 0, prof["chroma_envelope"]["peak_C"],
             prof["chroma_envelope"]["peak_at"] or 0,
             prof["chroma_envelope"]["peak_prominence"] or 0,
             prof["chroma_envelope"]["dark_over_peak"] or 0,
             prof["chroma_envelope"]["lit_over_peak"] or 0))
    for fam in prof["hue_families"][:6]:
        print("  HUE %-11s share=%5.1f%%  C*=%5.1f  L*=%5.1f  rep=%s"
              % (fam["name"], fam["share"] * 100, fam["C_p50"], fam["L_p50"], fam["rep_hex"]))
    print("DITHER checker=%.4f isolated=%.4f" % (prof["dither"]["checker_cell_ratio"],
                                                 prof["dither"]["isolated4_ratio"]))
    print("TAGS=" + ",".join(prof["style_tags"]))

    if a.out:
        save_json(prof, a.out)
        print("JSON=" + a.out)
    if a.swatch:
        print("SWATCH=" + render_swatch(prof, a.swatch))


if __name__ == "__main__":
    main()
