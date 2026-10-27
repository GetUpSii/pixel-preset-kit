#!/usr/bin/env python
# _lightlib.py — 光影共享库
#
# 来源：从已退役的 `relight-split.py` 抽出（逐像素重映射路线废弃，但这几个
# **几何/统计原语**被 light-audit / light-direction / light-rig / ref-analyze 当共享库复用）。
# 为什么必须独立成库：直接删 relight-split.py 会让这 4 个工具**一起 import 失败**
# （实测：`_mod("relight-split.py")` 出现在 5 个文件里）。
# 抽出后，"退役的路线"与"仍在用的原语"解耦——删路线不再牵连原语。
#
# 逐字搬移，逻辑未改动（搬家不改行为）；补了 `--selftest` 覆盖搬过来的每个函数，
# 所以这次退役**不丢断言**（relight-split 原有的 8 条自检测的是它的主流程，随主流程一起退役）。
#
# 用法：python _lightlib.py --selftest
import argparse
import importlib.util
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _mod(name):
    p = os.path.join(HERE, name)
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").replace(".py", ""), p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PF = _mod("palette-forge.py")


def erode4(m):
    """4 邻域腐蚀（纯 numpy）。"""
    e = m.copy()
    e[1:, :] &= m[:-1, :]
    e[:-1, :] &= m[1:, :]
    e[:, 1:] &= m[:, :-1]
    e[:, :-1] &= m[:, 1:]
    return e


def box_blur(a, radius, passes=2):
    """小图够用的盒式模糊（不引 scipy）。半径 0 原样返回。"""
    if radius <= 0:
        return a.astype(np.float64)
    out = a.astype(np.float64)
    for _ in range(passes):
        acc = out.copy()
        for d in range(1, radius + 1):
            acc += np.roll(out, d, axis=1) + np.roll(out, -d, axis=1)
            acc += np.roll(out, d, axis=0) + np.roll(out, -d, axis=0)
        out = acc / (1 + 4 * radius)
    return out


def shade_field(opaque, light, blur_radius=3, nz=0.6, mix_volume=0.3):
    """日照场 0..1。

    关键：**距离变换是整数阶梯，直接求梯度会得到噪声法线**（实测 shade 左 0.501/右 0.575、
    极值落在中心附近 = 方向完全错）。必须先平滑距离场，再用法线 n=(-∇dt, nz)。
    方向项之外再叠一个体积项（半球高度），否则平面内部没有体积感。
    """
    dt = np.zeros(opaque.shape, np.float64)
    cur = opaque.copy()
    for d in range(1, 400):
        cur = erode4(cur)
        if not cur.any():
            break
        dt[cur] = d
    dts = box_blur(dt, blur_radius, passes=2)
    gy, gx = np.gradient(dts)
    nn = np.sqrt(gx * gx + gy * gy + nz * nz) + 1e-9
    lx, ly = light
    s_dir = ((-gx) * lx + (-gy) * ly + nz * 0.45) / nn
    r = dt.max()
    h = np.sqrt(np.clip(1.0 - (dt / r) ** 2, 0.0, 1.0)) if r > 0 else dt

    def unit(v):
        v = v[opaque] if opaque.any() else v.reshape(-1)
        lo, hi = v.min(), v.max()
        return (v - lo) / (hi - lo) if hi > lo else np.zeros_like(v)
    s_dir_u = unit(s_dir)
    h_u = unit(h)
    s = (1.0 - mix_volume) * s_dir_u + mix_volume * h_u
    out = np.zeros(opaque.shape, np.float64)
    out[opaque] = s
    return out


def material_families(cols, c_thr=8.0, min_size=2, seed=0):
    """把该件的颜色分成材质族（色相优先 + 中性独立），族内按 L* 升序。

    返回 (fams, Lab)：fams 是成员下标数组的列表（每个已按 L* 排序），Lab 是全部颜色的 Lab。
    """
    Lab = PF.srgb_to_lab(cols)
    C, H = PF.chroma_hue(Lab)
    neutral = C < c_thr
    fams = []
    if (~neutral).any():
        k = max(1, int(round(np.sqrt((~neutral).sum() / 2.0))))
        F = np.stack([np.cos(H[~neutral]), np.sin(H[~neutral])], 1)
        _, cl = PF.wkmeans(F, np.ones(int((~neutral).sum())), k, seed=seed)
        for j in sorted(set(cl.tolist())):
            fams.append(np.nonzero(~neutral)[0][cl == j])
    if neutral.any():
        fams.append(np.nonzero(neutral)[0])
    out = []
    for m in fams:
        m = np.asarray(m)
        m = m[np.argsort(Lab[m][:, 0])]
        if len(m) >= min_size:
            out.append(m)
    return out, Lab


def band_stats(rgb, opaque, frac=0.4):
    """底部带（bbox 下 frac）的阶数与 CIE L* 跨度。

    用户反馈"锅下半部分黑得很明显、没有渐变光" => 直接把"底部带有几个色阶、明度跨度多大"量出来，
    否则改法只能靠肉眼猜。gradient_ok = 底部带至少用到 2 个色阶。
    """
    ys, xs = np.nonzero(opaque)
    if len(xs) == 0:
        return {"colors": 0, "l_min": 0.0, "l_max": 0.0, "l_std": 0.0, "gradient_ok": False}
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
    band = np.zeros_like(opaque)
    band[int(y0 + (1 - frac) * (y1 - y0)):y1, x0:x1] = True
    band &= opaque
    if not band.any():
        return {"colors": 0, "l_min": 0.0, "l_max": 0.0, "l_std": 0.0, "gradient_ok": False}
    cols = np.unique(rgb[band], axis=0).astype(np.int32)
    L = PF.srgb_to_lab(cols)[:, 0]
    return {"colors": int(len(cols)), "l_min": round(float(L.min()), 1),
            "l_max": round(float(L.max()), 1), "l_std": round(float(L.std()), 2),
            "gradient_ok": bool(len(cols) >= 2)}


# ---------------------------------------------------------------- 自检

def selftest():
    checks = []

    def ck(name, got, want):
        checks.append((name, got, want, got == want))

    # --- erode4：14x14 里嵌 10x10 实心块 -> 腐蚀后 8x8 = 64 px
    m = np.zeros((14, 14), bool)
    m[2:12, 2:12] = True
    e = erode4(m)
    ck("erode4.area", int(e.sum()), 64)
    ck("erode4.strictly_smaller", bool(e.sum() < m.sum()), True)
    ck("erode4.empty_stays_empty", int(erode4(np.zeros((5, 5), bool)).sum()), 0)

    # --- box_blur：半径 0 原样；对称；常数图仍是常数（否则模糊器有偏）
    a = np.zeros((21, 21))
    a[10, 10] = 1.0
    b0 = box_blur(a, 0)
    ck("blur.radius0_identity", bool((b0 == a).all()), True)
    b = box_blur(a, 3, passes=1)
    ck("blur.peak_at_center", bool(b[10, 10] == b.max()), True)
    ck("blur.symmetric_h", abs(float(b[10, 10 + 3] - b[10, 10 - 3])) < 1e-9, True)
    ck("blur.symmetric_v", abs(float(b[10 + 3, 10] - b[10 - 3, 10])) < 1e-9, True)
    const = np.full((11, 11), 7.0)
    ck("blur.const_preserved", bool(np.allclose(box_blur(const, 2), 7.0)), True)

    # --- shade_field：合成圆盘，光源方向必须真的响应
    S = 41
    yy, xx = np.mgrid[0:S, 0:S]
    disk = (yy - 20) ** 2 + (xx - 20) ** 2 <= 16 ** 2
    nw = (-0.7071, -0.7071)
    se = (0.7071, 0.7071)
    s_nw = shade_field(disk, nw)
    s_se = shade_field(disk, se)

    def quadrant_mean(s, y0, y1, x0, x1):
        sub = s[y0:y1, x0:x1]
        msk = disk[y0:y1, x0:x1]
        return float(sub[msk].mean()) if msk.any() else 0.0
    nw_ul = quadrant_mean(s_nw, 4, 20, 4, 20)
    nw_lr = quadrant_mean(s_nw, 21, 37, 21, 37)
    se_ul = quadrant_mean(s_se, 4, 20, 4, 20)
    se_lr = quadrant_mean(s_se, 21, 37, 21, 37)
    ck("shade.nw_bright_at_upper_left", nw_ul > nw_lr, True)
    ck("shade.se_bright_at_lower_right", se_lr > se_ul, True)
    # 负对照：换光源必须**反转**亮暗关系（不是"到处都亮"）
    ck("shade.light_direction_actually_matters", bool((nw_ul > nw_lr) != (se_ul > se_lr)), True)
    ck("shade.zero_outside", float(np.abs(s_nw[~disk]).max()), 0.0)
    ck("shade.range_0_1", bool(s_nw[disk].min() >= -1e-9 and s_nw[disk].max() <= 1 + 1e-9), True)

    # --- material_families：核心契约是"**中性色单独成族、不混进彩色族**"
    #     （族数不是"每个色相一族"，而是 sqrt(n_chroma/2) 的启发式 —— 别按直觉断言族数）
    cols = np.array([[200, 40, 40], [120, 20, 20], [40, 180, 60], [90, 60, 200],
                     [0, 0, 0], [255, 255, 255], [128, 128, 128]], np.int32)
    fams, Lab = material_families(cols, c_thr=8.0)
    fam_sets = [set(f.tolist()) for f in fams]
    neutral_idx = {4, 5, 6}                                  # 黑 / 白 / 灰
    nfam = [s for s in fam_sets if s & neutral_idx]
    ck("families.neutral_isolated", len(nfam) == 1 and nfam[0] == neutral_idx, True)
    ck("families.chroma_excludes_neutral",
       all(not (s & neutral_idx) for s in fam_sets if s is not nfam[0]), True)
    ck("families.all_indices_covered",
       sorted(set().union(*fam_sets)) == list(range(len(cols))), True)
    ascending = all(bool(np.all(np.diff(Lab[f][:, 0]) >= 0)) for f in fams)
    ck("families.L_ascending_within_family", ascending, True)
    # 色相差异大 + 数量多 -> 必须真的分出多个彩色族（否则"分族"是假的）
    many = np.array([[220, 30, 30], [30, 200, 60], [40, 80, 220],
                     [230, 200, 40], [160, 40, 200], [30, 200, 200]], np.int32)
    mfams, _ = material_families(many, c_thr=8.0)
    ck("families.multi_hue_splits", len(mfams) >= 2, True)
    # 负对照：全中性色 -> 只有 1 族
    gray = np.array([[0, 0, 0], [100, 100, 100], [255, 255, 255]], np.int32)
    gfams, _ = material_families(gray, c_thr=8.0)
    ck("families.all_gray_single_family", len(gfams), 1)

    # --- band_stats：底部有 2 阶 -> gradient_ok；纯平 -> False；全透明 -> colors 0
    img = np.zeros((20, 20, 3), np.int32)
    op = np.zeros((20, 20), bool)
    op[2:18, 2:18] = True
    img[op] = [150, 90, 60]
    img[14:18, 2:18] = [70, 40, 30]                          # 底部带两阶
    bs = band_stats(img, op, frac=0.4)
    ck("band.gradient_ok", bs["gradient_ok"], True)
    ck("band.colors_2", bs["colors"], 2)
    flat = np.zeros((20, 20, 3), np.int32)
    flat[op] = [120, 120, 120]
    bs_flat = band_stats(flat, op, frac=0.4)
    ck("band.flat_not_gradient", bs_flat["gradient_ok"], False)
    ck("band.flat_colors_1", bs_flat["colors"], 1)
    bs_empty = band_stats(np.zeros((20, 20, 3), np.int32), np.zeros((20, 20), bool))
    ck("band.empty_reports_zero", bs_empty["colors"], 0)

    bad = [c for c in checks if not c[3]]
    for name, got, want, ok in checks:
        print("  [%s] %s: got=%s want=%s" % ("ok" if ok else "FAIL", name, got, want))
    print("SELFTEST=%s checks=%d/%d" % ("PASS" if not bad else "FAIL",
                                        len(checks) - len(bad), len(checks)))
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    print("_lightlib.py is a shared library (erode4/box_blur/shade_field/material_families/band_stats).")
    print("Run with --selftest.")


if __name__ == "__main__":
    main()
