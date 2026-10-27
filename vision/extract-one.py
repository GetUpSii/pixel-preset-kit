# extract-one.py — 从"群组图"里抽出**单颗完整对象**（光照的前置条件：一个遮罩 = 一个形体）
# 为什么必须先做这步：光照模型（法线场/距离变换）假设 mask 是**单个凸形体**。
#   实测反面案例：把"3 颗蘑菇 + 薹藓 + 草"的碎片集合当整体求形体 => 明暗场是整个群组上的一个假穹顶，光照全错。
# 用法：
#   python extract-one.py --image cluster.png --report                 # 列连通体（面积/bbox/绿占比）
#   python extract-one.py --image cluster.png --drop-green --pick-largest --out one.png
#   python extract-one.py --image cluster.png --bbox 40,0,380,360 --out one.png
# 输出保证：恰好 1 个连通体（否则报 FAIL）、二值 alpha、保留原始颜色（不套调色板）。
import os
import sys
import argparse

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _vlib import load_rgba, label_mask, save_json  # noqa: E402


def green_share(rgb, mask):
    """粗略绿占比：G 明显高于 R 与 B（用于识别草/苔藓这类"底座"）。"""
    if not mask.any():
        return 0.0
    r, g, b = (rgb[:, :, i][mask].astype(np.int32) for i in range(3))
    is_green = (g > r + 8) & (g > b + 8)
    return round(float(is_green.mean()), 3)


def seeds_by_erosion(mask, want=2, max_iter=80, rel=0.2):
    """腐蚀到遮罩断成 want 个**显著**块 => 这些块就是"种子"。返回 (lab, ncomp, iters)。

    ⚠ 判据必须是**相对阈值**（≥ 最大块的 rel 倍）。用"≥8px"这类绝对阈值时，
    噪声碎片会被当成种子 ⇒ 腐蚀 1-2 层就"达标"停下、真正的粘连形体根本没分开（实测踩过）。
    """
    cur = mask.copy()
    for k in range(1, max_iter + 1):
        cur = erode4(cur)
        if not cur.any():
            break
        lab, n = label_mask(cur, conn=8)
        sizes = np.bincount(lab.reshape(-1))[1:]
        if len(sizes) == 0:
            break
        thr = max(4.0, rel * float(sizes.max()))
        sig = int((sizes >= thr).sum())
        if sig >= want:
            return lab, n, k
    return None, 0, 0


def erode4(m):
    e = m.copy()
    e[1:, :] &= m[:-1, :]
    e[:-1, :] &= m[1:, :]
    e[:, 1:] &= m[:, :-1]
    e[:, :-1] &= m[:, 1:]
    return e


def propagate_labels(mask, lab, iters=400):
    """把种子标签在 mask 内逐像素长回去（等价于最近种子的测地分配），不切坏形体。"""
    out = lab.copy()
    for _ in range(iters):
        grown = out.copy()
        for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            sh = np.roll(out, (dy, dx), axis=(0, 1))
            if dy == -1:
                sh[-1, :] = 0
            if dy == 1:
                sh[0, :] = 0
            if dx == -1:
                sh[:, -1] = 0
            if dx == 1:
                sh[:, 0] = 0
            fill = (grown == 0) & (sh > 0) & mask
            grown[fill] = sh[fill]
        if np.array_equal(grown, out):
            break
        out = grown
    return out


def components(rgba, drop_green=False, min_area=16, split_neck=0):
    opaque = rgba[:, :, 3] > 127
    rgb = rgba[:, :, :3]
    if drop_green:
        r, g, b = (rgb[:, :, i].astype(np.int32) for i in range(3))
        opaque = opaque & ~((g > r + 8) & (g > b + 8))
    lab, n = label_mask(opaque, conn=8)
    if split_neck >= 2:
        slab, sn, it = seeds_by_erosion(opaque, want=split_neck)
        if slab is not None:
            lab = propagate_labels(opaque, slab)
            lab[~opaque] = 0
            n = len(np.unique(lab)) - 1
    recs = []
    for i in range(1, n + 1):
        m = lab == i
        a = int(m.sum())
        if a < min_area:
            continue
        ys, xs = np.nonzero(m)
        recs.append({"id": i, "area": a,
                     "bbox": [int(xs.min()), int(ys.min()), int(xs.max() + 1), int(ys.max() + 1)],
                     "bbox_wh": [int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)],
                     "green_share": green_share(rgb, m)})
    recs.sort(key=lambda r: -r["area"])
    return opaque, lab, recs


def extract(rgba, lab, comp_ids, bbox=None, min_area=16):
    opaque = rgba[:, :, 3] > 127
    keep = np.isin(lab, comp_ids) if comp_ids else opaque.copy()
    if bbox:
        x0, y0, x1, y1 = bbox
        box = np.zeros_like(keep)
        box[max(0, y0):y1, max(0, x0):x1] = True
        keep = keep & box
    out = np.zeros_like(rgba)
    out[keep] = rgba[keep]
    ys, xs = np.nonzero(keep)
    if len(xs) == 0:
        return None
    crop = out[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    lab2, n2 = label_mask(crop[:, :, 3] > 127, conn=8)
    sizes = np.bincount(lab2.reshape(-1))[1:] if n2 else np.array([])
    return {"crop": crop, "components_out": int(n2),
            "component_sizes": sorted([int(s) for s in sizes], reverse=True)[:6],
            "bbox_in_src": [int(xs.min()), int(ys.min()), int(xs.max() + 1), int(ys.max() + 1)],
            "opaque_px": int(keep.sum()), "unique_colors": int(len(np.unique(crop[:, :, :3][crop[:, :, 3] > 127].reshape(-1, 3), axis=0)))}


def selftest():
    checks = []

    def ck(n, g, w):
        checks.append((n, g, w, g == w))
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest_tmp", "extract")
    os.makedirs(tmp, exist_ok=True)
    # 两个红块 + 一条绿桥：drop_green 后应分成 2 块
    img = np.zeros((40, 80, 4), np.uint8)
    img[10:30, 5:35, :3] = (200, 60, 50)
    img[10:30, 45:75, :3] = (190, 70, 55)
    img[18:22, 30:50, :3] = (70, 150, 60)          # 绿桥
    img[img[:, :, :3].any(-1), 3] = 255
    p = os.path.join(tmp, "bridge.png")
    Image.fromarray(img, "RGBA").save(p)

    rgba, _ = load_rgba(p)
    _, _, recs_all = components(rgba, drop_green=False)
    ck("bridge.one_component", len(recs_all), 1)
    opaque2, lab2, recs2 = components(rgba, drop_green=True)
    ck("bridge.splits", len(recs2), 2)
    ex = extract(rgba, lab2, [recs2[0]["id"]])
    ck("bridge.pick_one", ex["components_out"], 1)
    ck("bridge.size", ex["crop"].shape[1] > 20, True)

    # bbox 模式：只截左块
    ex2 = extract(rgba, lab2, None, bbox=(0, 0, 40, 40))
    ck("bbox.pick_left", ex2["components_out"], 1)

    # 细颈分割：两块被"同色细颈"相连时 drop_green 分不开，必须靠腐蚀种子 + 标签长回
    neck = np.zeros((40, 90, 4), np.uint8)
    neck[10:30, 5:45, :3] = (200, 60, 50)          # 左块（更大）
    neck[10:30, 55:80, :3] = (190, 70, 55)         # 右块
    neck[19:21, 45:55, :3] = (200, 60, 50)         # 同色细颈（2px）
    neck[neck[:, :, :3].any(-1), 3] = 255
    p3 = os.path.join(tmp, "neck.png")
    Image.fromarray(neck, "RGBA").save(p3)
    rgba3, _ = load_rgba(p3)
    _, _, recs_raw = components(rgba3, drop_green=False)
    ck("neck.one_before", len(recs_raw), 1)
    _, _, recs_neck = components(rgba3, drop_green=False, split_neck=2)
    ck("neck.splits_after", len(recs_neck) >= 2, True)
    ck("neck.largest_is_left", recs_neck[0]["bbox"][0] < 45, True)

    bad = [c for c in checks if not c[3]]
    for n, g, w, ok in checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {n}: got={g} want={w}")
    print(f"SELFTEST={'PASS' if not bad else 'FAIL'} checks={len(checks) - len(bad)}/{len(checks)}")
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--drop-green", action="store_true")
    ap.add_argument("--pick-largest", action="store_true")
    ap.add_argument("--keep-components", default="", help="逗号分隔的连通体 id（来自 --report）")
    ap.add_argument("--bbox", default="", help="x0,y0,x1,y1（与连通体取交集）")
    ap.add_argument("--min-area", type=int, default=16)
    ap.add_argument("--split-neck", type=int, default=0,
                    help="腐蚀到 N 块种子后把标签长回遮罩（细颈分割，用于切开粘连的多形体）")
    ap.add_argument("--out", default="")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if not a.image:
        ap.error("--image required")
    rgba, _ = load_rgba(a.image)
    opaque, lab, recs = components(rgba, drop_green=a.drop_green, min_area=a.min_area,
                                   split_neck=a.split_neck)

    if a.report or not (a.pick_largest or a.keep_components or a.bbox):
        print(f"IMAGE={os.path.basename(a.image)} drop_green={a.drop_green} components={len(recs)}")
        for r in recs[:20]:
            print(f"  id={r['id']:3d} area={r['area']:7d} bbox={r['bbox']} wh={r['bbox_wh']} green={r['green_share']}")
        if a.json_out:
            save_json({"image": a.image, "drop_green": a.drop_green, "components": recs}, a.json_out)
            print(f"JSON={a.json_out}")
        return

    ids = [int(v) for v in a.keep_components.split(",") if v.strip()] if a.keep_components else (
        [recs[0]["id"]] if a.pick_largest and recs else None)
    bbox = tuple(int(v) for v in a.bbox.split(",")) if a.bbox else None
    ex = extract(rgba, lab, ids, bbox=bbox, min_area=a.min_area)
    if ex is None:
        print("EXTRACT=FAIL empty result")
        sys.exit(1)
    ok = ex["components_out"] == 1
    print(f"EXTRACT={'PASS' if ok else 'FAIL'} components_out={ex['components_out']} "
          f"sizes={ex['component_sizes']} bbox_src={ex['bbox_in_src']} "
          f"opaque={ex['opaque_px']} colors={ex['unique_colors']} out={ex['crop'].shape[1]}x{ex['crop'].shape[0]}")
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        Image.fromarray(ex["crop"], "RGBA").save(a.out)
        print(f"OUT={a.out}")
    if a.json_out:
        save_json({"image": a.image, "drop_green": a.drop_green, "picked": ids, "bbox": bbox,
                   "components_out": ex["components_out"], "component_sizes": ex["component_sizes"],
                   "bbox_in_src": ex["bbox_in_src"], "opaque_px": ex["opaque_px"],
                   "unique_colors": ex["unique_colors"], "out": a.out}, a.json_out)
        print(f"JSON={a.json_out}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
