# ref-analyze.py — 参考光影指纹：对"参考图"和"我的产物"用同一把尺子量，才谈得上对比改进
# 量什么（都是可复现数字，不是观感）：
#   ① 光源方位角与置信度（light-direction 的一阶矩）
#   ② 用色数 / 不透明像素
#   ③ 明度阶梯数：占 ≥2% 像素的颜色里有多少个**明显不同**的 CIE L* 层（ΔL*≥4 记一层）
#      —— 这一项直接对应 ramp_room：层数少 ⇒ 材质做不出渐变（我的锅就是 3 层）
#   ④ 底部带/顶部带 L* 跨度与接地影强度（光影分布形态）
# 用法：python ref-analyze.py --glob "refs/*.png" [--json-out J]
import os
import sys
import glob
import json
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
LD = _mod("light-direction.py")
RS = _mod("_lightlib.py")   # 光影共享原语（原 relight-split.py 已退役，函数迁至 _lightlib）


def value_steps(rgb, opaque, share=0.02, dmin=4.0):
    """占 ≥share 像素的颜色里，能分出多少层"明显不同的明度"（ΔL* ≥ dmin 记一层）。"""
    flat = rgb[opaque].reshape(-1, 3).astype(np.int32)
    cols, cnt = np.unique(flat, axis=0, return_counts=True)
    keep = cnt >= max(1, int(round(share * cnt.sum())))
    cols, cnt = cols[keep], cnt[keep]
    L = PF.srgb_to_lab(cols)[:, 0]
    order = np.argsort(L)
    levels, last = [], None
    for i in order:
        if last is None or L[i] - last >= dmin:
            levels.append({"hex": "#%02x%02x%02x" % tuple(int(v) for v in cols[i]),
                           "L": round(float(L[i]), 1), "share": round(float(cnt[i]) / cnt.sum(), 3)})
            last = L[i]
    return levels


def analyze(path):
    rgba, _ = load_rgba(path)
    opaque = rgba[:, :, 3] > 127
    rgb = rgba[:, :, :3].astype(np.int32)
    if not opaque.any():
        return None
    lab = PF.srgb_to_lab(rgb[opaque].reshape(-1, 3))[:, 0]
    d = LD.light_direction(path)
    band = RS.band_stats(rgb, opaque)
    levels = value_steps(rgb, opaque)
    return {
        "image": os.path.basename(path),
        "path": path,
        "size": f"{rgba.shape[1]}x{rgba.shape[0]}",
        "opaque_px": int(opaque.sum()),
        "unique_colors": int(len(np.unique(rgb[opaque].reshape(-1, 3), axis=0))),
        "L_mean": round(float(lab.mean()), 1),
        "L_p05": round(float(np.percentile(lab, 5)), 1),
        "L_p95": round(float(np.percentile(lab, 95)), 1),
        "value_steps": len(levels),
        "value_levels": levels,
        "light_azimuth_deg": d["azimuth_deg"] if d else None,
        "light_compass": d["compass"] if d else None,
        "light_confidence": d["confidence"] if d else None,
        "bottom_band_L_span": [band["l_min"], band["l_max"]],
        "bottom_band_colors": band["colors"],
    }


def selftest():
    import numpy as np
    from PIL import Image
    checks = []

    def ck(n, g, w):
        checks.append((n, g, w, g == w))
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest_tmp", "refan")
    os.makedirs(tmp, exist_ok=True)
    # 3 层灰（ΔL* 明显）应报 3 层；同色块重复不增加层数
    S = 48
    yy, xx = np.mgrid[0:S, 0:S]
    m = (yy - 24) ** 2 + (xx - 24) ** 2 <= 20 ** 2
    img = np.zeros((S, S, 4), np.uint8)
    img[:, :, 3] = np.where(m, 255, 0)
    img[m & (xx < 20), :3] = 40
    img[m & (xx >= 20) & (xx < 30), :3] = 120
    img[m & (xx >= 30), :3] = 210
    p = os.path.join(tmp, "three_steps.png")
    Image.fromarray(img, "RGBA").save(p)
    r = analyze(p)
    ck("steps.three", r["value_steps"], 3)
    ck("steps.hex_recorded", len(r["value_levels"]), 3)
    ck("steps.light_dir_present", r["light_azimuth_deg"] is not None, True)
    # 单色 => 1 层
    flat = img.copy(); flat[:, :, :3] = 128
    p2 = os.path.join(tmp, "flat.png")
    Image.fromarray(flat, "RGBA").save(p2)
    ck("steps.flat_one", analyze(p2)["value_steps"], 1)

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
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    targets = ([a.image] if a.image else []) + [p for pat in a.glob.split(";") if pat
                                               for p in sorted(glob.glob(pat, recursive=True))]
    if not targets:
        ap.error("--image or --glob required")
    rows = [r for r in (analyze(t) for t in targets) if r]
    hdr = f"{'image':44s} {'size':>9s} {'cols':>5s} {'steps':>5s} {'L05..95':>13s} {'light':>6s} {'conf':>5s} {'bottomL':>13s}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['image'][:44]:44s} {r['size']:>9s} {r['unique_colors']:5d} {r['value_steps']:5d} "
              f"{r['L_p05']:6.1f}..{r['L_p95']:<6.1f} {str(r['light_compass']):>6s} "
              f"{(r['light_confidence'] or 0):5.2f} {r['bottom_band_L_span'][0]:6.1f}..{r['bottom_band_L_span'][1]:<6.1f}")
    if a.json_out:
        save_json({"rows": rows}, a.json_out)
        print(f"JSON={a.json_out}")


if __name__ == "__main__":
    main()
