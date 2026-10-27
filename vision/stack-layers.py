# stack-layers.py — 按顺序叠加多张 RGBA 层，落盘成一张合成图（用于"分层稿能否复现合成图"的独立验证）
# 顺序：先用 --layers 里靠后的覆盖靠前的？不 —— 语义为"自下而上"：列表第一个在最底层。
# 用法：python stack-layers.py --layers a.png,b.png,c.png --out comp_rebuilt.png
import os
import sys
import argparse

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _vlib import load_rgba  # noqa: E402


def stack(paths):
    base, _ = load_rgba(paths[0])
    out = base.copy()
    for p in paths[1:]:
        lay, _ = load_rgba(p)
        if lay.shape[:2] != out.shape[:2]:
            raise SystemExit(f"size mismatch: {p} {lay.shape[:2]} vs {out.shape[:2]}")
        m = lay[:, :, 3:4] > 0                     # 二值 alpha：非透明即覆盖
        out[:, :, :3] = np.where(m, lay[:, :, :3], out[:, :, :3])
        out[:, :, 3:4] = np.where(m, 255, out[:, :, 3:4])
    return out


def selftest():
    checks = []

    def ck(n, g, w):
        checks.append((n, g, w, g == w))
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest_tmp", "stack")
    os.makedirs(tmp, exist_ok=True)
    a = np.zeros((4, 4, 4), np.uint8); a[:, :, :3] = 10; a[:, :, 3] = 255
    b = np.zeros((4, 4, 4), np.uint8); b[:, :, :3] = 200; b[0:2, :, 3] = 255   # 只覆盖上半
    pa, pb = os.path.join(tmp, "a.png"), os.path.join(tmp, "b.png")
    Image.fromarray(a, "RGBA").save(pa)
    Image.fromarray(b, "RGBA").save(pb)
    out = stack([pa, pb])
    ck("stack.top_wins", int(out[0, 0, 0]), 200)
    ck("stack.base_kept", int(out[3, 3, 0]), 10)
    ck("stack.alpha", int(out[0, 0, 3]), 255)
    bad = [c for c in checks if not c[3]]
    for n, g, w, ok in checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {n}: got={g} want={w}")
    print(f"SELFTEST={'PASS' if not bad else 'FAIL'} checks={len(checks) - len(bad)}/{len(checks)}")
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--layers", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    paths = [p for p in a.layers.split(",") if p]
    if len(paths) < 2 or not a.out:
        ap.error("--layers a,b[,c] and --out required")
    out = stack(paths)
    Image.fromarray(out, "RGBA").save(a.out)
    print(f"OUT={a.out} size={out.shape[1]}x{out.shape[0]} layers={len(paths)}")


if __name__ == "__main__":
    main()
