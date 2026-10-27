# light-direction.py — 从图里**反推光源方向**（不假设、不猜）
# 原理：亮度的一阶矩。把 CIE L* 平滑后，求"亮度质心 - 形状质心"的向量 => 指向亮侧 = 光源来向。
#   对半球形物体（锅/蘑菇）与整幅场景都有效；对纯平色（无明暗）应当**报不出方向**（有负对照断言）。
# 输出：方位角（罗盘制：0=北/上，90=东/右，180=南/下，270=西/左，指"光从哪来"）+ 置信度 + 建议的 L 向量。
# 用法：python light-direction.py --image A.png [--json-out J]  |  --glob "*.png"  |  --selftest
import os
import sys
import glob
import math
import argparse
import importlib.util

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _vlib import load_rgba, save_json  # noqa: E402


def _mod(name):
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").replace(".py", ""), p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


PF = _mod("palette-forge.py")
RS = _mod("_lightlib.py")   # 光影共享原语（原 relight-split.py 已退役，函数迁至 _lightlib）


def compass(az):
    names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return names[int(((az % 360) + 22.5) // 45) % 8]


def light_direction(path, blur=3):
    """返回 {azimuth_deg, confidence, compass, light_vec}；confidence 为 0 时表示"测不出方向"。"""
    rgba, _ = load_rgba(path)
    opaque = rgba[:, :, 3] > 127
    rgb = rgba[:, :, :3].astype(np.int32)
    n = int(opaque.sum())
    if n < 16:
        return None
    LabL = PF.srgb_to_lab(rgb.reshape(-1, 3))[:, 0].reshape(rgb.shape[:2])
    Ls = RS.box_blur(np.where(opaque, LabL, 0.0), blur, passes=2)
    # 只在形状内部统计（模糊会把边界外的 0 拉进来，故按 mask 归一化）
    w = RS.box_blur(opaque.astype(np.float64), blur, passes=2)
    Ls = np.where(w > 1e-6, Ls / np.maximum(w, 1e-6), 0.0)

    ys, xs = np.nonzero(opaque)
    cy, cx = float(ys.mean()), float(xs.mean())
    dev = Ls[opaque] - float(Ls[opaque].mean())
    dx = float(((xs - cx) * dev).sum())
    dy = float(((ys - cy) * dev).sum())
    r_mean = float(np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2).mean()) + 1e-9
    conf = float(np.hypot(dx, dy) / (np.abs(dev).sum() * r_mean + 1e-9))
    az = math.degrees(math.atan2(dx, -dy)) % 360.0        # 罗盘制：0=N(上), 90=E(右)
    # 光"从"该方位来 => 指向光的单位向量（屏幕坐标 y 向下）
    lx = math.sin(math.radians(az))
    ly = -math.cos(math.radians(az))
    return {"image": path, "azimuth_deg": round(az, 1), "compass": compass(az),
            "confidence": round(conf, 4), "opaque_px": n,
            "light_vec": [round(lx, 4), round(ly, 4)]}


def selftest():
    import numpy as np
    from PIL import Image
    checks = []

    def ck(name, got, want):
        checks.append((name, got, want, got == want))

    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest_tmp", "lightdir")
    os.makedirs(tmp, exist_ok=True)

    def synth(az_deg, uniform=False):
        S = 64
        yy, xx = np.mgrid[0:S, 0:S]
        m = (yy - 32) ** 2 + (xx - 32) ** 2 <= 26 ** 2
        a = math.radians(az_deg)
        gx, gy = math.sin(a), -math.cos(a)              # 指向光的向量
        val = 120 + (0 if uniform else 70 * (gx * (xx - 32) / 26 + gy * (yy - 32) / 26))
        img = np.zeros((S, S, 4), np.uint8)
        img[:, :, :3] = np.clip(val, 0, 255)[..., None]
        img[:, :, 3] = np.where(m, 255, 0)
        p = os.path.join(tmp, f"lit_{az_deg}{'_flat' if uniform else ''}.png")
        Image.fromarray(img, "RGBA").save(p)
        return p

    for az in (315, 45, 135, 225):
        r = light_direction(synth(az))
        d = min(abs(r["azimuth_deg"] - az), 360 - abs(r["azimuth_deg"] - az))
        ck(f"recover_{az}", d < 12.0, True)
        ck(f"conf_{az}_gt0", r["confidence"] > 0.1, True)

    flat = light_direction(synth(315, uniform=True))
    ck("negative_uniform_lowconf", flat["confidence"] < 0.05, True)   # 负对照：无明暗就该报不出方向

    bad = [c for c in checks if not c[3]]
    for n, g, w, ok in checks:
        print(f"  [{'ok' if ok else 'FAIL'}] {n}: got={g} want={w}")
    print(f"SELFTEST={'PASS' if not bad else 'FAIL'} checks={len(checks) - len(bad)}/{len(checks)}")
    return 0 if not bad else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="")
    ap.add_argument("--glob", default="")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    targets = ([a.image] if a.image else []) + [p for pat in a.glob.split(";") if pat
                                               for p in sorted(glob.glob(pat))]
    if not targets:
        ap.error("--image or --glob required")
    rows = [r for r in (light_direction(t) for t in targets) if r]
    print(f"{'image':34s} {'azimuth':>8s} {'dir':>3s} {'conf':>7s}  light_vec")
    for r in rows:
        print(f"{os.path.basename(r['image'])[:34]:34s} {r['azimuth_deg']:7.1f}° {r['compass']:>3s} "
              f"{r['confidence']:7.3f}  {r['light_vec']}")
    strong = [r for r in rows if r["confidence"] >= 0.10]
    if strong:
        vx = np.mean([r["light_vec"][0] for r in strong])
        vy = np.mean([r["light_vec"][1] for r in strong])
        az = math.degrees(math.atan2(vx, -vy)) % 360
        spread = [min(abs(r["azimuth_deg"] - az), 360 - abs(r["azimuth_deg"] - az)) for r in strong]
        print(f"MEAN(conf>=0.10) azimuth={az:.1f}° ({compass(az)}) n={len(strong)} "
              f"median_dev={np.median(spread):.1f}° max_dev={max(spread):.1f}°")
    if a.json_out:
        save_json({"rows": rows}, a.json_out)
        print(f"JSON={a.json_out}")


if __name__ == "__main__":
    main()
