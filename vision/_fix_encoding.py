# _fix_encoding.py — 修复被 PowerShell Set-Content 写成 ANSI/GBK 的 UTF-8 源码文件
# 用法: python _fix_encoding.py <file> [<file> ...]
# 只做还原：GBK -> UTF-8；若文件本来就是合法 UTF-8 则不动。
import sys


def fix(path):
    raw = open(path, "rb").read()
    try:
        raw.decode("utf-8")
        print(f"SKIP(already utf-8) {path}")
        return 0
    except UnicodeDecodeError:
        pass
    try:
        txt = raw.decode("gbk")
    except UnicodeDecodeError as e:
        print(f"FAIL(cannot decode as gbk) {path}: {e}")
        return 1
    if "\ufffd" in txt:
        print(f"WARN(replacement chars present) {path}")
    open(path, "w", encoding="utf-8", newline="").write(txt)
    open(path, "rb").read().decode("utf-8")
    print(f"FIXED {path} bytes {len(raw)} -> {len(txt.encode('utf-8'))}")
    return 0


if __name__ == "__main__":
    rc = 0
    for p in sys.argv[1:]:
        rc |= fix(p)
    sys.exit(rc)
