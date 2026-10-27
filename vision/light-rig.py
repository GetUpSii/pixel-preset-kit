# light-rig.py — 光源模拟器：把光源变成可调参数，并直接产出「光影分线」+ 分带层
# 为什么需要它：光影分线的位置完全由光源决定。方向/仰角/数量一变，分线就变——
#   所以要把光源抽成参数（含多光源 key/fill/rim），并排预览，而不是每换一个光就重画一遍。
# 模型：法线场（平滑距离场求法线）· 多光源加权 max(0, n·L) + 环境项 => 明暗场
#       分线 = 各阈值 mask 的 1px 边界（mask & ~erode4(mask)）
# 产出：_shade.png（明暗场）/ _term.png（固有色 + 分线）/ _bands.png（分带上色）/ rigsheet（并排对照）
# 用法：python light-rig.py --image A.png --preset nw_key;measured;key_fill;top;candle --out-dir D
#       python light-rig.py --image A.png --rig "key:340,45,1.0;fill:160,35,0.35" --out-dir D
import os
import sys
import glob
import math
import argparse
import importlib.util

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _vlib import load_rgba, save_json, label_mask, _font, hexc  # noqa: E402


def _mod(name):
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").replace(".py", ""), p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PF = _mod("palette-forge.py")
RS = _mod("_lightlib.py")   # 光影共享原语（原 relight-split.py 已退役，函数迁至 _lightlib）

AMBIENT = 0.22          # 环境光底；"未被任何方向光照到"的判据 = s <= AMBIENT + 0.02

# 预设光源组：(名称, 方位角°, 仰角°, 权重)。方位角罗盘制 0=N(上) 90=E(右) 180=S(下) 270=W(左)
PRESETS = {
    "nw_key":     [("key", 315, 45, 1.0)],
    "measured":   [("key", 340, 45, 1.0)],                    # 本场景 12 件实测中位光向
    "key_fill":   [("key", 340, 45, 0.85), ("fill", 160, 35, 0.35)],
    "top":        [("key", 0, 80, 1.0)],
    "backlight":  [("key", 20, 30, 0.9), ("rim", 200, 25, 0.5)],
    "candle":     [("key", 250, -15, 0.9)],                   # 负仰角 = 来自下方（地上烛光）
    "two_side":   [("L", 300, 35, 0.7), ("R", 60, 35, 0.7)],
}


def erode4(m):
    e = m.copy()
    e[1:, :] &= m[:-1, :]
    e[:-1, :] &= m[1:, :]
    e[:, 1:] &= m[:, :-1]
    e[:, :-1] &= m[:, 1:]
    return e


def normal_field(opaque, blur_radius=3, nz=0.6):
    """平滑距离场 -> 法线场。整数距离变换直接求梯度会得到噪声法线（实测过），必须先平滑。"""
    dt = np.zeros(opaque.shape, np.float64)
    cur = opaque.copy()
    for d in range(1, 400):
        cur = erode4(cur)
        if not cur.any():
            break
        dt[cur] = d
    dts = RS.box_blur(dt, blur_radius, passes=2)
    gy, gx = np.gradient(dts)
    nn = np.sqrt(gx * gx + gy * gy + nz * nz) + 1e-9
    return -gx / nn, -gy / nn, nz / nn, dt


def shade_field(nx, ny, nz_, rig, ambient=AMBIENT):
    s = np.full(nx.shape, ambient, np.float64)
    for _name, az, elev, w in rig:
        a, e = math.radians(az), math.radians(elev)
        lx, ly, lz = math.cos(e) * math.sin(a), -math.cos(e) * math.cos(a), math.sin(e)
        dot = nx * lx + ny * ly + nz_ * lz
        s += w * np.clip(dot, 0.0, 1.0)
    return s


def flatten_base(rgb, opaque, cols, cnt):
    """固有色层：每个材质族压平到用量中位阶（光影因此完全不属于物品像素）。"""
    fams, LabU = RS.material_families(cols, c_thr=8.0)
    pix = np.full(rgb.shape[:2], -1, np.int64)
    for i, c in enumerate(cols):
        pix[np.all(rgb == c[None, None, :], axis=-1) & opaque] = i
    out = rgb.copy()
    ladder = []
    for m in fams:
        wj = cnt[m].astype(np.float64)
        cum = np.cumsum(wj) / wj.sum()
        if len(m) >= 3:
            lo = int(min(len(m) - 3, max(0, np.searchsorted(cum, 0.10))))
            hi = int(max(lo + 2, min(len(m) - 1, np.searchsorted(cum, 0.85))))
            mid = int(min(hi - 1, max(lo + 1, np.searchsorted(cum, 0.50))))
        else:
            lo, hi, mid = 0, len(m) - 1, 0
        ladder.append((m, lo, mid, hi))
        sel = opaque & np.isin(pix, m)
        if sel.any():
            out[sel] = cols[m[mid]].astype(np.uint8)
    return out, ladder, pix, LabU, fams


def render_rig(rgb_base, opaque, s, ladder, pix_base, cols, bands=3,
               shadow_fracs=(0.45, 0.28, 0.14), hl_frac=0.80, main_only=True, min_comp=12):
    """按明暗场分带 + 画分线。返回 (banded_rgb, term_line_mask, shadow_mask, hl_mask, thr)

    分线默认只画**主分界线**（受光/背光交界）：画全部阈值会得到一串闭合环，
    细结构（蒸汽/提手）尤其脏，读起来是噪声而不是分界线。小连通块按 min_comp 滤掉。
    """
    sv = s[opaque]
    if sv.size == 0 or sv.max() <= sv.min():
        z = np.zeros((*opaque.shape, 4), np.uint8)
        return {"banded": rgb_base.copy(), "term": np.zeros_like(opaque),
                "shadow": np.zeros_like(opaque), "hl": np.zeros_like(opaque),
                "base_rgba": np.dstack([rgb_base, np.where(opaque, 255, 0).astype(np.uint8)]),
                "shadow_rgba": z.copy(), "hl_rgba": z.copy(), "term_rgba": z.copy(),
                "thr": {"shadow": [], "highlight": None, "terminator_px": 0, "main_only": main_only}}
    lo_v, hi_v = float(sv.min()), float(sv.max())
    sn = (s - lo_v) / (hi_v - lo_v)
    q = lambda p: float(np.quantile(sn[opaque], p))            # noqa: E731
    thr_sh = [q(f) for f in shadow_fracs[:bands]]
    thr_hl = q(hl_frac)

    banded = rgb_base.copy()
    shadow = np.zeros_like(opaque)
    hl = np.zeros_like(opaque)
    shadow_rgba = np.zeros((*opaque.shape, 4), np.uint8)
    hl_rgba = np.zeros((*opaque.shape, 4), np.uint8)
    for m, lo_i, mid_i, hi_i in ladder:
        sel = opaque & np.isin(pix_base, m)
        if not sel.any():
            continue
        depths = [q(f) for f in (0.30, 0.18, 0.08)][:bands]
        for b, t in enumerate(sorted(depths, reverse=True)):   # 浅 -> 深
            # 用 <= 而不是 <：光从下方来时大量像素被 clip 到环境值，q(0.30) 恰好等于最小值，
            # 严格小于会得到空集（实测 candle 的 shadow 报 0%）。<= 保证嵌套且不为空。
            bsel = sel & (sn <= t)
            if not bsel.any():
                continue
            frac = (b + 1) / float(bands)
            tgt = max(lo_i, min(mid_i - 1, int(round(mid_i - frac * (mid_i - lo_i))))) \
                if mid_i > lo_i else mid_i
            banded[bsel] = cols[m[tgt]].astype(np.uint8)
            if tgt < mid_i:
                shadow |= bsel
                shadow_rgba[bsel, :3] = cols[m[tgt]].astype(np.uint8)
                shadow_rgba[bsel, 3] = 255
        hsel = sel & (sn >= thr_hl)
        if hsel.any() and hi_i > mid_i:
            col = cols[m[min(hi_i, mid_i + 1)]].astype(np.uint8)
            banded[hsel] = col
            hl |= hsel
            hl_rgba[hsel, :3] = col
            hl_rgba[hsel, 3] = 255

    # 分线 = 阈值 mask 的 1px 边界（mask & ~erode4(mask)）
    def boundary(mask):
        return mask & ~erode4(mask)

    if main_only:
        # 主分界线 = 受光/背光交界（取最浅的阴影阈值，即"刚好进入阴影"的那条）
        t_main = q(max(shadow_fracs[:bands]))
        term = boundary(opaque & (sn >= t_main))
    else:
        term = np.zeros_like(opaque)
        for t in sorted(set([thr_hl] + [q(f) for f in shadow_fracs[:bands]])):
            term |= boundary(opaque & (sn >= t))
    # 滤掉小连通块：细结构会生出成串闭合环，读起来是噪声
    if term.any():
        lab, nreg = label_mask(term, conn=8)
        sizes = np.bincount(lab.reshape(-1))
        keep = np.zeros(sizes.shape, bool)
        keep[1:] = sizes[1:] >= min_comp
        term = keep[lab]
    term_rgba = np.zeros((*opaque.shape, 4), np.uint8)
    term_rgba[term, :3] = (255, 40, 40)
    term_rgba[term, 3] = 255
    thr = {"shadow": [round(x, 4) for x in thr_sh], "highlight": round(thr_hl, 4),
           "terminator_px": int(term.sum()), "main_only": bool(main_only)}
    base_rgba = np.dstack([rgb_base, np.where(opaque, 255, 0).astype(np.uint8)])
    return {"banded": banded, "term": term, "shadow": shadow, "hl": hl,
            "base_rgba": base_rgba, "shadow_rgba": shadow_rgba, "hl_rgba": hl_rgba,
            "term_rgba": term_rgba, "thr": thr}


def run(path, presets, out_dir, zoom=3):
    rgba, _ = load_rgba(path)
    opaque = rgba[:, :, 3] > 127
    rgb = rgba[:, :, :3]
    if not opaque.any():
        return None
    name = os.path.splitext(os.path.basename(path))[0]
    out_dir = out_dir or os.path.dirname(os.path.abspath(path))
    os.makedirs(out_dir, exist_ok=True)

    cols, cnt = np.unique(rgb[opaque].reshape(-1, 3).astype(np.int32), axis=0, return_counts=True)
    base_rgb, ladder, pix_base, LabU, fams = flatten_base(rgb, opaque, cols, cnt)
    nx, ny, nz_, dt = normal_field(opaque)

    ys, xs = np.nonzero(opaque)
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1

    def crop(a):
        return a[y0:y1, x0:x1]

    records = []
    tile_rows = []
    for tag in presets:
        rig = PRESETS[tag] if tag in PRESETS else parse_rig(tag)
        s = shade_field(nx, ny, nz_, rig)
        res = render_rig(base_rgb, opaque, s, ladder, pix_base, cols)
        banded, term, shadow, hl, thr = res["banded"], res["term"], res["shadow"], res["hl"], res["thr"]

        sz = s.copy()
        sv = sz[opaque]
        sz = np.clip((sz - sv.min()) / max(1e-9, sv.max() - sv.min()), 0, 1)
        shade_img = np.dstack([(sz * 255).astype(np.uint8)] * 3 +
                              [np.where(opaque, 255, 0).astype(np.uint8)])
        term_rgb = base_rgb.copy()
        term_rgb[term] = (255, 40, 40)
        tile_term = np.dstack([term_rgb, np.where(opaque, 255, 0).astype(np.uint8)])
        tile_band = np.dstack([banded, np.where(opaque, 255, 0).astype(np.uint8)])
        p_shade = os.path.join(out_dir, f"{name}_{tag}_shade.png")
        p_term = os.path.join(out_dir, f"{name}_{tag}_term.png")
        p_band = os.path.join(out_dir, f"{name}_{tag}_bands.png")
        # 分层交付件：固有色 / 阴影 / 高光 / 仅分线（透明底）
        p_base = os.path.join(out_dir, f"{name}_{tag}_L0_base.png")
        p_shadow = os.path.join(out_dir, f"{name}_{tag}_L1_shadow.png")
        p_high = os.path.join(out_dir, f"{name}_{tag}_L2_highlight.png")
        p_termonly = os.path.join(out_dir, f"{name}_{tag}_L3_terminator.png")
        Image.fromarray(shade_img, "RGBA").save(p_shade)
        Image.fromarray(tile_term, "RGBA").save(p_term)
        Image.fromarray(tile_band, "RGBA").save(p_band)
        Image.fromarray(res["base_rgba"], "RGBA").save(p_base)
        Image.fromarray(res["shadow_rgba"], "RGBA").save(p_shadow)
        Image.fromarray(res["hl_rgba"], "RGBA").save(p_high)
        Image.fromarray(res["term_rgba"], "RGBA").save(p_termonly)

        rec = {"item": name, "rig": tag, "lights": [list(l) for l in rig],
               "shadow_px": int(shadow.sum()), "highlight_px": int(hl.sum()),
               # band_share 由分位阈值构造 ⇒ 每个光源都一样，只反映"带在哪"，不反映"照不到多少"
               "band_share": round(float(shadow.sum()) / float(opaque.sum()), 4),
               # unlit_share 是物理量：完全没被任何方向光照到的面积占比（这才是光源强弱的可比量）
               "unlit_share": round(float((s[opaque] <= AMBIENT + 0.02).sum()) / float(opaque.sum()), 4),
               "lit_mean": round(float(s[opaque].mean()), 4),
               "thresholds": thr, "out_bands": p_band, "out_term": p_term, "out_shade": p_shade,
               "layer_pngs": {"base": p_base, "shadow": p_shadow, "highlight": p_high,
                              "terminator": p_termonly}}
        # 预览瓦片单独存（numpy 数组不能进 JSON）
        tile_rows.append([crop(np.dstack([base_rgb, np.where(opaque, 255, 0).astype(np.uint8)])),
                          crop(shade_img), crop(tile_term), crop(tile_band)])
        records.append(rec)

    # 并排对照表：行 = 光源组，列 = 固有色 / 明暗场 / 分线 / 分带上色
    z = max(1, zoom)
    labels = ["BASE(flat)", "SHADE FIELD", "TERMINATOR", "BANDED"]
    ch, cw = tile_rows[0][0].shape[0], tile_rows[0][0].shape[1]
    gap, pad, rowlab, toplab = 4, 6, 74, 16
    W = pad + rowlab + 4 * (cw * z + gap) + pad
    H = pad + toplab + len(records) * (ch * z + gap) + pad
    sheet = np.zeros((H, W, 3), np.uint8)
    sheet[:, :] = (30, 30, 34)
    for i, rec in enumerate(records):
        ry = pad + toplab + i * (ch * z + gap)
        for j, t in enumerate(tile_rows[i]):
            big = np.repeat(np.repeat(t, z, axis=0), z, axis=1)
            al = big[:, :, 3:4].astype(np.float64) / 255.0
            comp = (big[:, :, :3].astype(np.float64) * al + np.array([52, 48, 44]) * (1 - al)).astype(np.uint8)
            ox = pad + rowlab + j * (cw * z + gap)
            sheet[ry:ry + comp.shape[0], ox:ox + comp.shape[1]] = comp
    im = Image.fromarray(sheet, "RGB")
    dr = ImageDraw.Draw(im)
    f, fb = _font(11), _font(13)
    for j, lab in enumerate(labels):
        dr.text((pad + rowlab + j * (cw * z + gap), 4), lab, fill=(235, 235, 242), font=fb)
    for i, rec in enumerate(records):
        ry = pad + toplab + i * (ch * z + gap) + ch * z // 2
        dr.text((pad, ry - 8), rec["rig"][:16], fill=(230, 230, 238), font=fb)
        for j, l in enumerate(rec["lights"]):
            dr.text((pad, ry + 6 + j * 12), f"{l[0]} {l[1]:.0f}°/{l[2]:.0f}° x{l[3]:.2f}",
                    fill=(170, 175, 185), font=f)
    p_sheet = os.path.join(out_dir, f"{name}_rigsheet.png")
    im.save(p_sheet)

    out = {"item": name, "size": f"{rgba.shape[1]}x{rgba.shape[0]}", "opaque_px": int(opaque.sum()),
           "colors": int(len(cols)), "families": int(len(fams)), "rigs": records,
           "compare_sheet": p_sheet}
    save_json(out, os.path.join(out_dir, f"{name}_rig.json"))
    return out


def parse_rig(spec):
    """'key:340,45,1.0;fill:160,35,0.35' -> rig 列表"""
    rig = []
    for part in spec.split(";"):
        part = part.strip()
        if not part:
            continue
        nm, rest = part.split(":")
        az, el, w = (float(v) for v in rest.split(","))
        rig.append((nm, az, el, w))
    if not rig:
        raise ValueError(f"bad rig spec: {spec}")
    return rig


def selftest():
    checks = []

    def ck(n, g, w):
        checks.append((n, g, w, g == w))
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest_tmp", "rig")
    os.makedirs(tmp, exist_ok=True)
    S = 56
    yy, xx = np.mgrid[0:S, 0:S]
    m = (yy - 28) ** 2 + (xx - 28) ** 2 <= 22 ** 2
    ramp = np.array([[45, 45, 45], [110, 110, 110], [175, 175, 175]], np.int32)
    img = np.zeros((S, S, 4), np.uint8)
    img[:, :, 3] = np.where(m, 255, 0)
    for i, c in enumerate(ramp):
        img[m & (np.abs(xx - 34 + (i - 1) * 8) < 8), :3] = c
    p = os.path.join(tmp, "ball.png")
    Image.fromarray(img, "RGBA").save(p)

    out = run(p, ["nw_key", "top", "key_fill", "candle"], tmp, zoom=2)
    by = {r["rig"]: r for r in out["rigs"]}
    ck("rig.all_present", sorted(by) == ["candle", "key_fill", "nw_key", "top"], True)
    ck("rig.nw_has_shadow", by["nw_key"]["shadow_px"] > 0, True)
    ck("rig.bands_written", all(os.path.exists(r["out_bands"]) for r in out["rigs"]), True)
    ck("rig.sheet_written", os.path.exists(out["compare_sheet"]), True)
    ck("rig.terminator_nonempty", all(r["thresholds"]["terminator_px"] > 0 for r in out["rigs"]), True)
    # 不同光源必须给出不同的分线位置（否则"模拟不同光源"就是假的）
    t_nw = np.asarray(Image.open(by["nw_key"]["out_term"]).convert("RGB"))[:, :, 0] == 255
    t_top = np.asarray(Image.open(by["top"]["out_term"]).convert("RGB"))[:, :, 0] == 255
    ck("rig.terminator_differs_by_light", bool((t_nw != t_top).sum() > 20), True)
    # 负对照：全环境光（无方向光）=> 没有分带、没有分线
    o = run(p, ["none:0,0,0.0"], tmp, zoom=2)
    ck("rig.negative_no_light", o["rigs"][0]["shadow_px"] == 0, True)
    ck("rig.rig_parse", parse_rig("key:340,45,1;fill:160,35,.3")[1][1], 160.0)

    bad = [c for c in checks if not c[3]]
    for n, g, w, ok in checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {n}: got={g} want={w}")
    print(f"SELFTEST={'PASS' if not bad else 'FAIL'} checks={len(checks) - len(bad)}/{len(checks)}")
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="")
    ap.add_argument("--glob", default="")
    ap.add_argument("--preset", default="nw_key;measured;key_fill;top;backlight;candle;two_side",
                    help="分号分隔的预设名，或 --rig 自定义")
    ap.add_argument("--rig", default="", help='"key:340,45,1.0;fill:160,35,0.35"')
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--zoom", type=int, default=3)
    ap.add_argument("--json-out", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    targets = ([a.image] if a.image else []) + [p for pat in a.glob.split(";") if pat
                                               for p in sorted(glob.glob(pat))]
    if not targets:
        ap.error("--image or --glob required")
    presets = [a.rig] if a.rig else [p for p in a.preset.split(";") if p]
    outs = []
    for t in targets:
        o = run(t, presets, a.out_dir, zoom=a.zoom)
        if o is None:
            continue
        outs.append(o)
        line = "  ".join(f"{r['rig']}:unlit{r['unlit_share'] * 100:.0f}%/lit{r['lit_mean']:.2f}"
                         f"/band{r['band_share'] * 100:.0f}%/term{r['thresholds']['terminator_px']}"
                         for r in o["rigs"])
        print(f"{os.path.basename(t)[:30]:30s} {line}")
        print(f"    sheet={o['compare_sheet']}")
    if a.json_out and outs:
        save_json({"items": outs}, a.json_out)
        print(f"JSON={a.json_out}")


if __name__ == "__main__":
    main()
