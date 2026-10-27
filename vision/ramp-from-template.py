# ramp-from-template.py — 从「材质光响应模板」反推材质色阶（颜色由实测曲线算出，不是拍脑袋）
# 原理：模板给了 C*(L*) 曲线 + 峰值色相 (a*,b*)；按**质量加权 L* 分位**取 N 个阶，
#       每个阶的 C* 从曲线插值得到，色相取模板峰值色相 => Lab -> sRGB（超色域就降 C* 直到落回）。
# 产出：<name>_ramp.gpl / .json + 色卡 PNG（给人看）
# 用法：python ramp-from-template.py --template spec/material-light-templates.json --name cap_red --steps 4 \
#         --out-gpl O.gpl --out-json O.json --out-swatch O.png
import os
import sys
import json
import argparse

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _vlib import save_json, write_gpl, _font, hexc  # noqa: E402

M = np.array([[0.4124564, 0.3575761, 0.1804375],
              [0.2126729, 0.7151522, 0.0721750],
              [0.0193339, 0.1191920, 0.9503041]])
WHITE = np.array([0.95047, 1.0, 1.08883])


def lab_to_srgb(L, a, b, report_clip=False):
    """Lab -> sRGB。report_clip=True 时返回 (rgb, clipped)：clipped 表示线性 RGB 出界（超色域）。"""
    fy = (L + 16) / 116.0
    fx, fz = fy + a / 500.0, fy - b / 200.0
    e, k = 216.0 / 24389.0, 24389.0 / 27.0
    t0 = 6.0 / 29.0

    def finv(t):
        t3 = t ** 3
        if t3 > e:
            return t3
        if t > t0:
            return (116 * t - 16) / k
        return 3 * t0 * t0 * (t - 4.0 / 29.0)          # 低分支必须带 (t-4/29)（我上一版漏了）

    xyz = np.array([finv(fx), finv(fy), finv(fz)]) * WHITE
    lin = np.linalg.inv(M) @ xyz
    clipped = bool((lin < -1e-4).any() or (lin > 1 + 1e-4).any())
    lin = np.clip(lin, 0.0, 1.0)
    c = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * np.power(lin, 1 / 2.4) - 0.055)
    rgb = np.clip(c * 255.0, 0, 255)
    return (rgb, clipped) if report_clip else rgb


def curve_C(curve, L):
    """从模板曲线 [[L,C],...] 线性插值出 C(L)。"""
    xs = np.array([p[0] for p in curve], float)
    ys = np.array([p[1] for p in curve], float)
    o = np.argsort(xs)
    return float(np.interp(L, xs[o], ys[o]))


def in_gamut(rgb, tol=1.5):
    return bool((rgb > tol).all() and (rgb < 255 - tol).all())


def curve_ab(curve, L):
    """从曲线插值出 (C, a, b)。曲线点支持 [L,C] 或 [L,C,a,b]（后者能表达**色相随明度旋转**）。"""
    if len(curve[0]) >= 4:
        xs = np.array([p[0] for p in curve], float)
        o = np.argsort(xs)
        a = float(np.interp(L, xs[o], np.array([p[2] for p in curve], float)[o]))
        b = float(np.interp(L, xs[o], np.array([p[3] for p in curve], float)[o]))
        return float(np.hypot(a, b)), a, b
    return curve_C(curve, L), None, None


def build_ramp(tpl, steps=4):
    curve = tpl["curve"]
    xs = np.array([p[0] for p in curve], float)
    lo, hi = float(xs.min()), float(xs.max())
    inset = 0.05 * (hi - lo)                      # 两端各内缩 5%，避开离群极值
    # 在材质**值域内等距取阶**（这是像素画的常规做法）；按分位取会让双峰分布的两步挤在亮端
    Ls = np.linspace(lo + inset, hi - inset, steps)
    hue_fallback = np.arctan2(tpl["peak"]["b"], tpl["peak"]["a"])
    out = []
    for L in Ls:
        C, a, b = curve_ab(curve, float(L))
        if a is None:                                   # 旧模板（只有 L,C）=> 退回恒定色相
            a, b = C * np.cos(hue_fallback), C * np.sin(hue_fallback)
        c = C
        clipped = True
        for _ in range(160):                            # 超色域就降彩度（用**裁剪前**的判据）
            rgb, clipped = lab_to_srgb(float(L), a * c / max(C, 1e-6), b * c / max(C, 1e-6), report_clip=True)
            if not clipped:
                break
            c *= 0.98
        out.append({"L": round(float(L), 1), "C_target": round(C, 2), "C_used": round(c, 2),
                    "a_used": round(a * c / max(C, 1e-6), 2), "b_used": round(b * c / max(C, 1e-6), 2),
                    "rgb": [int(round(v)) for v in rgb], "hex": hexc(rgb)})
    return out


def roundtrip(entry):
    """验回：sRGB -> Lab，比对 L 与 C。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import importlib.util
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "palette-forge.py")
    spec = importlib.util.spec_from_file_location("pf_rt", p)
    pf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pf)
    lab = pf.srgb_to_lab(np.array([entry["rgb"]], np.int32))[0]
    return float(lab[0]), float(np.hypot(lab[1], lab[2]))


def swatch(ramps, path, cell=54):
    cols = max(len(v) for v in ramps.values())
    W = 120 + cols * cell
    H = 24 + len(ramps) * (cell + 20)
    im = Image.new("RGB", (W, H), (26, 26, 30))
    dr = ImageDraw.Draw(im)
    f, fb = _font(11), _font(13)
    dr.text((6, 4), "material ramps from ground-truth templates (dark -> light)", fill=(235, 235, 240), font=fb)
    for i, (name, steps) in enumerate(ramps.items()):
        y = 24 + i * (cell + 20)
        dr.text((6, y + cell // 2 - 6), name, fill=(225, 225, 232), font=fb)
        for j, s in enumerate(steps):
            x = 120 + j * cell
            dr.rectangle([x + 1, y + 1, x + cell - 3, y + cell - 3],
                         fill=tuple(s["rgb"]), outline=(80, 80, 88))
            dr.text((x + 2, y + cell), f"L{s['L']:.0f} C{s['C_used']:.0f}", fill=(200, 200, 206), font=f)
    im.save(path)
    return path


def selftest():
    checks = []

    def ck(n, g, w):
        checks.append((n, g, w, g == w))
    tpl = {"peak": {"a": 56.43, "b": 45.35},           # 红伞盖峰值色相（含 a,b 的 4 元曲线点）
           "curve": [[8.1, 41.58, 37.34, 17.93], [21.0, 42.06, 38.68, 16.43], [23.1, 65.75, 52.89, 38.51],
                     [41.7, 71.94, 57.11, 43.69], [42.8, 72.45, 56.43, 45.35], [45.9, 70.71, 52.39, 47.37]]}
    # 先验换算本身：sRGB -> Lab -> sRGB 必须能回来（否则后面全是错的）
    import importlib.util as _iu
    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "palette-forge.py")
    _s = _iu.spec_from_file_location("pf_id", _p)
    _pf = _iu.module_from_spec(_s); _s.loader.exec_module(_pf)
    worst = 0.0
    for probe in ([200, 60, 50], [90, 45, 40], [245, 235, 222], [60, 80, 60], [180, 30, 30]):
        lab = _pf.srgb_to_lab(np.array([probe], np.int32))[0]
        back = lab_to_srgb(float(lab[0]), float(lab[1]), float(lab[2]))
        worst = max(worst, float(np.abs(back - np.array(probe, float)).max()))
    ck("lab_srgb_identity_maxerr_lt_3", worst < 3.0, True)
    print(f"    (sRGB->Lab->sRGB 最大误差 = {worst:.2f})")

    r = build_ramp(tpl, steps=4)
    ck("ramp.count", len(r), 4)
    ck("ramp.L_ascending", all(r[i]["L"] < r[i + 1]["L"] for i in range(3)), True)
    ok_rt = True
    for s in r:
        L2, C2 = roundtrip(s)
        if abs(L2 - s["L"]) > 2.5 or abs(C2 - s["C_used"]) > 4.0:
            ok_rt = False
    ck("ramp.lab_roundtrip", ok_rt, True)
    ck("ramp.hue_is_red", bool(r[-1]["rgb"][0] > r[-1]["rgb"][1] + 40), True)
    ck("ramp.dark_less_saturated", bool(r[0]["C_used"] < r[-1]["C_used"]), True)
    bad = [c for c in checks if not c[3]]
    for n, g, w, ok in checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {n}: got={g} want={w}")
    print(f"SELFTEST={'PASS' if not bad else 'FAIL'} checks={len(checks) - len(bad)}/{len(checks)}")
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", default="")
    ap.add_argument("--names", default="", help="逗号分隔；留空=全部")
    ap.add_argument("--steps", type=int, default=4)
    ap.add_argument("--out-gpl", default="")
    ap.add_argument("--out-json", default="")
    ap.add_argument("--out-swatch", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    with open(a.template, "r", encoding="utf-8") as f:
        T = json.load(f)
    names = [n for n in a.names.split(",") if n] or list(T["templates"])
    ramps, flat, meta = {}, [], []
    for n in names:
        st = build_ramp(T["templates"][n], steps=a.steps)
        ramps[n] = st
        for s in st:
            flat.append(s["rgb"])
            meta.append(f"{n}-L{s['L']:.0f}C{s['C_used']:.0f}")
        print(f"[{n}] " + "  ".join(f"L{s['L']:.0f}/C{s['C_used']:.0f} {s['hex']}" for s in st))
    if a.out_gpl:
        write_gpl(flat, a.out_gpl, name="material-ramps-from-truth", tags=meta)
        print(f"GPL={a.out_gpl} colors={len(flat)}")
    if a.out_json:
        save_json({"source_template": a.template, "steps": a.steps, "ramps": ramps}, a.out_json)
        print(f"JSON={a.out_json}")
    if a.out_swatch:
        print(f"SWATCH={swatch(ramps, a.out_swatch)}")


if __name__ == "__main__":
    main()
