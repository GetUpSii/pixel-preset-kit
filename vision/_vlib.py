# _vlib.py — 视觉回路原语共享库（P0：看图 -> 量化差异 -> 精确定位）
#
# 设计约束（本机实测环境）：
#   * 只依赖 numpy + Pillow，**不用 scipy / torch / onnx**（本机没有，也不下载模型）；
#     连通域自己实现（扫描线 run + 并查集），白边分层用 4 邻域膨胀，都是纯 numpy。
#   * 颜色距离一律 int32：`int16` 下 255**2 = 65025 溢出（上限 32767）-> 负 -> sqrt 出 NaN，
#     这坑已实测过一次（见 PIXEL-PIPELINE.md「调色板/数值」）。
#   * 不打印中文：宿主 pwsh 是 PS5.1 + OEM936，中文 stdout 会乱码；报告走 UTF-8 JSON 文件，
#     stdout 只出 ASCII 摘要（可被 agent 安全读回）。
#
# 语义约定：像素坐标一律 (x=列, y=行)，bbox 用**闭区间** [x0, y0, x1, y1]（与 numpy 切片不同）。
import json
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- 基础 IO

def load_rgba(path):
    """读任意图 -> (uint8 HxWx4, (w,h))。带 alpha 的原样，不带的全 255。"""
    im = Image.open(path)
    size = im.size
    return np.asarray(im.convert("RGBA"), dtype=np.uint8), size


def save_rgba(arr, path):
    Image.fromarray(np.asarray(arr, dtype=np.uint8), "RGBA").save(path)


def save_rgb(arr, path):
    Image.fromarray(np.asarray(arr, dtype=np.uint8), "RGB").save(path)


def save_json(obj, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    return path


def write_lines(lines, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))
    return path


def hexc(rgb):
    return "#%02x%02x%02x" % (int(rgb[0]), int(rgb[1]), int(rgb[2]))


def parse_hex(s):
    s = s.strip().lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


# ---------------------------------------------------------------- 颜色距离（int32，分块防爆内存）

def nearest_index(px, pal, chunk=None):
    """px (N,3) / pal (M,3) -> (idx (N,) int64, d2 (N,) int32)。分块算，避免 N*M*3 爆内存。

    chunk 自动按调色板大小收缩（目标每块 <= ~8M 个 int，约 32MB）；2K 图 + 64 色也安全。
    """
    px = np.asarray(px, dtype=np.int32)
    pal = np.asarray(pal, dtype=np.int32)
    if px.size == 0:
        return np.zeros((0,), np.int64), np.zeros((0,), np.int32)
    if chunk is None:
        chunk = int(max(256, min(65536, 8_000_000 // max(1, len(pal) * 3))))
    idx = np.empty(len(px), np.int64)
    d2 = np.empty(len(px), np.int32)
    for s in range(0, len(px), chunk):
        e = min(len(px), s + chunk)
        diff = px[s:e, None, :] - pal[None, :, :]           # int32 (n,M,3)
        dist = np.einsum("nmc,nmc->nm", diff, diff, dtype=np.int32)
        j = dist.argmin(1)
        idx[s:e] = j
        d2[s:e] = dist[np.arange(e - s), j]
    return idx, d2


def nearest_error(px, pal, chunk=None):
    """欧氏距离（不是平方），0..441 尺度，与 PIXEL-PIPELINE.md 的报告口径一致。"""
    _, d2 = nearest_index(px, pal, chunk)
    return np.sqrt(d2.astype(np.float64))


def err_stats(err):
    if len(err) == 0:
        return {"n": 0}
    return {
        "n": int(len(err)),
        "mean": round(float(err.mean()), 2),
        "p95": round(float(np.percentile(err, 95)), 2),
        "p99": round(float(np.percentile(err, 99)), 2),
        "max": round(float(err.max()), 2),
    }


# ---------------------------------------------------------------- 连通域（扫描线 run + 并查集，无 scipy）

def label_mask(mask, conn=8):
    """布尔 mask -> (labels int32 HxW，0=背景，id 按光栅序 1..n), n。

    conn=8 用「与上一行 run 重叠 ±1」判连通；conn=4 要求严格列重叠。
    单像素噪点与稀疏区域都正确；30 万像素级 mask 在毫秒级完成。
    """
    mask = np.ascontiguousarray(mask, dtype=bool)
    H, W = mask.shape
    if H == 0 or W == 0:
        return np.zeros((H, W), np.int32), 0

    parent = []

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:                     # 根取较小下标 -> 结果确定、可复现
            parent[max(ra, rb)] = min(ra, rb)

    pad = 1 if conn == 8 else 0
    rows = []
    prev = []
    for y in range(H):
        row = mask[y]
        cur = []
        if row.any():
            p = np.zeros(W + 2, np.int8)
            p[1:-1] = row
            d = np.diff(p)
            starts = np.flatnonzero(d == 1)
            ends = np.flatnonzero(d == -1) - 1
            for s, e in zip(starts.tolist(), ends.tolist()):
                idx = len(parent)
                parent.append(idx)
                cur.append((s, e, idx))
            if prev:
                for (s, e, idx) in cur:
                    for (ps, pe, pix) in prev:
                        if s - pad <= pe and ps <= e + pad:
                            union(idx, pix)
        rows.append(cur)
        prev = cur

    labels = np.zeros((H, W), np.int32)
    root2id = {}
    for y, cur in enumerate(rows):
        for (s, e, idx) in cur:
            r = find(idx)
            lid = root2id.get(r)
            if lid is None:
                lid = len(root2id) + 1
                root2id[r] = lid
            labels[y, s:e + 1] = lid
    return labels, len(root2id)


def dilate4(m):
    p = np.pad(m, 1, constant_values=False)
    return p[1:-1, 1:-1] | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]


def edge_ring(mask):
    """4 邻域边缘环：区域内「有邻居不在区域内」的像素。10x10 实心块 = 36 px（周长，可断言）。"""
    p = np.pad(mask, 1, constant_values=False)
    core = p[1:-1, 1:-1] & p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:]
    return mask & ~core


_DIRS8 = [(-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1)]  # N,NE,E,SE,S,SW,W,NW


def trace_contour(mask):
    """Moore 邻域追踪：返回 8 连通有序外轮廓坐标 [(x,y), ...]（best-effort，闭合多边形）。

    最坏情况（薄结构/多孔洞）只保证给出外圈；要"逐个边缘像素"的严格集合请用 edge_ring()。
    """
    m = np.asarray(mask, dtype=bool)
    ys, xs = np.nonzero(m)
    if len(ys) == 0:
        return []
    H, W = m.shape
    y0 = int(ys.min())
    x0 = int(xs[ys == y0].min())
    start = (y0, x0)

    def inside(y, x):
        return 0 <= y < H and 0 <= x < W and m[y, x]

    contour = [start]
    cur = start
    b = (cur[0], cur[1] - 1)                     # 最左上像素的西侧必在区域外
    guard = 0
    limit = 8 * int(m.sum()) + 64
    while guard < limit:
        guard += 1
        di = None
        for k, (dy, dx) in enumerate(_DIRS8):
            if (cur[0] + dy, cur[1] + dx) == b:
                di = k
                break
        if di is None:                            # 回溯点异常：退化为取第一个邻居
            di = 0
        found = None
        prev_c = b
        for k in range(1, 9):
            c = (cur[0] + _DIRS8[(di + k) % 8][0], cur[1] + _DIRS8[(di + k) % 8][1])
            if inside(*c):
                found = c
                break
            prev_c = c
        if found is None:                         # 孤立像素
            break
        b = prev_c
        cur = found
        if cur == start and len(contour) > 1:     # 回到起点且不是第一步 -> 闭合
            break
        contour.append(cur)
    return [(int(x), int(y)) for (y, x) in contour]


# ---------------------------------------------------------------- 区域统计（面积 / bbox / 质心 / 边界）

def region_records(mask, conn=8, weight=None, min_area=1, max_regions=2000):
    """逐连通域统计。weight: 与 mask 同形状的数值图（如差异幅度），用于 mean/max。"""
    labels, n = label_mask(mask, conn)
    if n == 0:
        return []
    ys, xs = np.nonzero(labels)
    lab = labels[ys, xs]
    area = np.bincount(lab, minlength=n + 1)
    sx = np.bincount(lab, weights=xs.astype(np.float64), minlength=n + 1)
    sy = np.bincount(lab, weights=ys.astype(np.float64), minlength=n + 1)
    BIG = 1 << 30
    xmin = np.full(n + 1, BIG, np.int64)
    xmax = np.full(n + 1, -1, np.int64)
    ymin = np.full(n + 1, BIG, np.int64)
    ymax = np.full(n + 1, -1, np.int64)
    np.minimum.at(xmin, lab, xs)
    np.maximum.at(xmax, lab, xs)
    np.minimum.at(ymin, lab, ys)
    np.maximum.at(ymax, lab, ys)
    if weight is not None:
        w = np.asarray(weight, dtype=np.float64)[ys, xs]
        wsum = np.bincount(lab, weights=w, minlength=n + 1)
        wmax = np.zeros(n + 1, np.float64)
        np.maximum.at(wmax, lab, w)

    recs = []
    for i in range(1, n + 1):
        a = int(area[i])
        if a < min_area or a == 0:
            continue
        rec = {
            "id": i,
            "area": a,
            "bbox": [int(xmin[i]), int(ymin[i]), int(xmax[i]), int(ymax[i])],
            "w": int(xmax[i] - xmin[i] + 1),
            "h": int(ymax[i] - ymin[i] + 1),
            "centroid": [round(float(sx[i] / a), 2), round(float(sy[i] / a), 2)],
        }
        if weight is not None:
            rec["weight_mean"] = round(float(wsum[i] / a), 2)
            rec["weight_max"] = round(float(wmax[i]), 2)
        recs.append(rec)
    recs.sort(key=lambda r: -r["area"])
    if len(recs) > max_regions:
        recs = recs[:max_regions]
    return recs


def mask_from_alpha(rgba, thr=127):
    return np.asarray(rgba)[:, :, 3] > thr


def mask_from_bg(rgb, bg, t0, t1, strong=None):
    """纯色背景软阈值抠图（cut-props.py 的同一口径）。返回 (soft_alpha_mask, strong_mask)。"""
    a = np.asarray(rgb, dtype=np.int32)
    d = np.sqrt(((a - np.asarray(bg, np.int32)) ** 2).sum(-1).astype(np.int32))
    return d, (d >= t1 if strong is None else d > strong)


def estimate_bg_ring(img, px=6):
    """用图四边内 px 像素的一圈中位数估背景色（**每格用自己的环**，别用全图边缘——暗角会把裁切框撑满）。"""
    a = np.asarray(img, dtype=np.float32)
    if a.ndim == 2:
        a = np.dstack([a] * 3)
    a = a[:, :, :3]
    ring = np.concatenate([a[0:px].reshape(-1, 3), a[-px:].reshape(-1, 3),
                           a[:, 0:px].reshape(-1, 3), a[:, -px:].reshape(-1, 3)])
    return np.round(np.median(ring, axis=0), 1)


# ---------------------------------------------------------------- 白边（外侧腐蚀）测厚

def distance_bands(opaque, maxd=6):
    """band[d] = 距透明区恰好 d 层的区域像素（d 从 1 = 最外层）。与 defringe-props.py 同口径。"""
    grown = ~opaque
    prev = ~opaque
    bands = {}
    for d in range(1, maxd + 1):
        grown = dilate4(grown)
        band = opaque & grown & ~prev
        if not band.any():
            break
        bands[d] = band
        prev = grown
    return bands


def fringe_thickness(opaque, rgb, maxd=6, near_white=232, rule=0.5):
    """白边厚度 = 从最外层起连续「近白占比 > rule」的层数；腐蚀不会碰到内部。

    rgb: HxWx3（int/uint）。返回 (thickness, per_layer_fraction, layer_counts)。
    """
    mn = np.asarray(rgb)[:, :, :3].min(-1)
    near = opaque & (mn > near_white)
    bands = distance_bands(opaque, maxd)
    frac, counts = {}, {}
    for d, band in bands.items():
        c = int(band.sum())
        counts[d] = c
        frac[d] = round(float((band & near).sum()) / c, 3) if c else 0.0
    th = 0
    for d in range(1, maxd + 1):
        if frac.get(d, 0) > rule:
            th = d
        else:
            break
    return th, frac, counts


def erode_outer(opaque, thickness):
    """按厚度腐蚀最外 thickness 层（内部不动）。返回新 opaque。"""
    if thickness <= 0:
        return opaque.copy()
    bands = distance_bands(opaque, thickness)
    kill = np.zeros_like(opaque)
    for d in range(1, thickness + 1):
        kill |= bands.get(d, np.zeros_like(opaque))
    return opaque & ~kill


# ---------------------------------------------------------------- 图差（image-diff 内核）

def diff_maps(a, b, metric="rgb", include_alpha=False):
    """两图差 -> int32 HxW 幅度图（0..255）× 逐通道图。尺寸必须一致。"""
    a = np.asarray(a, dtype=np.int32)
    b = np.asarray(b, dtype=np.int32)
    if a.shape != b.shape:
        raise ValueError("size/shape mismatch: %s vs %s" % (a.shape, b.shape))
    if metric == "luma":
        wa = np.array([0.299, 0.587, 0.114])
        la = np.round(a[:, :, :3] @ wa)
        lb = np.round(b[:, :, :3] @ wa)
        d = np.abs(la - lb).astype(np.int32)
    elif metric == "rgb":
        d = np.abs(a[:, :, :3] - b[:, :, :3]).max(-1).astype(np.int32)
    else:
        raise ValueError("unknown metric: %r" % (metric,))
    if include_alpha:
        d = np.maximum(d, np.abs(a[:, :, 3] - b[:, :, 3])).astype(np.int32)
    return d


_HEAT_STOPS = np.array([0, 32, 96, 176, 255], np.float64)
_HEAT_COLS = np.array([[12, 12, 18], [40, 60, 140], [0, 180, 230], [255, 215, 0], [255, 45, 45]], np.float64)


def heat_rgb(d):
    v = np.clip(np.asarray(d, dtype=np.float64), 0, 255)
    return np.stack([np.interp(v, _HEAT_STOPS, _HEAT_COLS[:, c]) for c in range(3)], -1).astype(np.uint8)


def overlay_rgb(base_rgba, d, thr):
    """把差异烧到压暗的底图上：未变=暗底，越过阈值的像素=热力图颜色。"""
    base = np.asarray(base_rgba, dtype=np.float64)[:, :, :3]
    out = base * 0.35 + 18.0
    m = np.asarray(d) >= thr
    if m.any():
        out[m] = heat_rgb(np.asarray(d)[m]).astype(np.float64)
    return np.clip(out, 0, 255).astype(np.uint8)


def roi_slice(spec, w, h):
    """'x0,y0,x1,y1' -> (slice_y, slice_x)，闭区间；None/'' = 全图。"""
    if not spec:
        return slice(0, h), slice(0, w)
    p = [int(v) for v in str(spec).replace(" ", "").split(",")]
    if len(p) != 4:
        raise ValueError("--roi needs x0,y0,x1,y1")
    x0, y0, x1, y1 = p
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w - 1, x1), min(h - 1, y1)
    return slice(y0, y1 + 1), slice(x0, x1 + 1)


# ---------------------------------------------------------------- 调色板

def read_gpl(path):
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.split()
            if len(p) >= 3 and p[0].lstrip("-").isdigit():
                out.append([int(p[0]), int(p[1]), int(p[2])])
    return np.array(out, dtype=np.int32).reshape(-1, 3)


def write_gpl(pal, path, name="palette", tags=None):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="ascii", newline="\n") as f:
        f.write("GIMP Palette\nName: %s\nColumns: 8\n#\n" % name)
        for i, c in enumerate(pal):
            tag = (tags[i] if tags and i < len(tags) else "c-%02d" % i)
            f.write("%3d %3d %3d\t%s\n" % (int(c[0]), int(c[1]), int(c[2]), tag))
    return path


def median_cut(px, colors):
    """px (N,3) -> (n,3) uint8 调色板。用 PIL 的 MEDIANCUT（无 scipy 依赖）。

    做法同 prop-pack.py：把像素铺成 64 列宽的临时图交给 PIL（PIL 不能直接吃像素数组）。
    """
    px = np.asarray(px, dtype=np.uint8).reshape(-1, 3)
    if len(px) == 0:
        return np.zeros((0, 3), np.uint8)
    colors = int(max(2, min(256, colors)))
    rows = int(np.ceil(len(px) / 64))
    pad = rows * 64 - len(px)
    grid = np.vstack([px, np.tile(px[:1], (pad, 1))]).reshape(rows, 64, 3)
    q = Image.fromarray(grid, "RGB").quantize(colors=colors, method=Image.MEDIANCUT, dither=Image.NONE)
    pal = np.array(q.getpalette()[: colors * 3], dtype=np.uint8)
    return pal.reshape(-1, 3)


def unique_colors(rgb, counts_top=24):
    """-> (unique_n, top list of {hex,rgb,count,share})。"""
    px = np.asarray(rgb, dtype=np.uint8).reshape(-1, 3)
    if len(px) == 0:
        return 0, []
    packed = (px[:, 0].astype(np.int64) << 16) | (px[:, 1].astype(np.int64) << 8) | px[:, 2]
    uniq, cnt = np.unique(packed, return_counts=True)
    order = np.argsort(-cnt)[:counts_top]
    top = []
    for i in order:
        v = int(uniq[i])
        top.append({"hex": hexc(((v >> 16) & 255, (v >> 8) & 255, v & 255)),
                    "rgb": [(v >> 16) & 255, (v >> 8) & 255, v & 255],
                    "count": int(cnt[i]), "share": round(float(cnt[i]) / len(px), 4)})
    return int(len(uniq)), top


def channel_report(rgb, bins=16):
    px = np.asarray(rgb, dtype=np.uint8).reshape(-1, 3).astype(np.float64)
    if len(px) == 0:
        return {}
    out = {}
    for i, nm in enumerate("rgb"):
        col = px[:, i]
        hist, _ = np.histogram(col, bins=bins, range=(0, 255))
        out[nm] = {"min": int(col.min()), "max": int(col.max()), "mean": round(float(col.mean()), 1),
                   "p5": round(float(np.percentile(col, 5)), 1), "p95": round(float(np.percentile(col, 95)), 1),
                   "hist": [int(v) for v in hist]}
    return out


def visible_pixels(rgba, alpha_thr=127):
    a = np.asarray(rgba)
    m = a[:, :, 3] > alpha_thr
    return a[:, :, :3][m]


def _font(size=14):
    for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\consola.ttf", r"C:\Windows\Fonts\arial.ttf"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def swatch_sheet(pal, path, cell=34, cols=16, title=None, tags=None):
    """调色板色卡（带 hex 标签）+ 可选来源标注。"""
    n = len(pal)
    cols = max(1, min(cols, n if n else 1))
    rows = int(np.ceil(n / cols)) if n else 1
    lab = 14
    top = 22 if title else 0
    W = cols * cell
    H = rows * (cell + lab) + top
    im = Image.new("RGB", (W, H), (28, 28, 32))
    dr = ImageDraw.Draw(im)
    f = _font(11)
    if title:
        dr.text((4, 4), title, fill=(235, 235, 240), font=_font(13))
    for i, c in enumerate(pal):
        cx = (i % cols) * cell
        cy = top + (i // cols) * (cell + lab)
        dr.rectangle([cx + 1, cy + 1, cx + cell - 2, cy + cell - 2],
                     fill=(int(c[0]), int(c[1]), int(c[2])), outline=(70, 70, 78))
        dr.text((cx + 2, cy + cell), hexc(c)[1:], fill=(210, 210, 215), font=f)
    im.save(path)
    return path


def checker_overlay(rgba, path, cell=8):
    """棋盘底贴图：肉眼验收 alpha 切口干净（cut-props.py 的同一手法）。"""
    a = np.asarray(rgba, dtype=np.uint8)
    H, W = a.shape[:2]
    bg = np.zeros((H, W, 3), np.uint8)
    yy, xx = np.mgrid[0:H, 0:W]
    chk = (((yy // cell) + (xx // cell)) % 2 == 0)
    bg[chk] = (72, 72, 80)
    bg[~chk] = (48, 48, 56)
    al = (a[:, :, 3:4].astype(np.float64)) / 255.0
    out = (a[:, :, :3].astype(np.float64) * al + bg.astype(np.float64) * (1 - al))
    save_rgb(np.clip(out, 0, 255).astype(np.uint8), path)
    return path


def zoom_nearest(path_in, path_out, factor=8):
    im = Image.open(path_in).convert("RGBA")
    im.resize((im.width * factor, im.height * factor), Image.NEAREST).save(path_out)
    return path_out


def fingerprint_rgb(rgb):
    """跨语言渲染指纹（与 README / build-scene.lua 同一公式）：行优先，
    h = (h*131 + r*65536 + g*256 + b) mod 2147483647。用于"这份产物是不是我以为的那份"。"""
    a = np.asarray(rgb)
    if a.ndim == 2:
        a = np.dstack([a, a, a])
    v = (a[:, :, 0].astype(np.int64) << 16) | (a[:, :, 1].astype(np.int64) << 8) | a[:, :, 2].astype(np.int64)
    h = 0
    for x in v.ravel().tolist():
        h = (h * 131 + x) % 2147483647
    return int(h)


def default_out_dir(sub="vision"):
    here = os.path.dirname(os.path.abspath(__file__))
    base = os.path.join(os.path.dirname(here), "out", sub)
    os.makedirs(base, exist_ok=True)
    return base
