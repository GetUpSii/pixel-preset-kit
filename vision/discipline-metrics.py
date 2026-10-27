# discipline-metrics.py — 像素纪律度量：抖动/孤立像素/棋盘格能量/alpha 二值性/调色板外色
# 用途：把"抖动太过""脏边""色数失控"从主观判断变成可复现数字。
# 依赖：numpy + Pillow（本机无 scipy/torch/onnx）
import os
import sys
import json
import glob
import argparse

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _vlib import load_rgba, read_gpl, save_json, parse_hex, hexc  # noqa: E402


def bayer4_energy(rgb, opaque):
    """棋盘格能量：2x2 单元内对角相等、邻角不等的单元占比（有序抖动的指纹）。"""
    h, w = opaque.shape
    a = rgb[: h - 1, : w - 1]
    b = rgb[: h - 1, 1:w]
    c = rgb[1:h, : w - 1]
    d = rgb[1:h, 1:w]
    m = opaque[: h - 1, : w - 1] & opaque[: h - 1, 1:w] & opaque[1:h, : w - 1] & opaque[1:h, 1:w]
    eq_ad = (a == d).all(-1)
    eq_bc = (b == c).all(-1)
    ne_ab = (a != b).any(-1)
    checker = m & eq_ad & eq_bc & ne_ab
    return int(checker.sum()), int(m.sum())


def isolated4(rgb, opaque):
    """孤立像素（椒盐）：4 邻域全为不透明，且颜色与四个邻居全不同。

    只统计内部像素——轮廓边界像素本来就有透明邻居，不算椒盐。
    """
    h, w = opaque.shape
    inner = opaque.copy()
    differs = np.ones((h, w), dtype=bool)
    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        sh = np.roll(rgb, (dy, dx), axis=(0, 1))
        sm = np.roll(opaque, (dy, dx), axis=(0, 1))
        if dy == -1:
            sh[-1, :] = rgb[-1, :]; sm[-1, :] = opaque[-1, :]
        if dy == 1:
            sh[0, :] = rgb[0, :]; sm[0, :] = opaque[0, :]
        if dx == -1:
            sh[:, -1] = rgb[:, -1]; sm[:, -1] = opaque[:, -1]
        if dx == 1:
            sh[:, 0] = rgb[:, 0]; sm[:, 0] = opaque[:, 0]
        inner &= sm
        differs &= (sh != rgb).any(-1)
    return int((inner & differs).sum())


def alt_energy(rgb, opaque):
    """高频能量：水平/垂直相邻不透明像素对中颜色不同的比例。"""
    out = {}
    for axis, tag in ((1, "h"), (0, "v")):
        a = rgb[:, :-1] if axis == 1 else rgb[:-1, :]
        b = rgb[:, 1:] if axis == 1 else rgb[1:, :]
        m = (opaque[:, :-1] & opaque[:, 1:]) if axis == 1 else (opaque[:-1, :] & opaque[1:, :])
        tot = int(m.sum())
        ne = int((m & (a != b).any(-1)).sum())
        out[tag] = {"pairs": tot, "diff": ne, "ratio": round(ne / tot, 4) if tot else 0.0}
    return out


def analyze(path, pal=None, probes=None):
    rgba, _size = load_rgba(path)
    rgb = rgba[:, :, :3].astype(np.int32)
    alpha = rgba[:, :, 3]
    opaque = alpha > 127
    n_op = int(opaque.sum())
    uniq = np.unique(rgb[opaque].reshape(-1, 3), axis=0) if n_op else np.zeros((0, 3), np.int32)
    checker, cells = bayer4_energy(rgb, opaque)
    iso = isolated4(rgb, opaque)
    rec = {
        "file": os.path.basename(path),
        "size": f"{rgba.shape[1]}x{rgba.shape[0]}",
        "opaque_px": n_op,
        "unique_colors": int(len(uniq)),
        "alpha_non_binary": int(((alpha != 0) & (alpha != 255)).sum()),
        "checker_cells": checker,
        "checker_cell_ratio": round(checker / cells, 4) if cells else 0.0,
        "isolated4_px": iso,
        "isolated4_ratio": round(iso / n_op, 4) if n_op else 0.0,
        "alt": alt_energy(rgb, opaque),
    }
    # 判据自证（F.3）：每条判据声明它要找的特征，这里回报"该特征实际命中多少像素"。
    # 命中 0 ⇒ 调用侧必须判 CHECKER_INVALID：量不到目标的量尺，给出的 0 不是"没问题"。
    if probes:
        rgbop = rgb[opaque]
        hits = {}
        for rgb_t, label in probes:
            if n_op:
                hits[label] = int((rgbop == np.asarray(rgb_t, np.int32)).all(-1).sum())
            else:
                hits[label] = 0
        rec["probes"] = hits
    # 空板不许在旁边偷偷崩：len(pal)==0 时 distance 归约是零长度，会让整条 JSON 通道消失。
    # 这里只如实不报 colors_outside_palette，由调用侧判 CHECKER_INVALID（"空板判不出板外色"）。
    if pal is not None and n_op and len(pal) > 0:
        d = ((uniq[:, None, :].astype(np.int64) - pal[None, :, :].astype(np.int64)) ** 2).sum(-1)
        rec["colors_outside_palette"] = int((d.min(1) > 0).sum())
    return rec


def luma(rgb):
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    return (0.299 * r + 0.587 * g + 0.114 * b)


def light_profile(path):
    """单件上的光照指纹：左右/上下亮度梯度 + 底部接地暗带强度。

    用途：判断一件素材是否把"场景烘焙光"带进了自身（=不可复用），
    以及多件之间光照方向是否一致（不一致＝光影矛盾）。
    """
    rgba, _ = load_rgba(path)
    rgb = rgba[:, :, :3].astype(np.int32)
    opaque = rgba[:, :, 3] > 127
    ys, xs = np.nonzero(opaque)
    if len(xs) == 0:
        return None
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    m = opaque[y0:y1, x0:x1]
    L = luma(rgb[y0:y1, x0:x1])
    h, w = m.shape

    def band_mean(sl):
        sel = m[sl]
        return float(L[sl][sel].mean()) if sel.any() else 0.0

    half = max(1, w // 2)
    qh = max(1, h // 4)
    bh = max(1, int(round(h * 0.12)))
    allmean = float(L[m].mean())
    left, right = band_mean((slice(None), slice(0, half))), band_mean((slice(None), slice(w - half, w)))
    top, bot = band_mean((slice(0, qh), slice(None))), band_mean((slice(h - qh, h), slice(None)))
    foot = band_mean((slice(h - bh, h), slice(None)))
    eps = 1.0
    return {
        "file": os.path.basename(path),
        "bbox": f"{w}x{h}",
        "mean_luma": round(allmean, 1),
        "lr_ratio": round((right + eps) / (left + eps), 3),
        "tb_ratio": round((bot + eps) / (top + eps), 3),
        "foot_ratio": round((foot + eps) / (allmean + eps), 3),
    }


def selftest():
    """量尺自检：合成样例上答案已知，跑不通就不许用它下结论。"""
    checks = []

    def ck(name, got, want):
        checks.append((name, got, want, got == want))

    # 1) 纯平色块：无棋盘格、无椒盐
    flat = np.zeros((8, 8, 3), np.int32) + 100
    op = np.ones((8, 8), bool)
    ck("flat.checker", bayer4_energy(flat, op)[0], 0)
    ck("flat.isolated", isolated4(flat, op), 0)

    # 2) 2 色棋盘格：每个 2x2 单元都是棋盘 => checker=49, 且每个像素都与四邻不同 => isolated=36
    ck_img = np.zeros((8, 8, 3), np.int32)
    ck_img[::2, ::2] = 255
    ck_img[1::2, 1::2] = 255
    ck("checker.checker", bayer4_energy(ck_img, op)[0], 49)
    ck("checker.isolated", isolated4(ck_img, op), 36)

    # 3) 单个椒盐点：内部 1 个像素异色 => isolated=1（边界像素不算）
    salt = np.zeros((8, 8, 3), np.int32) + 100
    salt[4, 4] = 255
    ck("salt.isolated", isolated4(salt, op), 1)
    ck("salt.checker", bayer4_energy(salt, op)[0], 0)

    # 4) 轮廓边界不误报：全图仅最外圈不透明
    ring = np.zeros((8, 8, 3), np.int32) + 100
    rmask = np.zeros((8, 8), bool)
    rmask[0, :] = rmask[-1, :] = rmask[:, 0] = rmask[:, -1] = True
    ck("ring.isolated", isolated4(ring, rmask), 0)

    # 5) 光照指纹：左暗右亮 => lr_ratio > 1；上下同亮 => tb_ratio ≈ 1
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest_tmp", "light_probe.png")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    lg = np.zeros((16, 16, 4), np.uint8)
    lg[:, :, 3] = 255
    lg[:, :8, :3] = 60
    lg[:, 8:, :3] = 200
    from PIL import Image
    Image.fromarray(lg, "RGBA").save(tmp)
    lp = light_profile(tmp)
    ck("light.lr_gt1", lp["lr_ratio"] > 1.0, True)
    ck("light.tb_near1", abs(lp["tb_ratio"] - 1.0) < 0.05, True)
    ck("light.foot_near1", abs(lp["foot_ratio"] - 1.0) < 0.05, True)

    # 6) 探针（F.3）：已知色块的命中数必须逐字对上；不存在的颜色必须是 0；空图必须是 0
    #    —— 三个方向都验，否则"探针"自己就可能恒 0（那正是 farm02 那次假绿）。
    probe = np.zeros((8, 8, 4), np.uint8)
    probe[:, :, 3] = 255
    probe[2, 2, :3] = (0x17, 0x13, 0x10)
    probe[5, 5, :3] = (0x17, 0x13, 0x10)
    tmp2 = os.path.join(os.path.dirname(tmp), "probe_colors.png")
    Image.fromarray(probe, "RGBA").save(tmp2)
    pr = analyze(tmp2, None, [((0x17, 0x13, 0x10), "#171310"), ((1, 2, 3), "#010203")])
    ck("probe.present_hits_exactly", pr["probes"]["#171310"], 2)
    ck("probe.absent_is_zero", pr["probes"]["#010203"], 0)
    blank = np.zeros((4, 4, 4), np.uint8)
    tmp3 = os.path.join(os.path.dirname(tmp), "probe_blank.png")
    Image.fromarray(blank, "RGBA").save(tmp3)
    blank_rec = analyze(tmp3, None, [((0, 0, 0), "#000000")])
    ck("probe.blank_artifact_is_zero", blank_rec["probes"]["#000000"], 0)
    ck("probe.blank_artifact_opaque_zero", blank_rec["opaque_px"], 0)

    bad = [c for c in checks if not c[3]]
    for n, g, w, ok in checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {n}: got={g} want={w}")
    print(f"SELFTEST={'PASS' if not bad else 'FAIL'} checks={len(checks) - len(bad)}/{len(checks)}")
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="", help="glob of PNGs")
    ap.add_argument("--palette", default="")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--light", action="store_true", help="改为输出逐件光照指纹")
    ap.add_argument("--probe-color", action="append", default=[],
                    help="#rrggbb：回报该颜色在产物里的精确命中像素数（可重复）。命中 0 由调用侧判 CHECKER_INVALID")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if not a.glob:
        ap.error("--glob or --selftest required")
    if a.light:
        rows = [r for r in (light_profile(p) for p in sorted(glob.glob(a.glob))) if r]
        hdr = f"{'prop':32s} {'bbox':>9s} {'luma':>6s} {'R/L':>6s} {'B/T':>6s} {'foot':>6s}"
        print(hdr)
        print("-" * len(hdr))
        for r in rows:
            print(f"{r['file'][:32]:32s} {r['bbox']:>9s} {r['mean_luma']:6.1f} "
                  f"{r['lr_ratio']:6.3f} {r['tb_ratio']:6.3f} {r['foot_ratio']:6.3f}")
        rt = [r for r in rows if r["lr_ratio"] > 1.02]
        lf = [r for r in rows if r["lr_ratio"] < 0.98]
        print(f"LIGHT_DIR right-brighter={len(rt)} left-brighter={len(lf)} neutral={len(rows) - len(rt) - len(lf)} of {len(rows)}")
        if a.json_out:
            save_json({"rows": rows}, a.json_out)
            print(f"JSON={a.json_out}")
        return
    pal = read_gpl(a.palette) if a.palette else None
    probes = []
    for c in a.probe_color:
        try:
            probes.append((parse_hex(c), hexc(parse_hex(c))))
        except Exception:
            ap.error(f"--probe-color must be #rrggbb, got {c!r}")
    rows = [analyze(p, pal, probes) for p in sorted(glob.glob(a.glob))]
    hdr = f"{'layer':32s} {'opaque':>7s} {'cols':>5s} {'na-bin':>6s} {'checker%':>9s} {'iso%':>7s} {'altH%':>6s} {'altV%':>6s}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['file'][:32]:32s} {r['opaque_px']:7d} {r['unique_colors']:5d} {r['alpha_non_binary']:6d} "
              f"{r['checker_cell_ratio'] * 100:8.2f}% {r['isolated4_ratio'] * 100:6.2f}% "
              f"{r['alt']['h']['ratio'] * 100:5.1f}% {r['alt']['v']['ratio'] * 100:5.1f}%")
    for label in [lab for _t, lab in probes]:
        per = " ".join(f"{r['file'][:16]}={r.get('probes', {}).get(label, 0)}" for r in rows)
        total = sum(r.get("probes", {}).get(label, 0) for r in rows)
        print(f"PROBE {label} hits={total} [{per}]")
    if a.json_out:
        save_json({"rows": rows, "palette_size": int(len(pal)) if pal is not None else None}, a.json_out)
        print(f"JSON={a.json_out}")


if __name__ == "__main__":
    main()
