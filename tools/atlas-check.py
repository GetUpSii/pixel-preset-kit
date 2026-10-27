#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""图集合成独立校验（atlas-pack.py 的配对闸门）。

    python atlas-check.py --index <outdir>/<out>.json
    python atlas-check.py --index <out>.json --src-dir <覆盖源目录>
    python atlas-check.py --json-out report.json --index <out>.json
    python atlas-check.py --selftest          # 先证明闸门能判负，再用它判产物

为什么单独一个文件、只吃产物：
    打包脚本自报"成功"不是证据（它可能贴错偏移、裁掉边、把两件叠在一起）。
    这里只读三样东西——索引 JSON、源件 PNG、合集 PNG——逐条独立复核：
      G1 非空      件数>0 且与索引 count 一致、id 不重复
      G2 画布      尺寸为 tile 整数倍
      G3 格子      x/y/gw/gh 全为 tile 整数倍，且 gw>=w、gh>=h
      G4 越界      x+gw<=W、y+gh<=H
      G5 重叠      任意两件的 gw×gh 占格矩形互不相交
      G6 逐件像素  合集对应区域与源件 RGBA **完全一致**（错位/裁切/缩放都会露）
      G7 杂散墨    占格矩形之外不得有任何非透明像素（贴错的残影会露）
      G8 选件集    与期望清单**逐 id 相等**（最能咬人的一条：G1~G7 全过，选件仍可能是错的）
退出码：0 = 全过；1 = 有 FAIL；2 = 用法/输入错误。
"""
import argparse
import importlib.util
import json
import os
import shutil
import sys


def _load_packer():
    """同目录的 atlas-pack.py 文件名含 '-'，不能直接 import。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "atlas-pack.py")
    spec = importlib.util.spec_from_file_location("atlas_pack", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rects_overlap(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)


def _load_expected(path):
    """期望清单：JSON 列表（[{"id":..}] 或 ["id",..]）或一份已有索引 json（取其 items[].id）。"""
    data = json.load(open(path, encoding="utf-8"))
    rows = data.get("items") if isinstance(data, dict) else data
    return [str(r.get("id") if isinstance(r, dict) else r) for r in (rows or [])]


def check(index_path, src_override="", expect_ids=None, expect_count=0):
    """返回 (ok, gates, meta)。gates = [{id, ok, detail}]"""
    from PIL import Image, ImageChops

    with open(index_path, encoding="utf-8") as f:
        idx = json.load(f)

    atlas_path = os.path.splitext(index_path)[0] + ".png"
    gates = []
    tile = int(idx.get("tile") or 0)
    items = idx.get("items") or []
    aw, ah = (idx.get("atlas") or [0, 0])[:2]
    src = src_override or idx.get("src") or ""

    # ---- G1 非空 ----------------------------------------------------------
    ids = [it.get("id") for it in items]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    ok1 = len(items) > 0 and tile > 0 and aw > 0 and ah > 0
    ok1 = ok1 and idx.get("count") == len(items) and not dup
    gates.append({"id": "G1.nonempty", "ok": ok1,
                  "detail": "items=%d count=%s tile=%d atlas=%dx%d dup_ids=%s" %
                            (len(items), idx.get("count"), tile, aw, ah, dup or "none")})
    if not items:
        return False, gates, {"atlas": atlas_path, "src": src}

    # ---- G2 画布 ----------------------------------------------------------
    ok2 = tile > 0 and aw % tile == 0 and ah % tile == 0
    gates.append({"id": "G2.canvas_tile_multiple", "ok": ok2,
                  "detail": "%dx%d tile=%d (both must be multiples)" % (aw, ah, tile)})

    # ---- G3 格子对齐 ------------------------------------------------------
    bad3 = []
    for it in items:
        r = {k: int(it.get(k) or 0) for k in ("x", "y", "w", "h", "gw", "gh")}
        if (r["x"] % tile or r["y"] % tile or r["gw"] % tile or r["gh"] % tile
                or r["gw"] < r["w"] or r["gh"] < r["h"] or r["w"] <= 0 or r["h"] <= 0):
            bad3.append(it.get("id"))
    ok3 = not bad3 and tile > 0
    gates.append({"id": "G3.grid_align", "ok": ok3,
                  "detail": "off-grid/miscounted items=%d %s" % (len(bad3), bad3[:6])})

    # ---- G4 越界 ----------------------------------------------------------
    bad4 = [it.get("id") for it in items
            if int(it.get("x") or 0) + int(it.get("gw") or 0) > aw
            or int(it.get("y") or 0) + int(it.get("gh") or 0) > ah]
    gates.append({"id": "G4.in_bounds", "ok": not bad4,
                  "detail": "out-of-canvas items=%d %s" % (len(bad4), bad4[:6])})

    # ---- G5 重叠 ----------------------------------------------------------
    ov = []
    boxes = [(int(it.get("x") or 0), int(it.get("y") or 0),
              int(it.get("gw") or 0), int(it.get("gh") or 0)) for it in items]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            if _rects_overlap(boxes[i], boxes[j]):
                ov.append("%s~%s" % (ids[i], ids[j]))
    gates.append({"id": "G5.no_overlap", "ok": not ov,
                  "detail": "overlapping pairs=%d %s" % (len(ov), ov[:4])})

    # ---- G8 选件集：先算好，报告时排在最后（只在给了期望时评；两侧非空才可比）
    g8 = None
    if expect_ids is not None or expect_count:
        got = set(ids)
        if expect_ids is not None:
            want = set(expect_ids)
            g8 = {"id": "G8.expected_set",
                  "ok": bool(want) and bool(got) and want == got,
                  "detail": "expected=%d got=%d missing=%s extra=%s" %
                            (len(want), len(got), sorted(want - got)[:8],
                             sorted(got - want)[:8])}
        else:
            g8 = {"id": "G8.expected_count", "ok": len(got) == expect_count,
                  "detail": "expected_count=%d got=%d" % (expect_count, len(got))}

    # ---- G6/G7 需要真的读图 ----------------------------------------------
    atlas_ok = os.path.exists(atlas_path)
    atlas = Image.open(atlas_path).convert("RGBA") if atlas_ok else None
    size_note = "ok"
    if atlas is not None and atlas.size != (aw, ah):
        size_note = "size %s != index %s" % (atlas.size, (aw, ah))
        atlas = None
        atlas_ok = False
    elif not atlas_ok:
        size_note = "MISSING"

    missing = []
    mismatched = []
    checked = 0
    if atlas_ok:
        for it in items:
            x, y, w, h = (int(it.get(k)) for k in ("x", "y", "w", "h"))
            rel = it.get("file")
            if not rel:
                rel = "%s%s" % (idx.get("id_prefix") or "", (it.get("id") or "") + ".png")
            sp = os.path.join(src, rel)
            if not os.path.exists(sp):
                missing.append(it.get("id"))
                continue
            src_im = Image.open(sp).convert("RGBA")
            if src_im.size != (w, h):
                mismatched.append("%s:size %s!=%s" % (it.get("id"), src_im.size, (w, h)))
                continue
            if ImageChops.difference(atlas.crop((x, y, x + w, y + h)), src_im).getbbox():
                mismatched.append(it.get("id"))
            checked += 1

    ok6 = atlas_ok and not missing and not mismatched and checked == len(items)
    gates.append({"id": "G6.pixel_identity", "ok": ok6,
                  "detail": "atlas=%s checked=%d/%d missing=%d %s mismatch=%d %s" %
                            (size_note, checked, len(items), len(missing), missing[:6],
                             len(mismatched), mismatched[:6])})
    if not atlas_ok:
        gates.append({"id": "G7.no_stray_ink", "ok": False,
                      "detail": "skipped: atlas PNG unusable"})
        if g8:
            gates.append(g8)
        return False, gates, {"atlas": atlas_path, "src": src}

    # ---- G7 占格之外的杂散墨 ---------------------------------------------
    mask = Image.new("L", (aw, ah), 0)
    for it in items:
        x, y = int(it.get("x")), int(it.get("y"))
        mask.paste(255, (x, y, x + int(it.get("gw")), y + int(it.get("gh"))))
    outside = ImageChops.multiply(atlas.getchannel("A"), ImageChops.invert(mask))
    stray = sum(outside.histogram()[1:])
    gates.append({"id": "G7.no_stray_ink", "ok": stray == 0,
                  "detail": "opaque px outside all grid boxes=%d" % stray})
    if g8:
        gates.append(g8)

    return all(g["ok"] for g in gates), gates, {"atlas": atlas_path, "src": src,
                                                "size": [aw, ah], "items": len(items)}


# --------------------------------------------------------------------------
def selftest(workdir=""):
    """先证明这把尺子能判负：好产物必过，四类坏产物必挂。

    夹具体一律落在工作区内（沙箱下 %TEMP% 可能拒写），跑完保留，便于事后复核。
    """
    from PIL import Image

    tmp = os.path.abspath(workdir or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "_probe", "atlas-tmp"))
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        os.makedirs(tmp, exist_ok=True)
        src = os.path.join(tmp, "src")
        os.makedirs(src)
        a = Image.new("RGBA", (20, 10), (0, 0, 0, 0))
        for i in range(20):
            a.putpixel((i, i % 10), (255, 0, 0, 255))
        a.save(os.path.join(src, "alpha_item.png"))
        b = Image.new("RGBA", (8, 8), (0, 0, 255, 255))
        b.save(os.path.join(src, "beta_item.png"))
        # 一件更高的，撑出底下/右侧的空白行——否则占格铺满画布，G7 永远无从触发
        c = Image.new("RGBA", (20, 20), (0, 255, 255, 255))
        c.save(os.path.join(src, "gamma_item.png"))

        packer = _load_packer()
        packer.pack(src, "*.png", 16, 64, tmp, "good", lua=False)
        good = os.path.join(tmp, "good.json")

        steps = []
        ok, gates, _ = check(good)
        steps.append(("good passes", ok, [g["id"] for g in gates if not g["ok"]]))

        def corrupt(tag, mutate, expect_gate):
            d = json.load(open(good, encoding="utf-8"))
            mutate(d)
            ip = os.path.join(tmp, tag + ".json")
            json.dump(d, open(ip, "w", encoding="utf-8"))
            shutil.copy(os.path.splitext(good)[0] + ".png", os.path.splitext(ip)[0] + ".png")
            ok2, g2, _ = check(ip)
            failed = [g["id"] for g in g2 if not g["ok"]]
            hit = (not ok2) and any(expect_gate in f for f in failed)
            steps.append(("%s must fail via %s" % (tag, expect_gate), hit, failed))

        corrupt("shift", lambda d: d["items"][0].__setitem__("y", d["items"][0]["y"] + 1),
                "G3")
        corrupt("overlap", lambda d: d["items"][1].__setitem__("x", d["items"][0]["x"]),
                "G5")
        corrupt("oob", lambda d: d["items"][0].__setitem__("x", d["atlas"][0]),
                "G4")

        # 像素级坏：把某件区域涂空（索引没改，图变了）
        d = json.load(open(good, encoding="utf-8"))
        ip = os.path.join(tmp, "ghost.json")
        json.dump(d, open(ip, "w", encoding="utf-8"))
        im = Image.open(os.path.splitext(good)[0] + ".png").convert("RGBA")
        it = d["items"][0]
        im.paste((0, 0, 0, 0), (it["x"], it["y"], it["x"] + it["w"], it["y"] + it["h"]))
        # 找一格之外的空白像素（找不到说明夹具无意义，必须显式失败）
        free = next(((xx, yy)
                     for yy in range(d["atlas"][1]) for xx in range(d["atlas"][0])
                     if not any(q["x"] <= xx < q["x"] + q["gw"]
                                and q["y"] <= yy < q["y"] + q["gh"] for q in d["items"])), None)
        if free is None:
            print("SELFTEST=FAIL  fixture has no pixel outside grid boxes")
            return 1
        im.putpixel(free, (0, 255, 0, 255))
        im.save(os.path.splitext(ip)[0] + ".png")
        ok3, g3, _ = check(ip)
        failed = [g["id"] for g in g3 if not g["ok"]]
        steps.append(("ghost+stray must fail via G6/G7",
                      (not ok3) and "G6.pixel_identity" in failed and "G7.no_stray_ink" in failed,
                      failed))

        # 选件集错：G1~G7 可以全过（合集忠实复制了"错的那批件"），只有 G8 咬得住。
        # 这不是假想——farm02 首次复现时 hand_*.png 混进 6 张质检图，7 条闸门全绿。
        ids_all = [q["id"] for q in json.load(open(good, encoding="utf-8"))["items"]]
        ok8a, _, _ = check(good, expect_ids=ids_all)
        ok8b, g8b, _ = check(good, expect_ids=ids_all[:-1])
        f8b = [g["id"] for g in g8b if not g["ok"]]
        steps.append(("expected set exactly right -> pass", ok8a, []))
        steps.append(("missing one expected id must fail via G8",
                      (not ok8b) and "G8.expected_set" in f8b, f8b))

        bad = [s for s in steps if not s[1]]
        for name, good_, detail in steps:
            print("  %-38s ok=%-5s %s" % (name, good_, "" if good_ else detail))
        print("SELFTEST=" + ("PASS" if not bad else "FAIL") + "  fixtures=" + tmp)
        return 0 if not bad else 1
    except Exception as err:  # 夹具都建不起来时，尺子本身就是坏的：明确报失败，别静默
        print("SELFTEST=FAIL  fixtures=%s  error=%s" % (tmp, err))
        return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", help="atlas-pack.py 产出的 .json 索引")
    ap.add_argument("--src-dir", default="", help="覆盖索引里的 src（目录搬过时用）")
    ap.add_argument("--expect-ids", default="",
                    help="期望件清单（JSON 列表，或直接给一份已有索引 .json 取其 id）")
    ap.add_argument("--expect-count", type=int, default=0, help="只校验件数")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--workdir", default="", help="自检夹具目录（默认 <tools>/../_probe/atlas-tmp）")
    a = ap.parse_args()
    if a.selftest:
        return selftest(a.workdir)
    if not a.index:
        print("need --index <atlas.json> or --selftest")
        return 2
    if not os.path.exists(a.index):
        print("index not found: %s" % a.index)
        return 2

    expect = _load_expected(a.expect_ids) if a.expect_ids else None
    if expect is not None and not expect:
        print("expected ids file is empty (or has no ids): %s" % a.expect_ids)
        return 2
    ok, gates, meta = check(a.index, a.src_dir, expect, a.expect_count)
    for g in gates:
        print("%-24s %-4s %s" % (g["id"], "PASS" if g["ok"] else "FAIL", g["detail"]))
    print("VERDICT=%s  %s" % ("PASS" if ok else "FAIL", json.dumps(meta, ensure_ascii=False)))
    if a.json_out:
        json.dump({"verdict": "PASS" if ok else "FAIL", "gates": gates, "meta": meta},
                  open(a.json_out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
