# light-audit.py — 光影审计（把 doctrine 的规则变成可算数字，见 docs/LIGHTING-STUDY.md）
# 规则 → 判据：
#   R2 "阴影比亮部更不饱和"      => C*_shadow < C*_lit
#   R3 "明暗过渡带是饱和的"      => C*_edge > C*_blend（两侧均值线性混合的彩度）
#   R5 "别往阴影里掺黑"          => C*_dark / C*_mid 不塌（阈值 0.6）
#   R1 "天空冷/地面暖、环境不是常数" => 阴影相对亮部的 (Δa*,Δb*) 方向（应偏冷/偏蓝，且量级可读）
# 明暗区域的定义**不依赖光源**：族内按 CIE L* 分位 —— 亮部 = p75 以上、阴影 = p25 以下、过渡带 = p45~p55。
# 用法：python light-audit.py --image A.png [--json-out J] | --glob "*.png" | --selftest
import os
import sys
import glob
import argparse
import importlib.util

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _vlib import load_rgba, save_json  # noqa: E402


def _mod(name):
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").replace(".py", ""), p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PF = _mod("palette-forge.py")
RS = _mod("_lightlib.py")   # 光影共享原语（原 relight-split.py 已退役，函数迁至 _lightlib）


def audit_bins(path, bins=10, sel_hue=None, sel_cmin=0.0):
    """箱式审计（**连续调也适用**）：按 CIE L* 分位分箱，量"彩度/色相随明度的曲线"。

    ⚠ 必须先按材质选区再分箱：实测原图最亮的箱由**白斑点**（近无彩度 C*≈21）主导，
    而红伞盖亮面其实很饱和 —— 不分材质就分箱会把两种材质混进同一箱，结论全错。
    材料选区 = Lab 色相角窗口 + 最小彩度（`--sel-hue 0,45 --sel-cmin 30` 即"红伞盖"）。

    规则 → 判据：
      R2 阴影更不饱和 => 最暗箱 C* < 最亮箱 C*
      R3 过渡带饱和   => C* 峰值落在中间箱
      R5 别往阴影掺黑 => 最暗箱 C* / 中间箱 C* >= 0.6
      R1 环境有色     => 最暗箱相对最亮箱的 (Δa*,Δb*) 方向与量级
    """
    rgba, _ = load_rgba(path)
    opaque = rgba[:, :, 3] > 127
    rgb = rgba[:, :, :3]
    if int(opaque.sum()) < 64:
        return None
    lab = PF.srgb_to_lab(rgb[opaque].reshape(-1, 3))
    L, a, b = lab[:, 0], lab[:, 1], lab[:, 2]
    C = np.hypot(a, b)
    hue = np.degrees(np.arctan2(b, a)) % 360.0
    keep = C >= sel_cmin
    if sel_hue:
        keep &= (hue >= sel_hue[0]) & (hue <= sel_hue[1])
    # 小图/低色数适配：像素画只有几个明度阶，128px 下限与 10 箱都不适用
    min_px = 24
    if int(keep.sum()) < min_px:
        return None
    L, a, b, C = L[keep], a[keep], b[keep], C[keep]
    n_levels = len(np.unique(np.round(L, 1)))
    if n_levels <= 12:
        # 像素画（低色数）：**按明度阶分箱**，每阶一箱。
        # 用分位边界会把两个相邻阶并进同一箱（实测 4 阶被压成 3 箱、暗/峰比被算成 1.0）⇒ 判据失真。
        lv = np.unique(np.round(L, 1))
        edges = np.concatenate([[lv[0] - 0.05], (lv[:-1] + lv[1:]) / 2.0, [lv[-1] + 0.05]])
        bins = len(lv)
    else:
        bins = max(2, min(bins, n_levels))
        edges = np.percentile(L, np.linspace(0, 100, bins + 1))
    rows = []
    for i in range(bins):
        lo, hi = edges[i], edges[i + 1]
        m = (L >= lo) & (L <= hi) if i == bins - 1 else (L >= lo) & (L < hi)
        if m.sum() == 0:
            continue
        rows.append({"bin": i, "L_lo": round(float(lo), 1), "L_hi": round(float(hi), 1),
                     "share": round(float(m.mean()), 4),
                     "C_mean": round(float(C[m].mean()), 2),
                     "a_mean": round(float(a[m].mean()), 2), "b_mean": round(float(b[m].mean()), 2)})
    if len(rows) < 3:
        return None
    Cs = [r["C_mean"] for r in rows]
    dark, lit = rows[0], rows[-1]
    mid_i = len(rows) // 2
    peak_i = int(np.argmax(Cs))
    R2 = dark["C_mean"] < lit["C_mean"]
    R3 = 0.25 * len(rows) <= peak_i <= 0.75 * len(rows)
    # R5 阈值标定：以"专业真值"实测为准 —— 红伞盖 dark/mid = 0.574（正常阴影），
    # 原用 0.6 会把参考图自己判成"暗阶彩度崩塌"⇒ 阈值放到 0.4（真崩塌是接近 0）。
    R5 = (dark["C_mean"] / rows[mid_i]["C_mean"]) >= 0.4 if rows[mid_i]["C_mean"] > 0 else None
    return {
        "image": os.path.basename(path), "path": path, "mode": "bins",
        "size": f"{rgba.shape[1]}x{rgba.shape[0]}", "opaque_px": int(opaque.sum()),
        "unique_colors": int(len(np.unique(rgb[opaque].reshape(-1, 3), axis=0))),
        "C_dark": dark["C_mean"], "C_lit": lit["C_mean"], "C_mid": rows[mid_i]["C_mean"],
        "C_peak_bin": peak_i, "peak_at": round(peak_i / max(1, len(rows) - 1), 2),
        "dA_dark_minus_lit": round(dark["a_mean"] - lit["a_mean"], 2),
        "dB_dark_minus_lit": round(dark["b_mean"] - lit["b_mean"], 2),
        "verdict": {"R2_shadow_less_saturated": bool(R2), "R3_peak_in_middle": bool(R3),
                    "R5_dark_not_collapsed": R5},
        "bins": rows,
    }


def audit(path, c_thr=8.0):
    rgba, _ = load_rgba(path)
    opaque = rgba[:, :, 3] > 127
    rgb = rgba[:, :, :3]
    if int(opaque.sum()) < 64:
        return None
    cols, cnt = np.unique(rgb[opaque].reshape(-1, 3).astype(np.int32), axis=0, return_counts=True)
    fams, LabU = RS.material_families(cols, c_thr=c_thr)
    LabAll = PF.srgb_to_lab(rgb[opaque].reshape(-1, 3))

    fam_rows, agg_sh, agg_li, wsum = [], [], [], 0.0
    for m in fams:
        if len(m) < 2:
            continue
        lab = LabU[m]
        # 该族像素的 L*（按颜色取用，用计数加权）
        L = lab[:, 0]
        p25, p45, p55, p75 = (float(np.percentile(L, q)) for q in (25, 45, 55, 75))
        C = np.sqrt(lab[:, 1] ** 2 + lab[:, 2] ** 2)
        lit = L >= p75
        sh = L <= p25
        edge = (L >= p45) & (L <= p55)
        if lit.sum() == 0 or sh.sum() == 0:
            continue
        w = cnt[m].astype(np.float64)
        wl, ws, we = w[lit].sum(), w[sh].sum(), w[edge].sum()
        if wl <= 0 or ws <= 0:
            continue
        C_lit = float((C[lit] * w[lit]).sum() / wl)
        C_sh = float((C[sh] * w[sh]).sum() / ws)
        C_ed = float((C[edge] * w[edge]).sum() / we) if we > 0 else float("nan")
        a_lit = float((lab[lit, 1] * w[lit]).sum() / wl); b_lit = float((lab[lit, 2] * w[lit]).sum() / wl)
        a_sh = float((lab[sh, 1] * w[sh]).sum() / ws); b_sh = float((lab[sh, 2] * w[sh]).sum() / ws)
        # 过渡带"线性混合"应有的彩度：亮/暗均值色按 L* 比例混合
        C_blend = float(np.hypot((a_lit + a_sh) / 2, (b_lit + b_sh) / 2))
        mid = (~lit) & (~sh)
        C_mid = float((C[mid] * w[mid]).sum() / w[mid].sum()) if mid.any() else float("nan")
        fam_rows.append({
            "n_colors": int(len(m)), "weight": int(w.sum()),
            "C_lit": round(C_lit, 2), "C_shadow": round(C_sh, 2), "C_edge": round(C_ed, 2),
            "C_mid": round(C_mid, 2), "C_blend": round(C_blend, 2),
            "shadow_over_lit": round(C_sh / C_lit, 3) if C_lit > 0 else None,
            "edge_over_blend": round(C_ed / C_blend, 3) if C_blend > 0 else None,
            "dark_over_mid": round(C_sh / C_mid, 3) if C_mid and C_mid > 0 else None,
            "dA_shadow_minus_lit": round(a_sh - a_lit, 2), "dB_shadow_minus_lit": round(b_sh - b_lit, 2),
            "R2_shadow_less_saturated": bool(C_sh < C_lit),
            "R3_edge_more_saturated": bool(C_ed > C_blend) if we > 0 else None,
            "R5_dark_not_collapsed": bool((C_sh / C_mid) >= 0.6) if C_mid and C_mid > 0 else None,
        })
        agg_sh.append((C_sh, w.sum())); agg_li.append((C_lit, w.sum())); wsum += w.sum()

    if not fam_rows:
        return None
    C_sh_all = sum(c * w for c, w in agg_sh) / sum(w for _, w in agg_sh)
    C_lit_all = sum(c * w for c, w in agg_li) / sum(w for _, w in agg_li)
    r2 = sum(1 for r in fam_rows if r["R2_shadow_less_saturated"])
    r3 = sum(1 for r in fam_rows if r["R3_edge_more_saturated"])
    r5 = sum(1 for r in fam_rows if r["R5_dark_not_collapsed"])
    return {
        "image": os.path.basename(path), "path": path,
        "size": f"{rgba.shape[1]}x{rgba.shape[0]}", "opaque_px": int(opaque.sum()),
        "colors": int(len(cols)), "n_families": len(fam_rows),
        "C_lit_all": round(C_lit_all, 2), "C_shadow_all": round(C_sh_all, 2),
        "families_pass_R2": r2, "families_pass_R3": r3, "families_pass_R5": r5,
        "verdict": {"R2": r2 == len(fam_rows), "R3": r3 == len(fam_rows), "R5": r5 == len(fam_rows)},
        "families": fam_rows,
    }


def selftest():
    checks = []

    def ck(n, g, w):
        checks.append((n, g, w, g == w))
    from PIL import Image
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest_tmp", "audit")
    os.makedirs(tmp, exist_ok=True)
    S = 60
    img = np.zeros((S, S, 4), np.uint8)
    img[:, :, 3] = 255
    # 同一材质（同色相红）的 3 个明度层，暗部**更不饱和** => 符合 R2
    # 关键：明暗必须落进**同一个族**才有意义（族是按色相/彩度聚的，色相要接近）
    img[:, 0:S // 3, :3] = (220, 60, 50)        # 亮、最饱和
    img[:, S // 3:2 * S // 3, :3] = (150, 60, 55)   # 中
    img[:, 2 * S // 3:, :3] = (90, 50, 50)      # 暗、最不饱和
    p1 = os.path.join(tmp, "good.png")
    Image.fromarray(img, "RGBA").save(p1)
    r1 = audit_bins(p1, bins=6)
    ck("good.not_none", r1 is not None, True)
    ck("good.R2", (r1 or {}).get("verdict", {}).get("R2_shadow_less_saturated"), True)

    # 反例：暗部**更饱和**（亮部故意做浅淡） => R2 必须判否（负对照，证明量尺有牙）
    # ⚠ 断言里的数值必须是实测确认过的：上一版我"以为" (140,18,18) 比 (220,60,50) 更饱和，实测相反 ⇒ 假负对照
    img2 = img.copy()
    img2[:, 0:S // 3, :3] = (230, 130, 110)        # 亮但浅淡（低彩度）
    img2[:, S // 3:2 * S // 3, :3] = (200, 90, 80)
    img2[:, 2 * S // 3:, :3] = (150, 10, 5)        # 暗但高彩度
    p2 = os.path.join(tmp, "bad.png")
    Image.fromarray(img2, "RGBA").save(p2)
    r2 = audit_bins(p2, bins=6)
    ck("bad.R2_false", (r2 or {}).get("verdict", {}).get("R2_shadow_less_saturated"), False)
    ck("bad.reports_bins", len((r2 or {}).get("bins", [])) >= 3, True)

    bad = [c for c in checks if not c[3]]
    for n, g, w, ok in checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {n}: got={g} want={w}")
    print(f"SELFTEST={'PASS' if not bad else 'FAIL'} checks={len(checks) - len(bad)}/{len(checks)}")
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="")
    ap.add_argument("--glob", default="")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--sel-hue", default="", help="材质选区：Lab 色相角窗口，如 0,45（红伞盖）")
    ap.add_argument("--sel-cmin", type=float, default=0.0, help="材质选区：最小彩度")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    targets = ([a.image] if a.image else []) + [p for pat in a.glob.split(";") if pat
                                               for p in sorted(glob.glob(pat, recursive=True))]
    if not targets:
        ap.error("--image or --glob required")
    sel_hue = tuple(float(v) for v in a.sel_hue.split(",")) if a.sel_hue else None
    rows = [r for r in (audit_bins(t, sel_hue=sel_hue, sel_cmin=a.sel_cmin) for t in targets) if r]
    hdr = (f"{'image':34s} {'cols':>6s} {'C*dark':>7s} {'C*mid':>6s} {'C*lit':>6s} {'peak@':>6s} "
           f"{'d(ab)':>14s} {'R2':>4s} {'R3':>4s} {'R5':>4s}")
    print(hdr); print("-" * len(hdr))
    for r in rows:
        v = r["verdict"]
        print(f"{r['image'][:34]:34s} {r['unique_colors']:6d} {r['C_dark']:7.2f} {r['C_mid']:6.2f} "
              f"{r['C_lit']:6.2f} {r['peak_at']:6.2f} "
              f"({r['dA_dark_minus_lit']:6.2f},{r['dB_dark_minus_lit']:6.2f}) "
              f"{'PASS' if v['R2_shadow_less_saturated'] else 'FAIL':>4s} "
              f"{'PASS' if v['R3_peak_in_middle'] else 'FAIL':>4s} "
              f"{'PASS' if v.get('R5_dark_not_collapsed') else 'FAIL':>4s}")
    for r in rows:
        print(f"    [{r['image'][:24]}] 箱曲线 C*: " +
              " ".join(f"{b['L_lo']:.0f}->{b['C_mean']:.1f}" for b in r["bins"]))
    if a.json_out:
        save_json({"rows": rows}, a.json_out)
        print(f"JSON={a.json_out}")


if __name__ == "__main__":
    main()
