"""生成 app.ico（纯标准库，无需 PIL）：暗夜金圆角方 + 深色菱形，与软件 favicon 同风格。

多尺寸 32 位带 alpha（16/32/48/256），Windows 快捷方式/任务栏各档都清晰。
运行：python installer/make_icon.py  →  installer/app.ico
"""

from __future__ import annotations

import struct
from pathlib import Path

GOLD = (0x63, 0xB9, 0xE4)   # BGR of #E4B963
DARK = (0x07, 0x1A, 0x22)   # BGR of #221A07
SIZES = (256, 48, 32, 16)


def _pixels(n: int) -> bytes:
    """返回 n×n 的 BGRA 像素（自上而下），圆角方底金、居中深色菱形。"""
    rad = n * 0.22          # 圆角半径
    dia = n * 0.36          # 菱形半径
    cx = cy = (n - 1) / 2.0
    rows = []
    for y in range(n):
        row = bytearray()
        for x in range(n):
            # 圆角方形透明遮罩
            inside = True
            for ox, oy in ((rad, rad), (n - rad, rad), (rad, n - rad), (n - rad, n - rad)):
                # 只在四角区域做圆角判定
                if ((x < rad and y < rad) or (x > n - rad and y < rad) or
                        (x < rad and y > n - rad) or (x > n - rad and y > n - rad)):
                    if (x - ox) ** 2 + (y - oy) ** 2 > rad ** 2:
                        inside = False
                    break
            if not inside:
                row += bytes((0, 0, 0, 0))
                continue
            # 菱形（曼哈顿距离）
            if abs(x - cx) / dia + abs(y - cy) / dia <= 1.0:
                b, g, r = DARK
            else:
                b, g, r = GOLD
            row += bytes((b, g, r, 255))
        rows.append(bytes(row))
    return b"".join(reversed(rows))   # BMP 自下而上


def _dib(n: int) -> bytes:
    """单尺寸 ICO 内的 BMP：BITMAPINFOHEADER(高=2n) + BGRA 像素 + 全 0 AND 掩码。"""
    header = struct.pack("<IiiHHIIiiII", 40, n, n * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    xor = _pixels(n)
    and_row = ((n + 31) // 32) * 4      # 1bpp 行按 4 字节对齐
    and_mask = b"\x00" * (and_row * n)
    return header + xor + and_mask


def build(out: Path) -> None:
    dibs = [(n, _dib(n)) for n in SIZES]
    entries, blobs, offset = [], [], 6 + 16 * len(dibs)
    for n, data in dibs:
        wh = 0 if n >= 256 else n
        entries.append(struct.pack("<BBBBHHII", wh, wh, 0, 0, 1, 32, len(data), offset))
        blobs.append(data)
        offset += len(data)
    ico = struct.pack("<HHH", 0, 1, len(dibs)) + b"".join(entries) + b"".join(blobs)
    out.write_bytes(ico)
    print(f"已生成 {out}（{len(ico)} 字节，尺寸 {SIZES}）")


if __name__ == "__main__":
    build(Path(__file__).resolve().parent / "app.ico")
