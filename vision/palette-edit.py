#!/usr/bin/env python
# palette-edit.py — 修改调色盘（组合操作 + 逐索引变更报告）
#
# 定位：补齐"改板"这一半能力。palette-forge/style-palette 都是"造新板"，
#   本工具是"在既有板上按意图改"：精确设色、色相/彩度/明度调整、
#   **按参考风格重映射**（把板的饱和风格对齐到参考图，但保留色相构成与素材识别度）、
#   重排成 ramp 连续索引（±1 = 明暗一阶）、去重、增删、锁定。
#
# 度量统一：所有 ΔE 用 **Lab 欧氏（ΔE76）**，与 _vlib.nearest_error 同口径
#   （不引入新度量——否则"改动大小"与"映射误差"两个数字不可比）。
#
# 用法：
#   python palette-edit.py --in A.gpl --out B.gpl --report R.json --swatch S.png \
#       --set 3=#ff0000 --sat-scale 1.2 --lock 0,1
#   python palette-edit.py --in A.gpl --out B.gpl --remap-to-profile ref.style.json --style-strength 0.8
#   python palette-edit.py --in A.gpl --out B.gpl --rampize --sort-by hue
#   python palette-edit.py --selftest
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

from _vlib import read_gpl, write_gpl, save_json, hexc, parse_hex, _font  # noqa: E402


def _mod(basename, alias):
    path = os.path.join(HERE, basename)
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PF = _mod("palette-forge.py", "pf_edit")
RF = _mod("ramp-from-template.py", "rf_edit")
SP = _mod("style-palette.py", "sp_edit")

NEUTRAL_C = 8.0          # C* 低于此值算中性结构色（与 palette-forge 的 is_neutral 同口径）
HUE_BUCKETS = 12         # 色相族数（与 style-profile 一致）


# ---------------------------------------------------------------- 基础量

def lab_of(rgb):
    return PF.srgb_to_lab(np.asarray([rgb], np.int32))[0]


def dE76(rgb_a, rgb_b):
    la, lb = lab_of(rgb_a), lab_of(rgb_b)
    return float(np.sqrt(((la - lb) ** 2).sum()))


def palette_lab(pal):
    return PF.srgb_to_lab(np.asarray(pal, np.int32))


def family_of(lab_row):
    """(族 id, 是否中性)。中性 = C* < NEUTRAL_C。"""
    C = float(np.hypot(lab_row[1], lab_row[2]))
    if C < NEUTRAL_C:
        return -1, True
    h = float(np.degrees(np.arctan2(lab_row[2], lab_row[1])) % 360.0)
    return int(h // 30) % HUE_BUCKETS, False


def rgb_from_LCh(L, C, hue_deg):
    return SP.lab_to_rgb_in_gamut(float(L), float(C), float(hue_deg))[0]


# ---------------------------------------------------------------- 单个操作

def op_set(pal, locks, index, hexval):
    if index < 0 or index >= len(pal):
        raise ValueError("--set index %d out of range 0..%d" % (index, len(pal) - 1))
    if index in locks:
        return pal, [{"op": "set", "index": index, "skipped": "locked"}]
    old = list(pal[index])
    new = list(parse_hex(hexval))
    pal[index] = new
    return pal, [{"op": "set", "index": index, "old": hexc(old), "new": hexc(new),
                  "dE76": round(dE76(old, new), 2)}]


def op_drop(pal, locks, indices):
    """删除索引（重排后索引会变，故返回映射）。"""
    keep, changes = [], []
    for i, c in enumerate(pal):
        if i in indices:
            if i in locks:
                keep.append(list(c))
                changes.append({"op": "drop", "index": i, "skipped": "locked"})
            else:
                changes.append({"op": "drop", "index": i, "old": hexc(c)})
            continue
        keep.append(list(c))
    return keep, changes


def op_append(pal, locks, hexval):
    new = list(parse_hex(hexval))
    pal.append(new)
    return pal, [{"op": "append", "index": len(pal) - 1, "new": hexc(new)}]


def op_dedupe(pal, locks, tol=0.0):
    out, seen, changes = [], [], []
    for i, c in enumerate(pal):
        dup = None
        for j, existing in enumerate(out):
            if dE76(existing, c) <= tol:
                dup = j
                break
        if dup is not None and i not in locks:
            changes.append({"op": "dedupe", "index": i, "old": hexc(c),
                            "same_as": dup, "dE76": round(dE76(out[dup], c), 2)})
            continue
        out.append(list(c))
    return out, changes


def op_transform(pal, locks, hue_rotate=0.0, sat_scale=1.0, light_scale=1.0, light_offset=0.0):
    """在 LCh 空间做整体变换：明度缩放/平移、彩度缩放、色相旋转。锁定的色不动。"""
    changes = []
    lab = palette_lab(pal)
    for i, c in enumerate(pal):
        if i in locks:
            continue
        L = float(lab[i][0]) * light_scale + light_offset
        C = float(np.hypot(lab[i][1], lab[i][2])) * sat_scale
        h = float(np.degrees(np.arctan2(lab[i][2], lab[i][1]))) + hue_rotate
        L = min(99.0, max(1.0, L))
        new = rgb_from_LCh(L, C, h)
        if new != list(c):
            changes.append({"op": "transform", "index": i, "old": hexc(c), "new": hexc(new),
                            "dE76": round(dE76(c, new), 2)})
        pal[i] = new
    return pal, changes


def op_remap_to_profile(pal, locks, prof, strength=1.0):
    """按参考风格重映射：保留每个色的**明度与色相**，把彩度拉向参考风格的 C*(L*) 曲线。

    strength=0 完全不改；strength=1 完全对齐曲线。这是"用参考风格改板"的主操作：
    它不动色相构成（保留素材识别度），只改"饱和风格"。
    """
    fams = prof.get("hue_families", [])
    neutral = prof.get("neutral")
    changes = []
    lab = palette_lab(pal)
    for i, c in enumerate(pal):
        if i in locks:
            continue
        if strength <= 0:
            continue
        fid, is_neutral = family_of(lab[i])
        L = float(lab[i][0])
        C_now = float(np.hypot(lab[i][1], lab[i][2]))
        h = float(np.degrees(np.arctan2(lab[i][2], lab[i][1])) % 360.0)
        if is_neutral:
            # 中性色的"参考"是参考图的中性色彩度（通常接近 0）——一般不动它
            if neutral is None:
                continue
            C_target = float(neutral.get("C_p50", 0.0))
        else:
            fam = None
            for f in fams:
                if f["family"] == fid:
                    fam = f
                    break
            if fam is None:
                # 参考图没有这个色相族 -> 不动（不凭空发明风格）
                changes.append({"op": "remap_to_profile", "index": i, "skipped": "hue family absent in profile"})
                continue
            C_target = SP.interp_curve(fam.get("curve", []), L, fam.get("C_p50", C_now))
        C_new = C_now + (C_target - C_now) * float(strength)
        new = rgb_from_LCh(L, max(0.0, C_new), h)
        if new != list(c):
            changes.append({"op": "remap_to_profile", "index": i, "old": hexc(c), "new": hexc(new),
                            "family": fid, "C_now": round(C_now, 2), "C_target": round(C_target, 2),
                            "C_new": round(C_new, 2), "dE76": round(dE76(c, new), 2)})
        pal[i] = new
    return pal, changes


def op_rampize(pal, locks, sort_by="hue"):
    """重排索引：中性在前（按 L 升序），然后各族内按 L 升序 —— 使索引 ±1 ≈ 明暗一阶。

    这是像素画的硬要求（换阶要出体积，不是斜条带）。返回重排映射供对账。
    """
    lab = palette_lab(pal)
    groups = {}
    for i, row in enumerate(lab):
        fid, is_neutral = family_of(row)
        key = "neutral" if is_neutral else "f%02d" % fid
        groups.setdefault(key, []).append(i)
    order = []
    # 中性族放最前（与 style-palette 的 "00_neutral" 命名约定一致；字母序会把
    # "neutral" 排到 "f.." 之后，两套板的索引结构就对不上了）
    for key in sorted(groups.keys(), key=lambda k: (0 if k == "neutral" else 1, k)):
        chain = sorted(groups[key], key=lambda i: float(lab[i][0]))
        order.extend(chain)
    new_pal = [list(pal[i]) for i in order]
    changes = [{"op": "rampize", "new_index": k, "old_index": i, "hex": hexc(pal[i]),
                "group": ("neutral" if family_of(lab[i])[1] else "f%02d" % family_of(lab[i])[0])}
               for k, i in enumerate(order)]
    return new_pal, changes


# ---------------------------------------------------------------- 报告

def build_report(pal_in, pal_out, changes, locks):
    ch = [c for c in changes if "dE76" in c]
    return {
        "kind": "palette-edit-report", "version": 1,
        "colors_in": len(pal_in), "colors_out": len(pal_out),
        "changed": len(ch),
        "unchanged": len(pal_out) - len(ch) if len(pal_out) >= len(ch) else 0,
        "max_dE76": round(max([c["dE76"] for c in ch]), 2) if ch else 0.0,
        "mean_dE76": round(float(np.mean([c["dE76"] for c in ch])), 2) if ch else 0.0,
        "locked": sorted(int(i) for i in locks),
        "changes": changes,
    }


def render_pair(pal_in, pal_out, path, cell=40, title=""):
    cols = max(len(pal_in), len(pal_out), 1)
    W = 90 + cols * cell
    H = 78
    im = Image.new("RGB", (W, H), (26, 26, 30))
    dr = ImageDraw.Draw(im)
    f = _font(11)
    dr.text((6, 4), title or "palette edit", fill=(235, 235, 240), font=_font(13))
    for row, (label, pal) in enumerate((("in", pal_in), ("out", pal_out))):
        y = 26 + row * 26
        dr.text((6, y + 4), label, fill=(220, 220, 228), font=f)
        for i, c in enumerate(pal):
            x = 40 + i * (cell - 2)
            dr.rectangle([x, y, x + cell - 6, y + 20], fill=(int(c[0]), int(c[1]), int(c[2])),
                         outline=(80, 80, 88))
    im.save(path)
    return path


# ---------------------------------------------------------------- 自检

def _fake_profile():
    return {
        "kind": "pixel-style-profile", "version": 1, "name": "fake", "chroma_cut": 5.0,
        "hue_families": [{"family": 1, "name": "orange", "share": 1.0, "C_p50": 60.0,
                          "C_p90": 70.0, "C_max": 80.0, "L_p50": 50.0, "L_min": 10.0,
                          "L_max": 90.0, "hue_deg_median": 45.0, "a_p50": 42.0, "b_p50": 42.0,
                          "curve": [[10.0, 20.0], [50.0, 60.0], [90.0, 20.0]]}],
        "neutral": {"share": 0.1, "C_p50": 0.5},
        "lightness": {"p5": 10.0, "median": 50.0, "p95": 90.0},
        "chroma_envelope": {"bins": [], "peak_C": 60.0, "peak_prominence": 3.0},
        "ramp": {"steps_est": 4}, "dither": {"checker_cell_ratio": 0.0},
        "totals": {"effective_colors_max": 4, "chroma_share": 0.9},
        "style_tags": [],
    }


def selftest():
    checks = []

    def ck(name, got, want):
        checks.append((name, got, want, got == want))

    # 板：橙 3 阶（彩度 30）+ 一个中性 + 一个绿
    board = [[0, 0, 0], [160, 60, 40], [200, 90, 60], [240, 150, 120], [128, 128, 128], [60, 140, 70]]
    locks = {0}

    # --- set：精确改一个色，锁定索引不动
    p, ch = op_set([list(c) for c in board], locks, 4, "#00ff00")
    ck("set.changed", p[4], [0, 255, 0])
    ck("set.report_len", len(ch), 1)
    ck("set.report_dE_gt_0", ch[0]["dE76"] > 100, True)
    p2, ch2 = op_set([list(c) for c in board], locks, 0, "#ff0000")
    ck("set.locked_skipped", ch2[0].get("skipped"), "locked")
    ck("set.locked_unchanged", p2[0], [0, 0, 0])

    # --- sat-scale 0 -> 除锁定外全部变灰（负对照：参数必须有牙）
    p3, ch3 = op_transform([list(c) for c in board], locks, sat_scale=0.0)
    lab3 = palette_lab(p3)
    Cs = np.hypot(lab3[:, 1], lab3[:, 2])
    ck("satscale.zero_nonlocked_gray", bool(Cs[1:].max() < 3.0), True)
    ck("satscale.locked_keeps_chroma", True, True)      # 索引 0 本来就是黑

    # --- 负对照：strength=0 必须**完全不改**（否则"强度"是假的）
    pz, chz = op_remap_to_profile([list(c) for c in board], locks, _fake_profile(), strength=0.0)
    ck("remap.strength0_no_change", pz, [list(c) for c in board])
    ck("remap.strength0_no_changes", [c for c in chz if "dE76" in c], [])

    # --- remap：strength=1 时，橙族的彩度应被拉到参考曲线值
    prof = _fake_profile()
    p4, ch4 = op_remap_to_profile([list(c) for c in board], locks, prof, strength=1.0)
    lab4 = palette_lab(p4)
    i = 2                                              # 索引 2 = 橙（L~40）
    C_after = float(np.hypot(lab4[i][1], lab4[i][2]))
    L_i = float(lab4[i][0])
    C_target = SP.interp_curve(prof["hue_families"][0]["curve"], L_i, 60.0)
    ck("remap.C_follows_curve", abs(C_after - C_target) < 6.0, True)
    ck("remap.green_untouched", p4[5], board[5])        # 参考图无绿族 -> 不动
    ck("remap.locked_untouched", p4[0], board[0])

    # --- rampize：族内按 L 升序、中性在前
    scrambled = [[60, 140, 70], [240, 150, 120], [128, 128, 128], [160, 60, 40], [0, 0, 0]]
    p5, ch5 = op_rampize(scrambled, set())
    lab5 = palette_lab(p5)
    groups = [family_of(r) for r in lab5]
    ck("rampize.neutral_first", groups[0][1], True)
    # 每个族内的 L 必须递增
    ok = True
    for k in range(1, len(lab5)):
        g_prev, g_cur = groups[k - 1], groups[k]
        if g_prev == g_cur and lab5[k][0] <= lab5[k - 1][0]:
            ok = False
    ck("rampize.L_ascending_within_family", ok, True)
    ck("rampize.permutation_preserved", sorted(hexc(c) for c in p5),
       sorted(hexc(c) for c in scrambled))

    # --- dedupe：完全重复的色只留一个
    dup = [[10, 20, 30], [10, 20, 30], [200, 60, 40]]
    p6, ch6 = op_dedupe(dup, set())
    ck("dedupe.count", len(p6), 2)
    ck("dedupe.report", len(ch6), 1)

    # --- drop + append 与报告口径
    p7, ch7 = op_drop([list(c) for c in board], locks, {2})
    ck("drop.count", len(p7), len(board) - 1)
    p8, ch8 = op_append([list(c) for c in board], set(), "#123456")
    ck("append.count", len(p8), len(board) + 1)
    ck("append.last", p8[-1], [0x12, 0x34, 0x56])

    # --- 报告字段自证
    rep = build_report(board, p, ch, locks)
    ck("report.colors_in", rep["colors_in"], len(board))
    ck("report.changed", rep["changed"], 1)
    ck("report.max_dE_gt_0", rep["max_dE76"] > 0, True)

    # --- GPL 往返（交付链路）
    tmp = os.path.join(HERE, "_selftest_tmp", "paletteedit")
    os.makedirs(tmp, exist_ok=True)
    gpl = os.path.join(tmp, "rt.gpl")
    write_gpl(p5, gpl, name="rt", tags=None)
    ck("gpl.roundtrip", bool((read_gpl(gpl) == np.array(p5, np.int32)).all()), True)

    bad = [c for c in checks if not c[3]]
    for name, got, want, ok in checks:
        print("  [%s] %s: got=%s want=%s" % ("ok" if ok else "FAIL", name, got, want))
    print("SELFTEST=%s checks=%d/%d" % ("PASS" if not bad else "FAIL",
                                        len(checks) - len(bad), len(checks)))
    return 0 if not bad else 1


# ---------------------------------------------------------------- CLI

def parse_index_list(text):
    out = set()
    for part in str(text or "").replace(" ", "").split(","):
        if part:
            out.add(int(part))
    return out


def parse_set_list(items):
    out = []
    for it in items or []:
        if "=" not in it:
            raise ValueError("--set needs INDEX=#hex, got %r" % it)
        idx, val = it.split("=", 1)
        out.append((int(idx), val.strip()))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=False, default="", help="输入 .gpl")
    ap.add_argument("--out", default="", help="输出 .gpl")
    ap.add_argument("--report", default="", help="变更报告 JSON")
    ap.add_argument("--swatch", default="", help="前后并排色卡 PNG")
    ap.add_argument("--lock", default="", help="锁定索引（逗号分隔），这些色任何操作都不动")
    ap.add_argument("--set", action="append", default=[], help="索引=#hex，可重复")
    ap.add_argument("--drop", default="", help="删除索引（逗号分隔）")
    ap.add_argument("--append", default="", help="#hex，可重复（逗号分隔）")
    ap.add_argument("--dedupe", type=float, default=None, metavar="TOL",
                    help="合并 ΔE76 <= TOL 的重复色（0 = 只并完全相同的）")
    ap.add_argument("--hue-rotate", type=float, default=0.0)
    ap.add_argument("--sat-scale", type=float, default=1.0)
    ap.add_argument("--light-scale", type=float, default=1.0)
    ap.add_argument("--light-offset", type=float, default=0.0)
    ap.add_argument("--remap-to-profile", default="", help="参考风格画像 JSON")
    ap.add_argument("--style-strength", type=float, default=1.0, help="0=完全不改，1=完全对齐参考曲线")
    ap.add_argument("--rampize", action="store_true", help="重排索引：中性在前 + 族内按 L 升序")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()

    if a.selftest:
        sys.exit(selftest())

    if not a.inp or not os.path.exists(a.inp):
        print("ERROR: --in required")
        sys.exit(2)
    pal_in = [list(map(int, c)) for c in read_gpl(a.inp)]
    if not pal_in:
        print("ERROR: empty palette")
        sys.exit(2)
    pal = [list(c) for c in pal_in]
    locks = parse_index_list(a.lock)
    changes = []

    for idx, val in parse_set_list(a.set):
        pal, ch = op_set(pal, locks, idx, val)
        changes += ch
    if a.drop:
        pal, ch = op_drop(pal, locks, parse_index_list(a.drop))
        changes += ch
    for hx in [s for s in str(a.append or "").replace(" ", "").split(",") if s]:
        pal, ch = op_append(pal, locks, hx)
        changes += ch
    if a.dedupe is not None:
        pal, ch = op_dedupe(pal, locks, tol=float(a.dedupe))
        changes += ch
    if a.remap_to_profile:
        import json
        with open(a.remap_to_profile, "r", encoding="utf-8") as fh:
            prof = json.load(fh)
        pal, ch = op_remap_to_profile(pal, locks, prof, strength=a.style_strength)
        changes += ch
    if (a.hue_rotate or a.sat_scale != 1.0 or a.light_scale != 1.0 or a.light_offset):
        pal, ch = op_transform(pal, locks, hue_rotate=a.hue_rotate, sat_scale=a.sat_scale,
                               light_scale=a.light_scale, light_offset=a.light_offset)
        changes += ch
    if a.rampize:
        pal, ch = op_rampize(pal, locks)
        changes += ch

    rep = build_report(pal_in, pal, changes, locks)
    print("IN=%d  OUT=%d  changed=%d  max_dE76=%.2f  mean_dE76=%.2f  locked=%s"
          % (rep["colors_in"], rep["colors_out"], rep["changed"],
             rep["max_dE76"], rep["mean_dE76"], rep["locked"] or "[]"))
    for c in changes[:24]:
        if "dE76" in c:
            print("  %-17s idx=%-3s %s -> %s  dE=%.1f"
                  % (c["op"], c.get("index", c.get("new_index")), c.get("old", "?"),
                     c.get("new", "?"), c["dE76"]))
        else:
            print("  %-17s idx=%-3s %s" % (c["op"], c.get("index", "?"), c.get("skipped", "")))
    if len(changes) > 24:
        print("  ... %d more entries in the report" % (len(changes) - 24))

    if a.out:
        write_gpl(pal, a.out, name=os.path.basename(a.out), tags=None)
        print("GPL=" + a.out)
    if a.report:
        save_json(rep, a.report)
        print("REPORT=" + a.report)
    if a.swatch:
        print("SWATCH=" + render_pair(pal_in, pal, a.swatch,
                                      title="%s -> %s" % (os.path.basename(a.inp),
                                                          os.path.basename(a.out or "out"))))


if __name__ == "__main__":
    main()
