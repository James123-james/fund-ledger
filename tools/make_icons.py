#!/usr/bin/env python3
"""生成 PWA 图标（纯标准库，不依赖 PIL）。

用法：python3 tools/make_icons.py
输出：web/icon-192.png、icon-512.png、icon-maskable-512.png、icon-180.png

想改配色或比例，改下面的 COLORS 和 draw() 里的坐标即可。
坐标按 512×512 设计稿写，脚本内部用 4 倍超采样再缩回来做抗锯齿。
"""

import math
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), "web")

SS = 4                     # 超采样倍数
DESIGN = 512               # 设计稿边长
N = DESIGN * SS

ACCENT = (20, 107, 92)
CREAM = (245, 244, 239)
GOLD = (168, 118, 42)


def blend(fg, bg, alpha):
    return tuple(round(f * alpha + b * (1 - alpha)) for f, b in zip(fg, bg))


class Canvas:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.buf = bytearray(w * h * 4)

    def span(self, y, x0, x1, rgba):
        """一行内填充 [x0, x1)，带 alpha 混合。"""
        if y < 0 or y >= self.h:
            return
        x0 = max(0, int(x0))
        x1 = min(self.w, int(x1))
        if x1 <= x0:
            return
        r, g, b, a = rgba
        row = (y * self.w) * 4
        if a >= 255:
            self.buf[row + x0 * 4:row + x1 * 4] = bytes((r, g, b, 255)) * (x1 - x0)
            return
        inv = 255 - a
        for x in range(x0, x1):
            i = row + x * 4
            self.buf[i] = (r * a + self.buf[i] * inv) // 255
            self.buf[i + 1] = (g * a + self.buf[i + 1] * inv) // 255
            self.buf[i + 2] = (b * a + self.buf[i + 2] * inv) // 255
            self.buf[i + 3] = min(255, self.buf[i + 3] + a)

    def round_rect(self, x0, y0, x1, y1, radius, rgba):
        radius = min(radius, (x1 - x0) / 2, (y1 - y0) / 2)
        for y in range(int(y0), int(math.ceil(y1))):
            dy = 0.0
            if y < y0 + radius:
                dy = (y0 + radius) - y
            elif y > y1 - radius:
                dy = y - (y1 - radius)
            if dy > 0:
                dx = radius - math.sqrt(max(0.0, radius * radius - dy * dy))
            else:
                dx = 0.0
            self.span(y, x0 + dx, x1 - dx, rgba)

    def circle(self, cx, cy, r, rgba):
        for y in range(int(cy - r), int(math.ceil(cy + r))):
            dy = y + 0.5 - cy
            if abs(dy) > r:
                continue
            dx = math.sqrt(max(0.0, r * r - dy * dy))
            self.span(y, cx - dx, cx + dx, rgba)

    def downsample(self, size):
        """box filter 缩放到 size×size，返回 RGBA bytes。"""
        scale = self.w // size
        out = bytearray(size * size * 4)
        area = scale * scale
        for oy in range(size):
            for ox in range(size):
                r = g = b = a = 0
                for sy in range(scale):
                    row = ((oy * scale + sy) * self.w + ox * scale) * 4
                    for sx in range(scale):
                        i = row + sx * 4
                        r += self.buf[i]
                        g += self.buf[i + 1]
                        b += self.buf[i + 2]
                        a += self.buf[i + 3]
                o = (oy * size + ox) * 4
                out[o] = r // area
                out[o + 1] = g // area
                out[o + 2] = b // area
                out[o + 3] = a // area
        return out


def write_png(path, size, rgba):
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)                      # filter: none
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n" +
           chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)) +
           chunk(b"IDAT", zlib.compress(bytes(raw), 9)) +
           chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)


def draw(maskable=False):
    """画一张 N×N 的图。maskable 时把内容缩到中间安全区。"""
    cv = Canvas(N, N)
    center = N / 2
    k = 0.86 if maskable else 1.0

    def s(v):
        """设计稿坐标 → 画布坐标（maskable 时绕中心缩放）。"""
        return center + (v * SS - center) * k

    def rect(x0, y0, x1, y1, r, color):
        cv.round_rect(s(x0), s(y0), s(x1), s(y1), r * SS * k, color)

    # 背景：整块强调色，四角随系统裁切
    rect(0, 0, 512, 512, 116, (*ACCENT, 255))

    # 账本卡片
    rect(92, 92, 420, 420, 58, (*CREAM, 255))

    # 三根柱子，越往后越高
    rect(150, 292, 194, 374, 13, (*blend(ACCENT, CREAM, .42), 255))
    rect(234, 242, 278, 374, 13, (*blend(ACCENT, CREAM, .68), 255))
    rect(318, 180, 362, 374, 13, (*ACCENT, 255))

    # 金色圆点：留一个“收益”的记认
    cv.circle(s(340), s(153), 18 * SS * k, (*GOLD, 255))
    return cv


def main():
    os.makedirs(WEB, exist_ok=True)
    big = draw(maskable=False).downsample(512)
    write_png(os.path.join(WEB, "icon-512.png"), 512, big)
    # 192 和 180 从 512 图里原生重画，比直接缩更干净
    for size in (192, 180):
        write_png(os.path.join(WEB, f"icon-{size}.png"), size,
                  draw(maskable=False).downsample(size))
    write_png(os.path.join(WEB, "icon-maskable-512.png"), 512,
              draw(maskable=True).downsample(512))
    print("icons written to", WEB)


if __name__ == "__main__":
    main()
