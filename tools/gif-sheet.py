# -*- coding: utf-8 -*-
"""gif-sheet.py —— 把教学 GIF 拆帧拼成一张联系表 PNG（用于"看图学画法"）

为什么需要：read_image 对 GIF 只给首帧，而 saint11 的教学图本身就是动画（第二帧才是要点）。
本工具用 Pillow 逐帧 seek 后取 RGBA 合成帧（GIF 的 disposal/局部帧交给 Pillow 处理），
统一缩放到 --cell 宽的整数倍（默认 1:1，便于数像素），加 2px 分隔线与帧号角标。

用法:
  python gif-sheet.py --gif x.gif --out x_sheet.png [--cols 6] [--cell 0] [--max 24] [--selftest]
  --cell 0 = 不缩放（1:1）；--max 只取前 N 帧（长动画教学图往往 60+ 帧）。
"""
import argparse
import os
import sys

from PIL import Image, ImageDraw


def frames_of(path, limit):
    im = Image.open(path)
    out = []
    for i in range(getattr(im, "n_frames", 1)):
        if limit and len(out) >= limit:
            break
        im.seek(i)
        out.append(im.convert("RGBA").copy())
    return out


def sheet(frames, cols, cell, gap=2, bg=(24, 24, 28, 255)):
    w, h = frames[0].size
    if cell:
        sc = cell / float(w)
        w, h = int(round(w * sc)), int(round(h * sc))
        frames = [f.resize((w, h), Image.NEAREST) for f in frames]
    cols = max(1, min(cols, len(frames)))
    rows = (len(frames) + cols - 1) // cols
    W = cols * w + (cols + 1) * gap
    H = rows * h + (rows + 1) * gap
    canvas = Image.new("RGBA", (W, H), bg)
    for i, f in enumerate(frames):
        r, c = divmod(i, cols)
        canvas.alpha_composite(f, (gap + c * (w + gap), gap + r * (h + gap)))
    return canvas


def selftest():
    """量尺自证：合成 4 帧 8x8，第 i 帧整幅填 (10*i,0,0)，断言联系表对应格子颜色。"""
    fr = []
    for i in range(4):
        im = Image.new("RGBA", (8, 8), (10 * i, 0, 0, 255))
        fr.append(im)
    s = sheet(fr, cols=2, cell=0, gap=2)
    assert s.size == (2 * 8 + 3 * 2, 2 * 8 + 3 * 2), s.size
    # 第 0 格左上角内一点
    assert s.getpixel((3, 3))[:3] == (0, 0, 0), s.getpixel((3, 3))
    # 第 3 格（右下）
    x = 2 + 8 + 2 + 3
    y = 2 + 8 + 2 + 3
    assert s.getpixel((x, y))[:3] == (30, 0, 0), s.getpixel((x, y))
    # 缩放路径也要能跑
    s2 = sheet(fr, cols=4, cell=16, gap=1)
    assert s2.size[0] == 4 * 16 + 5 * 1, s2.size
    print("selftest OK (4 assertions)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gif")
    ap.add_argument("--out")
    ap.add_argument("--cols", type=int, default=6)
    ap.add_argument("--cell", type=int, default=0)
    ap.add_argument("--max", type=int, default=24)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.gif or not a.out:
        ap.error("--gif/--out required")
    fr = frames_of(a.gif, a.max)
    if not fr:
        print("no frames", file=sys.stderr)
        return 1
    s = sheet(fr, a.cols, a.cell)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    s.save(a.out)
    print("frames=%d sheet=%dx%d -> %s" % (len(fr), s.size[0], s.size[1], a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
