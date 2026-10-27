# template-check.py — 用「材质光响应模板」校验一张图（模板不能只读不验，否则是装饰）
# 判据（都来自 spec/material-light-templates.json 的 tolerances）：
#   peak_at（彩度峰值位置）· peak_C（峰值彩度）· lit/dark_over_peak（两侧衰减比例）· shadow_shift_ab（阴影色偏）
# 用途：画出 32x32 像素蘑菇后，先拿真值模板卡一遍，再交给用户看。
# 用法：python template-check.py --template spec/material-light-templates.json --name cap_red --image X.png
#       python template-check.py --template ... --all --image X.png
import os
import sys
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


LA = _mod("light-audit.py")


def measure(path, sel, bins=10):
    hue = sel.get("lab_hue_deg")
    m = LA.audit_bins(path, bins=bins, sel_hue=tuple(hue) if hue else None,
                      sel_cmin=float(sel.get("c_min", 0.0)))
    if m is None:
        return None
    binsr = m["bins"]
    peak_i = m["C_peak_bin"]
    peak_C = binsr[peak_i]["C_mean"]
    return {
        "peak_at": m["peak_at"], "peak_C": peak_C, "peak_L": binsr[peak_i]["L_lo"],
        "lit_over_peak": round(binsr[-1]["C_mean"] / peak_C, 3) if peak_C else None,
        "dark_over_peak": round(binsr[0]["C_mean"] / peak_C, 3) if peak_C else None,
        "dA": m["dA_dark_minus_lit"], "dB": m["dB_dark_minus_lit"],
        "n_bins": len(binsr), "sel_px_share_ok": all(b["share"] >= 0.0 for b in binsr),
        "curve": [[b["L_lo"], b["C_mean"]] for b in binsr],
    }


def check(meas, tpl, tol):
    """逐条判据比较。返回 (criteria, overall_pass)。"""
    crit = []

    def add(name, got, want, ok, unit=""):
        crit.append({"criterion": name, "got": got, "want": want, "ok": bool(ok), "unit": unit})

    pk = tpl["peak"]
    # peak_at 只在"峰值有意义"时判：若峰值与亮端几乎齐平（<5% 差），位置就不是判别量
    # （实测真值红伞盖的峰极浅：72.45 vs 亮端 70.71，只差 2.4%；对浅峰判位置会把正确结果判错）
    peak_shallow = abs(meas["peak_C"] - (meas["lit_over_peak"] or 0) * meas["peak_C"]) \
        <= 0.05 * meas["peak_C"]
    if peak_shallow:
        crit.append({"criterion": "peak_at", "got": meas["peak_at"], "want": pk["peak_at"],
                     "ok": True, "unit": "informational(浅峰，不判别)"})
    else:
        add("peak_at", meas["peak_at"], pk["peak_at"],
            abs(meas["peak_at"] - pk["peak_at"]) <= tol["peak_at"])
    add("peak_C", meas["peak_C"], pk["C"],
        abs(meas["peak_C"] - pk["C"]) <= tol["peak_C_rel"] * pk["C"])
    for k in ("lit_over_peak", "dark_over_peak"):
        w = tpl["retention"][k]
        add(k, meas[k], w, abs((meas[k] or 0) - w) <= tol["retention_abs"])
    for k, tag in (("dA", "dA"), ("dB", "dB")):
        w = tpl["shadow_shift_ab"][tag]
        add(f"shadow_shift_{tag}", meas[k], w,
            abs(meas[k] - w) <= tol["shadow_shift_ab_abs"])
    add("bins_available", meas["n_bins"], tol["min_bins"], meas["n_bins"] >= tol["min_bins"])
    return crit, all(c["ok"] for c in crit)


def selftest():
    import json
    from PIL import Image
    checks = []

    def ck(n, g, w):
        checks.append((n, g, w, g == w))
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest_tmp", "tpl")
    os.makedirs(tmp, exist_ok=True)

    def synth(name, cols, S=100):
        img = np.zeros((S, len(cols) * 20, 4), np.uint8)
        img[:, :, 3] = 255
        for i, c in enumerate(cols):
            img[:, i * 20:(i + 1) * 20, :3] = c
        p = os.path.join(tmp, name)
        Image.fromarray(img, "RGBA").save(p)
        return p

    # 好样例：彩度在中间带达到峰值（模拟"交界带最饱和"）
    good = synth("good.png", [(120, 40, 45), (160, 45, 45), (210, 50, 45), (230, 105, 100), (240, 170, 165)])
    # 坏样例：**彩度随明度单调递增**（峰值跑到最亮端）—— 违反"峰值在交界带"
    # ⚠ 负对照必须改动"测量真正依赖的量"。上一版我只是把色带在空间上倒序，而分箱按 L* 不按位置
    #   ⇒ 曲线完全相同 ⇒ 假负对照（同样的错我这轮已犯第二次：拿"以为"当断言）。
    bad = synth("bad.png", [(120, 100, 95), (150, 115, 100), (190, 130, 100), (220, 140, 90), (245, 150, 70)])

    sel = {"lab_hue_deg": [0, 60], "c_min": 5}
    m_good = measure(good, sel, bins=5)
    ck("measure.good", m_good is not None, True)
    # 用"好样例自身的测量值"当模板 => 它必须 PASS（仅作量具自洽），
    # 再拿同一个模板去卡坏样例 => 必须 FAIL（这才是真正有判别力的断言）
    tpl = {"peak": {"C": m_good["peak_C"], "peak_at": m_good["peak_at"]},
           "retention": {"lit_over_peak": m_good["lit_over_peak"],
                         "dark_over_peak": m_good["dark_over_peak"]},
           "shadow_shift_ab": {"dA": m_good["dA"], "dB": m_good["dB"]}}
    tol = {"peak_at": 0.1, "peak_C_rel": 0.1, "retention_abs": 0.1,
           "shadow_shift_ab_abs": 2.0, "min_bins": 3, "min_bin_share": 0.05}
    _, ok_good = check(m_good, tpl, tol)
    ck("check.good_passes", ok_good, True)
    m_bad = measure(bad, sel, bins=5)
    _, ok_bad = check(m_bad, tpl, tol)
    ck("check.bad_fails", ok_bad, False)

    bad_l = [c for c in checks if not c[3]]
    for n, g, w, ok in checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {n}: got={g} want={w}")
    print(f"SELFTEST={'PASS' if not bad_l else 'FAIL'} checks={len(checks) - len(bad_l)}/{len(checks)}")
    return 0 if not bad_l else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", default="")
    ap.add_argument("--name", default="")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--image", default="")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if not a.image or not a.template:
        ap.error("--template and --image required")
    import json
    with open(a.template, "r", encoding="utf-8") as f:
        T = json.load(f)
    tol = T["tolerances"]
    names = list(T["templates"]) if a.all else [a.name or list(T["templates"])[0]]
    results = []
    for nm in names:
        tpl = T["templates"][nm]
        meas = measure(a.image, tpl["selector"])
        if meas is None:
            print(f"{nm}: NO_DATA（材质选区内像素不足）")
            results.append({"template": nm, "error": "no_data"})
            continue
        crit, ok = check(meas, tpl, tol)
        results.append({"template": nm, "image": a.image, "pass": ok,
                        "peak_at": meas["peak_at"], "peak_C": meas["peak_C"],
                        "lit_over_peak": meas["lit_over_peak"], "dark_over_peak": meas["dark_over_peak"],
                        "dA": meas["dA"], "dB": meas["dB"], "criteria": crit})
        print(f"[{nm}] {'PASS' if ok else 'FAIL'}  peak_at={meas['peak_at']:.2f} peak_C={meas['peak_C']:.1f} "
              f"lit/peak={meas['lit_over_peak']} dark/peak={meas['dark_over_peak']} "
              f"d(ab)=({meas['dA']},{meas['dB']})")
        for c in crit:
            print(f"    {'ok  ' if c['ok'] else 'FAIL'} {c['criterion']:18s} got={c['got']} want={c['want']}")
    if a.json_out:
        save_json({"image": a.image, "results": results}, a.json_out)
        print(f"JSON={a.json_out}")


if __name__ == "__main__":
    main()
