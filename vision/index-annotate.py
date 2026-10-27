# index-annotate.py — 标注取色：对每件素材逐像素取色 -> 转成「色号」(调色板索引)
# 产出：
#   1) <name>_indexed.png   取色后的索引色图（等价于量化到该调色板）
#   2) <name>_annot.png     放大 + 每个色号的区域打上「色号」标注 + 底部图例（色号/hex/ramp-阶/占比）
#   3) <name>_colors.json   逐色号清单：index / hex / ramp-step / 像素数 / 占比 / 映射误差
#   4) index_annotate_summary.json  多图汇总（含"哪些色号在用、哪些没用上"）
# 用法：
#   python index-annotate.py --palette P.gpl --ramp-map P.json --image A.png --out-dir D --zoom 2
#   python index-annotate.py --palette P.gpl --ramp-map P.json --glob "props/*.png" --out-dir D
import os
import sys
import glob
import argparse
import json

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _vlib import read_gpl, load_rgba, label_mask, nearest_index, save_json, _font, hexc  # noqa: E402


def load_ramp_map(path):
    """来自 palette-forge 的 ramp map：index -> "R03-2"。"""
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        d = json.load(f)
    out = {}
    for r in d.get("ramps", []):
        for st in r.get("steps", []):
            out[int(st["index"])] = {"ramp": r.get("ramp"), "name": r.get("name"),
                                     "step": st.get("step"), "hex": st.get("hex")}
    return out


def analyze_one(path, pal, ramp_map, out_dir, zoom=2, label_min_area=24, chunk_label=True):
    rgba, _ = load_rgba(path)
    h, w, _ = rgba.shape
    alpha = rgba[:, :, 3]
    opaque = alpha > 127
    rgb = rgba[:, :, :3]
    base = os.path.splitext(os.path.basename(path))[0]

    idx = np.zeros((h, w), np.int64)
    err = np.zeros((h, w), np.float64)
    if opaque.any():
        ii, d2 = nearest_index(rgb[opaque].reshape(-1, 3), pal)
        idx[opaque] = ii
        err[opaque] = np.sqrt(d2.astype(np.float64))
    out_rgb = np.zeros((h, w, 3), np.uint8)
    out_rgb[opaque] = pal[idx[opaque]].astype(np.uint8)
    out_rgba = np.dstack([out_rgb, np.where(opaque, 255, 0).astype(np.uint8)])

    indexed_png = os.path.join(out_dir, f"{base}_indexed.png")
    Image.fromarray(out_rgba, "RGBA").save(indexed_png)

    # ---- 逐色号统计 ----
    counts = np.bincount(idx[opaque], minlength=len(pal)) if opaque.any() else np.zeros(len(pal), int)
    total = int(counts.sum())
    entries = []
    for i in np.nonzero(counts)[0]:
        m = ramp_map.get(int(i), {})
        entries.append({
            "color_no": int(i),                       # 色号 = 调色板索引
            "hex": hexc(pal[i]),
            "ramp": m.get("name"), "ramp_index": m.get("ramp"), "step": m.get("step"),
            "px": int(counts[i]),
            "share": round(float(counts[i]) / total, 4) if total else 0.0,
        })
    entries.sort(key=lambda e: -e["px"])

    # ---- 放大 + 标注（必须裁到物件 bbox，否则小物件在整幅画布上标签会叠成一团）----
    ys, xs = np.nonzero(opaque)
    if len(xs):
        y0, y1 = max(0, int(ys.min()) - 2), min(h, int(ys.max()) + 3)
        x0, x1 = max(0, int(xs.min()) - 2), min(w, int(xs.max()) + 3)
    else:
        y0, y1, x0, x1 = 0, h, 0, w
    idxc, opc = idx[y0:y1, x0:x1], opaque[y0:y1, x0:x1]
    cropped = out_rgba[y0:y1, x0:x1]
    z = max(1, zoom)
    big = np.repeat(np.repeat(cropped, z, axis=0), z, axis=1)
    lab_h, lab_w = 16, 15
    legend_rows = max(1, int(np.ceil(len(entries) / 6)))
    legend_h = 24 + legend_rows * 20
    canvas = np.zeros((big.shape[0] + legend_h, max(big.shape[1], 460), 4), np.uint8)
    canvas[:, :, 3] = 255
    canvas[:, :, :3] = 24
    canvas[: big.shape[0], : big.shape[1]] = big
    im = Image.fromarray(canvas, "RGBA")
    dr = ImageDraw.Draw(im)
    f, fs = _font(11), _font(13)

    # 标注：每个色号的最大区域质心处写色号
    for e in entries:
        i = e["color_no"]
        m = (opc & (idxc == i))
        if int(m.sum()) < label_min_area:
            continue
        lab, nreg = label_mask(m, conn=8)
        if nreg == 0:
            continue
        sizes = np.bincount(lab.reshape(-1))
        sizes[0] = 0
        rid = int(sizes.argmax())
        ys, xs = np.nonzero(lab == rid)
        cx, cy = int(xs.mean()) * z, int(ys.mean()) * z
        txt = str(i)
        tw = 7 * len(txt) + 4
        dr.rectangle([cx - tw // 2, cy - 8, cx + tw // 2, cy + 8], fill=(255, 255, 255), outline=(20, 20, 20))
        dr.text((cx - tw // 2 + 2, cy - 7), txt, fill=(10, 10, 10), font=f)

    # 图例
    ly = big.shape[0] + 4
    dr.text((6, ly), f"{base}  colors_used={len(entries)}  px={total} "
                     f"err(mean/p95/max)={err[opaque].mean():.1f}/{np.percentile(err[opaque], 95) if total else 0:.1f}/"
                     f"{err[opaque].max() if total else 0:.1f}", fill=(235, 235, 240), font=fs)
    for k, e in enumerate(entries):
        col, row = k % 6, k // 6
        x = 6 + col * 76
        y = ly + 20 + row * 20
        dr.rectangle([x, y, x + 14, y + 14], fill=tuple(int(v) for v in pal[e["color_no"]]), outline=(90, 90, 98))
        dr.text((x + 18, y + 2), f"{e['color_no']}:{e['share'] * 100:.0f}%", fill=(215, 215, 220), font=f)
    annot_png = os.path.join(out_dir, f"{base}_annot.png")
    im.save(annot_png)

    rec = {
        "image": path, "size": f"{w}x{h}", "opaque_px": total,
        "bbox": [x0, y0, x1 - x0, y1 - y0],
        "colors_used": len(entries),
        "mapping_err_mean": round(float(err[opaque].mean()), 2) if total else 0.0,
        "mapping_err_p95": round(float(np.percentile(err[opaque], 95)), 2) if total else 0.0,
        "mapping_err_max": round(float(err[opaque].max()), 2) if total else 0.0,
        "colors": entries,
        "indexed_png": indexed_png, "annot_png": annot_png,
    }
    save_json(rec, os.path.join(out_dir, f"{base}_colors.json"))
    return rec


def selftest():
    checks = []

    def ck(name, got, want):
        checks.append((name, got, want, got == want))

    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest_tmp")
    os.makedirs(tmp, exist_ok=True)
    pal = np.array([[200, 30, 30], [30, 200, 60], [40, 60, 220]], np.int32)
    gpl = os.path.join(tmp, "pal3.gpl")
    with open(gpl, "w", encoding="ascii", newline="\n") as f:
        f.write("GIMP Palette\nName: t3\nColumns: 8\n#\n")
        for c in pal:
            f.write("%3d %3d %3d\tc\n" % (c[0], c[1], c[2]))
    img = np.zeros((20, 30, 4), np.uint8)
    img[:, :, 3] = 255
    img[:, :10, :3] = (198, 32, 33)        # 近红 -> 色号 0
    img[:, 10:20, :3] = (35, 205, 55)      # 近绿 -> 色号 1
    img[:, 20:, :3] = (255, 255, 255)      # 白 -> 最近是蓝? 距离: 红 351, 绿 437, 蓝 411 -> 红
    p = os.path.join(tmp, "pal3_probe.png")
    Image.fromarray(img, "RGBA").save(p)

    loaded = read_gpl(gpl)
    ck("gpl.roundtrip", [list(map(int, c)) for c in loaded], [list(map(int, c)) for c in pal])
    ii, _ = nearest_index(np.array([[198, 32, 33], [35, 205, 55]]), loaded)
    ck("nearest.red0", int(ii[0]), 0)
    ck("nearest.green1", int(ii[1]), 1)

    rec = analyze_one(p, loaded, {0: {"ramp": 0, "name": "red", "step": 0, "hex": "#c81e1e"}},
                      tmp, zoom=2)
    ck("annot.colors_used", rec["colors_used"], 3)
    ck("annot.opaque", rec["opaque_px"], 20 * 30)
    ck("annot.stat_max", rec["colors"][0]["px"], 10 * 20)
    ck("annot.indexed_exists", os.path.exists(rec["indexed_png"]), True)
    ck("annot.annot_exists", os.path.exists(rec["annot_png"]), True)
    back = np.asarray(Image.open(rec["indexed_png"]).convert("RGB"))
    ck("annot.indexed_color0", tuple(int(v) for v in back[5, 5]), (200, 30, 30))

    bad = [c for c in checks if not c[3]]
    for n, g, w, ok in checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {n}: got={g} want={w}")
    print(f"SELFTEST={'PASS' if not bad else 'FAIL'} checks={len(checks) - len(bad)}/{len(checks)}")
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--palette", default="")
    ap.add_argument("--ramp-map", default="", help="palette-forge 的 json（用于标注 ramp/阶）")
    ap.add_argument("--image", default="")
    ap.add_argument("--glob", default="")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--zoom", type=int, default=2)
    ap.add_argument("--json-out", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    if not a.palette or not (a.image or a.glob):
        ap.error("--palette and (--image|--glob) required")
    pal = read_gpl(a.palette)
    rm = load_ramp_map(a.ramp_map)
    out_dir = a.out_dir or os.path.dirname(os.path.abspath(a.image or a.glob.split(";")[0]))
    os.makedirs(out_dir, exist_ok=True)

    targets = []
    if a.image:
        targets.append(a.image)
    for pat in a.glob.split(";"):
        if pat:
            targets += sorted(glob.glob(pat))

    recs = []
    for t in targets:
        r = analyze_one(t, pal, rm, out_dir, zoom=a.zoom)
        recs.append(r)
        used = {e["color_no"] for e in r["colors"]}
        print(f"{os.path.basename(t):34s} 色号={len(used):2d} px={r['opaque_px']:6d} "
              f"err={r['mapping_err_mean']:5.2f}/{r['mapping_err_p95']:5.2f}/{r['mapping_err_max']:5.2f} "
              f"-> {os.path.basename(r['annot_png'])}")
    all_used = sorted({e["color_no"] for r in recs for e in r["colors"]})
    print(f"PALETTE={os.path.basename(a.palette)} size={len(pal)} "
          f"used={len(all_used)}/{len(pal)} unused={[i for i in range(len(pal)) if i not in all_used]}")
    if a.json_out:
        save_json({"palette": a.palette, "palette_size": int(len(pal)),
                   "used_color_numbers": all_used,
                   "unused": [i for i in range(len(pal)) if i not in all_used],
                   "items": [{k: v for k, v in r.items() if k != "colors"} | {"colors": r["colors"]}
                             for r in recs]}, a.json_out)
        print(f"SUMMARY={a.json_out}")


if __name__ == "__main__":
    main()
