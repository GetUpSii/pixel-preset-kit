# palette-forge.py — 把 median-cut 顺序的调色板重铸为「ramp 连续索引」调色板（C1 的前提）
# 算法（可复现，不用外部模型）：
#   1) 用真实产物的像素直方图给每个源色算使用权重
#   2) sRGB -> CIE Lab，按权重做 k-means++/Lloyd 聚类 => k = keep/steps 条 ramp
#   3) 每条 ramp 内按权重做 1 维 k-means 选 steps 个「medoid」（取原始色，不发明新色）
#   4) 每个源色映射到「本 ramp 内最近 medoid」=> 索引连续、step±1 = 明暗一阶
#   5) 输出 .gpl + ramp map JSON + 重映射表 + ramp-swatch 对照图 + 加权映射误差
# 用法：
#   python palette-forge.py --palette P.gpl --range 0-47 --keep 32 --steps 4 \
#       --weights-from "img1.png;img2.png" --out-gpl O.gpl --out-json O.json --out-swatch O.png
import os
import sys
import glob
import argparse

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _vlib import read_gpl, write_gpl, save_json, load_rgba, _font, hexc  # noqa: E402

HUE_NAMES = [(15, "red"), (45, "orange"), (70, "yellow"), (100, "yellowgreen"), (150, "green"),
             (190, "teal"), (250, "blue"), (290, "violet"), (330, "magenta"), (361, "red")]


def hue_name(rgb):
    r, g, b = [float(v) / 255.0 for v in rgb]
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d < 1e-6:
        return "neutral"
    if mx == r:
        h = (60 * ((g - b) / d)) % 360
    elif mx == g:
        h = 60 * ((b - r) / d) + 120
    else:
        h = 60 * ((r - g) / d) + 240
    for hi, nm in HUE_NAMES:
        if h < hi:
            return nm
    return "red"


def srgb_to_lab(rgb):
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    M = np.array([[0.4124564, 0.3575761, 0.1804375],
                  [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    xyz = (c @ M.T) / np.array([0.95047, 1.0, 1.08883])
    e, k = 216.0 / 24389.0, 24389.0 / 27.0
    f = np.where(xyz > e, np.cbrt(xyz), (k * xyz + 16.0) / 116.0)
    return np.stack([116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]), 200 * (f[:, 1] - f[:, 2])], 1)


def wkmeans(X, w, k, iters=80, seed=0):
    """加权 k-means（k-means++ 初始化，空簇用最远点重启）。确定性：固定 seed。"""
    X = np.asarray(X, dtype=np.float64)
    w = np.asarray(w, dtype=np.float64)
    n = X.shape[0]
    k = max(1, min(k, n))
    rng = np.random.default_rng(seed)
    C = [X[int(rng.choice(n, p=w / w.sum()))]]
    for _ in range(k - 1):
        d = np.min(((X[:, None, :] - np.array(C)[None, :, :]) ** 2).sum(-1), axis=1)
        p = d * w
        C.append(X[int(rng.choice(n, p=(p / p.sum()) if p.sum() > 0 else w / w.sum()))])
    C = np.array(C)
    for _ in range(iters):
        lab = ((X[:, None, :] - C[None, :, :]) ** 2).sum(-1).argmin(1)
        newC = C.copy()
        for j in range(k):
            m = lab == j
            if m.any():
                newC[j] = (X[m] * w[m, None]).sum(0) / w[m].sum()
            else:
                d = ((X - C) ** 2).sum(-1).min(1) * w
                newC[j] = X[int(d.argmax())]
        if np.allclose(newC, C):
            break
        C = newC
    lab = ((X[:, None, :] - C[None, :, :]) ** 2).sum(-1).argmin(1)
    return C, lab


def rebalance(cl, feats, k, min_size):
    """保证每条 ramp 至少 min_size 个成员（否则末尾会出现 1-2 阶的残废 ramp）。"""
    cl = cl.copy()
    n = len(cl)
    if n < k * min_size:
        return cl
    for _ in range(500):
        sizes = np.array([int((cl == j).sum()) for j in range(k)])
        if sizes.min() >= min_size:
            break
        small, big = int(np.argmin(sizes)), int(np.argmax(sizes))
        if sizes[big] <= min_size:
            break
        cen = feats[cl == small].mean(0)
        idxs = np.nonzero(cl == big)[0]
        d = ((feats[idxs] - cen) ** 2).sum(1)
        cl[idxs[int(d.argmin())]] = small
    return cl


def chroma_hue(Lab):
    a, b = Lab[:, 1], Lab[:, 2]
    return np.sqrt(a * a + b * b), np.arctan2(b, a)


def balance(cl, G, k, min_size, fam_type):
    """均衡化：成员数 < min_size 的族，从同类（中性/彩色）最大族里"抢"最近的成员。

    与"把小族并掉"相比，均衡化不减少族数、不丢颜色 —— 因此恰好用满 keep 预算。
    """
    cl = cl.copy()
    for _ in range(1000):
        sizes = [int((cl == j).sum()) for j in range(k)]
        small = next((j for j in range(k) if sizes[j] < min_size), None)
        if small is None:
            break
        donors = [j for j in range(k) if sizes[j] > min_size and fam_type[j] == fam_type[small]]
        if not donors:
            donors = [j for j in range(k) if sizes[j] > min_size]
        if not donors:
            break
        big = max(donors, key=lambda j: sizes[j])
        cen = G[cl == small].mean(0)
        idxs = np.nonzero(cl == big)[0]
        d = ((G[idxs] - cen) ** 2).sum(1)
        cl[idxs[int(d.argmin())]] = small
    return cl


def merge_small(cl, G, k, steps, is_neutral):
    """把成员数 < steps 的族并入同类（中性->中性 / 彩色->彩色）特征最近的族。

    否则会出现 1-2 阶的残废 ramp（实测 v1 输出 29 色就是此因）。
    返回 (cl, k, is_neutral)——族数可能减少。
    """
    cl = cl.copy()
    for _ in range(200):
        sizes = [int((cl == j).sum()) for j in range(k)]
        small = next((j for j in range(k) if sizes[j] < steps), None)
        if small is None or k <= 1:
            break
        cen = {j: G[cl == j].mean(0) for j in range(k)}
        cands = [j for j in range(k) if j != small and sizes[j] >= steps
                 and is_neutral[j] == is_neutral[small]]
        if not cands:
            cands = [j for j in range(k) if j != small and sizes[j] >= steps]
        if not cands:
            break
        tgt = min(cands, key=lambda j: float(((cen[j] - cen[small]) ** 2).sum()))
        cl[cl == small] = tgt
        keep_list = [j for j in range(k) if j != small]
        remap = {old: new for new, old in enumerate(keep_list)}
        cl = np.array([remap[int(v)] for v in cl], dtype=int)
        is_neutral = [is_neutral[j] for j in keep_list]
        k -= 1
    return cl, k, is_neutral


def forge(colors, weights, keep, steps, seed=0, c_thr=8.0):
    """返回 ramps: [{steps:[{idx, rgb, lab}], members:[...]}]，以及映射与误差。

    分族规则（两段式，色相优先）：
      1) 彩度 C < c_thr 的颜色视为中性族，按明度单独成族；
      2) 其余颜色在单位色相圆 (cos h, sin h) 上做加权 k-means —— 明度**不参与**分族。
    （反例：用 (L,a,b) 直接聚类时，低彩度色会被明度主导，把深红和灰并成同一条 ramp；
      自检 mix.split 用例专锁此缺陷。）
    族内再按明度选 steps 个 medoid（取原始色，不发明新色），并均衡化保证每族 >= steps 个成员。
    """
    colors = np.asarray(colors, dtype=np.int32)
    weights = np.asarray(weights, dtype=np.float64)
    weights = np.where(weights <= 0, 1.0, weights)          # 零使用色给最小权重，避免被抹掉
    Lab = srgb_to_lab(colors)
    C, H = chroma_hue(Lab)
    k = max(1, int(round(keep / float(steps))))

    neutral = C < c_thr
    w_n, w_c = weights[neutral].sum(), weights[~neutral].sum()
    k_n = 0
    if neutral.any():
        k_n = int(np.clip(round(k * (w_n / max(1e-9, w_n + w_c))), 1, k - 1)) if (~neutral).any() else k
    k_c = k - k_n

    cl = np.full(len(colors), -1, dtype=int)
    fam_is_neutral = []
    nxt = 0
    if k_c > 0 and (~neutral).any():
        F = np.stack([np.cos(H[~neutral]), np.sin(H[~neutral])], 1)
        _, clc = wkmeans(F, weights[~neutral], k_c, seed=seed)
        for j in sorted(set(clc.tolist())):
            lab_idx = np.nonzero(~neutral)[0][clc == j]
            cl[lab_idx] = nxt
            fam_is_neutral.append(False)
            nxt += 1
    if k_n > 0 and neutral.any():
        F = Lab[neutral][:, 0:1]
        _, cln = wkmeans(F, weights[neutral], k_n, seed=seed)
        for j in sorted(set(cln.tolist())):
            lab_idx = np.nonzero(neutral)[0][cln == j]
            cl[lab_idx] = nxt
            fam_is_neutral.append(True)
            nxt += 1
    if (cl < 0).any():                                     # 兜底：漏掉的色归到最近族
        for i in np.nonzero(cl < 0)[0]:
            cl[i] = int(np.argmin([((Lab[i] - Lab[cl == j].mean(0)) ** 2).sum() for j in range(nxt)]))
    k = nxt

    # 特征空间（用于小族合并的"最近邻"判定）：彩色=单位色相向量，中性=明度
    G = np.zeros((len(colors), 2))
    G[~neutral, 0], G[~neutral, 1] = np.cos(H[~neutral]), np.sin(H[~neutral])
    G[neutral, 0] = Lab[neutral, 0]
    cl = balance(cl, G, k, steps, fam_is_neutral)

    ramps = []
    for j in range(k):
        m = np.nonzero(cl == j)[0]
        if len(m) == 0:
            continue
        if len(m) == steps:
            levels = sorted(m.tolist(), key=lambda i: Lab[i][0])      # 正好 steps 个 -> 全用
        else:
            L1 = Lab[m][:, 0:1]                                       # 1 维按明度选阶
            _, sl = wkmeans(L1, weights[m], steps, seed=seed)
            levels = []
            for s in sorted(set(sl.tolist())):
                mm = m[sl == s]
                mu = (Lab[mm] * weights[mm, None]).sum(0) / weights[mm].sum()
                medoid = mm[int(((Lab[mm] - mu) ** 2).sum(1).argmin())]  # 取原始色，不发明新色
                levels.append(int(medoid))
            levels = sorted(levels, key=lambda i: Lab[i][0])
            if len(levels) < steps:                                   # 仍不足则并入族内最远点
                rest = [int(i) for i in m if int(i) not in levels]
                rest.sort(key=lambda i: -float(((Lab[i] - Lab[levels[-1]]) ** 2).sum()))
                levels = sorted((levels + rest[:steps - len(levels)]), key=lambda i: Lab[i][0])
        ramps.append({"members": m, "levels": levels, "neutral": bool(fam_is_neutral[j])})

    # 排序：彩色族按平均色相角，中性族排最后；阶内按明度升序
    def ramp_key(r):
        li = r["levels"]
        if r.get("neutral"):
            return (1, 0.0, float(Lab[li][:, 0].mean()))
        return (0, float(np.arctan2(Lab[li][:, 2].mean(), Lab[li][:, 1].mean())),
                float(Lab[li][:, 0].mean()))

    ramps.sort(key=ramp_key)

    def ramp_label(r):
        """近中性族的色相名不可靠（近黑色算不出色相）=> 一律标 neutral。"""
        li = r["levels"]
        c = float(np.sqrt(Lab[li][:, 1] ** 2 + Lab[li][:, 2] ** 2).mean())
        if r.get("neutral") or c < c_thr:
            return "neutral"
        return hue_name(colors[li].mean(0))

    for r in ramps:
        r["name"] = ramp_label(r)

    index_of = {}
    out_colors, out_meta, src_of_out = [], [], []
    for ri, r in enumerate(ramps):
        start = len(out_colors)
        for si, cidx in enumerate(r["levels"]):
            out_colors.append([int(v) for v in colors[cidx]])
            out_meta.append({"index": len(out_colors) - 1, "ramp": ri, "step": si})
            src_of_out.append(int(cidx))       # 输出索引 -> 源色索引（两个空间必须分清）
        r["start"] = start
        for cidx in r["members"]:
            d = [((Lab[cidx] - Lab[l]) ** 2).sum() for l in r["levels"]]
            index_of[int(cidx)] = start + int(np.argmin(d))

    # 误差必须在「源色 Lab」空间里量：源色 i vs 它被映射到的那个输出条目背后的源色
    errs = np.array([np.sqrt(((Lab[i] - Lab[src_of_out[index_of[i]]]) ** 2).sum())
                     for i in range(len(colors))])
    wsum = weights.sum()
    stats = {
        "colors_in": int(len(colors)), "colors_out": int(len(out_colors)),
        "ramps": int(len(ramps)), "steps_per_ramp": int(steps),
        "lab_err_weighted_mean": round(float((errs * weights).sum() / wsum), 2),
        "lab_err_p95": round(float(np.percentile(errs, 95)), 2),
        "lab_err_max": round(float(errs.max()), 2),
        "identical_colors": int(sum(1 for i in range(len(colors)) if index_of[i] is not None
                                    and tuple(out_colors[index_of[i]]) == tuple(int(v) for v in colors[i]))),
    }
    return ramps, out_colors, out_meta, index_of, stats, Lab, errs


def swatch(ramps, colors, out_path, cell=46):
    n_ramp = len(ramps)
    W = 8 + 96 + max(len(r["levels"]) for r in ramps) * cell
    H = 30 + n_ramp * (cell + 18)
    im = Image.new("RGB", (W, H), (26, 26, 30))
    dr = ImageDraw.Draw(im)
    f, fb = _font(11), _font(13)
    dr.text((6, 6), "ramp / (index) hex  ->  step0=dark ... stepN=light", fill=(235, 235, 240), font=fb)
    for ri, r in enumerate(ramps):
        y = 30 + ri * (cell + 18)
        rgb0 = colors[r["levels"]]
        dr.text((6, y + cell // 2 - 6), f"R{ri:02d} {r.get('name', '?')}", fill=(220, 220, 226), font=f)
        for si, cidx in enumerate(r["levels"]):
            c = [int(v) for v in colors[cidx]]
            x = 96 + si * cell
            dr.rectangle([x + 1, y + 1, x + cell - 3, y + cell - 3], fill=tuple(c), outline=(80, 80, 88))
            dr.text((x + 2, y + cell - 14), f"({r['start'] + si})", fill=(20, 20, 20), font=f)
            dr.text((x + 2, y + cell + 1), hexc(c)[1:], fill=(200, 200, 206), font=f)
    im.save(out_path)
    return out_path


def usage_weights(colors, sources):
    """按真实产物的像素直方图统计每个源色使用量（不在源图里出现的色 -> 0）。"""
    lut = {tuple(int(v) for v in c): i for i, c in enumerate(np.asarray(colors))}
    w = np.zeros(len(colors), dtype=np.float64)
    for pat in sources:
        for p in sorted(glob.glob(pat)):
            rgba, _ = load_rgba(p)
            rgb = rgba[:, :, :3]
            a = rgba[:, :, 3]
            if a.min() < 255:
                rgb = rgb[a > 127]
            flat = rgb.reshape(-1, 3).astype(np.int32)
            cols, cnt = np.unique(flat, axis=0, return_counts=True)
            for c, n in zip(cols, cnt):
                i = lut.get((int(c[0]), int(c[1]), int(c[2])))
                if i is not None:
                    w[i] += int(n)
    return w


def selftest():
    checks = []

    def ck(name, got, want):
        checks.append((name, got, want, got == want))

    # 4 个色族 × 4 档明度 = 16 色，keep=16 steps=4 应当无损
    fams = [(200, 40, 40), (40, 180, 60), (50, 80, 200), (150, 120, 60)]
    cols = []
    for r, g, b in fams:
        for s in range(4):
            k = 0.55 + 0.15 * s
            cols.append([min(255, int(r * k)), min(255, int(g * k)), min(255, int(b * k))])
    cols = np.array(cols, np.int32)
    w = np.ones(16)
    ramps, outc, meta, idx_of, st, Lab, errs = forge(cols, w, 16, 4)
    ck("identity.ramps", st["ramps"], 4)
    ck("identity.out", st["colors_out"], 16)
    ck("identity.err0", st["lab_err_weighted_mean"], 0.0)

    asc = True
    for r in ramps:
        ls = [Lab[i][0] for i in r["levels"]]
        if any(ls[i] >= ls[i + 1] for i in range(len(ls) - 1)):
            asc = False
    ck("identity.luma_ascending", asc, True)
    ck("identity.steps_uniform", all(len(r["levels"]) == 4 for r in ramps), True)

    # 32 色预算必须用满（不足则尾巴出现 1-2 阶残废 ramp）
    big = np.array([[int(v * f), int(v * f * 0.8), int(v * f * 0.6)]
                    for v in (60, 110, 160, 210) for f in (0.5, 0.8, 1.0, 1.2)], np.int32)
    big = np.clip(big, 0, 255)
    rb, _, _, _, stb, _, _ = forge(big, np.ones(len(big)), 16, 4)
    ck("budget.out_full", stb["colors_out"], 16)
    ck("budget.ramps", stb["ramps"], 4)
    ck("budget.steps_uniform", all(len(r["levels"]) == 4 for r in rb), True)

    # 8 级灰：keep=4 必须丢信息，keep=8 应当更准（量尺"有牙"）
    grays = np.array([[v, v, v] for v in (16, 48, 80, 112, 144, 176, 208, 240)], np.int32)
    w8 = np.ones(8)
    _, _, _, _, st4, _, _ = forge(grays, w8, 4, 4)
    _, _, _, _, st8, _, _ = forge(grays, w8, 8, 4)
    ck("gray4.out", st4["colors_out"], 4)
    ck("gray4.lossy", st4["lab_err_weighted_mean"] > 0.0, True)
    ck("gray8.beats_gray4", st8["lab_err_weighted_mean"] < st4["lab_err_weighted_mean"], True)

    # 色相族优先：4 个红 + 4 个灰 必须分成两条 ramp（v2 用 (L,a,b) 聚类时会把它们并在一起）
    reds = np.array([[150, 20, 20], [180, 40, 30], [210, 60, 40], [240, 80, 50]], np.int32)
    greys = np.array([[60, 60, 60], [110, 110, 110], [160, 160, 160], [210, 210, 210]], np.int32)
    mix = np.vstack([reds, greys])
    rm, _, _, _, stm, _, _ = forge(mix, np.ones(8), 8, 4)
    sets = [set(int(i) for i in r["members"]) for r in rm]
    ck("mix.split", len(rm), 2)
    ck("mix.red_isolated", any(s <= {0, 1, 2, 3} and len(s) == 4 for s in sets), True)
    ck("mix.grey_isolated", any(s <= {4, 5, 6, 7} and len(s) == 4 for s in sets), True)

    bad = [c for c in checks if not c[3]]
    for n, g, wv, ok in checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {n}: got={g} want={wv}")
    print(f"SELFTEST={'PASS' if not bad else 'FAIL'} checks={len(checks) - len(bad)}/{len(checks)}")
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--palette", default="")
    ap.add_argument("--range", default="", help="源调色板取哪一段，如 0-47")
    ap.add_argument("--keep", type=int, default=32)
    ap.add_argument("--steps", type=int, default=4)
    ap.add_argument("--weights-from", default="", help="分号分隔的 glob，用于统计使用权重")
    ap.add_argument("--out-gpl", default="")
    ap.add_argument("--out-json", default="")
    ap.add_argument("--out-swatch", default="")
    ap.add_argument("--name", default="forged")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())

    src = read_gpl(a.palette)
    if a.range:
        lo, hi = (int(v) for v in a.range.split("-"))
        colors = src[lo:hi + 1]
    else:
        colors = src
    w = usage_weights(colors, a.weights_from.split(";")) if a.weights_from else np.ones(len(colors))
    ramps, outc, meta, idx_of, stats, Lab, errs = forge(colors, w, a.keep, a.steps)

    print(f"IN={stats['colors_in']} OUT={stats['colors_out']} RAMPS={stats['ramps']} STEPS={a.steps}")
    for ri, r in enumerate(ramps):
        tw = w[r["members"]].sum()
        print(f"  R{ri:02d} {r.get('name', '?'):12s} start={r['start']:2d} "
              f"members={len(r['members']):2d} weight={tw:8.0f} "
              f"steps={' '.join(hexc(colors[i]) for i in r['levels'])}")
    print(f"ERR lab weighted_mean={stats['lab_err_weighted_mean']} p95={stats['lab_err_p95']} "
          f"max={stats['lab_err_max']} identical={stats['identical_colors']}/{stats['colors_in']}")

    if a.out_gpl:
        write_gpl(outc, a.out_gpl, name=a.name,
                  tags=[f"r{m['ramp']}s{m['step']}" for m in meta])
        print(f"GPL={a.out_gpl}")
    if a.out_json:
        payload = {
            "source": a.palette, "range": a.range, "keep": a.keep, "steps": a.steps,
            "stats": stats,
            "ramps": [{"ramp": ri, "name": hue_name(colors[r["levels"]].mean(0)), "start": r["start"],
                       "steps": [{"index": r["start"] + si, "rgb": [int(v) for v in colors[c]],
                                  "hex": hexc(colors[c]), "lab_l": round(float(Lab[c][0]), 2)}
                                 for si, c in enumerate(r["levels"])],
                       "luma_ascending": all(Lab[r["levels"]][i][0] < Lab[r["levels"]][i + 1][0]
                                             for i in range(len(r["levels"]) - 1))}
                      for ri, r in enumerate(ramps)],
            "remap": {hexc(colors[i]): int(idx_of[i]) for i in range(len(colors))},
        }
        save_json(payload, a.out_json)
        print(f"JSON={a.out_json}")
    if a.out_swatch:
        print(f"SWATCH={swatch(ramps, colors, a.out_swatch)}")


if __name__ == "__main__":
    main()
