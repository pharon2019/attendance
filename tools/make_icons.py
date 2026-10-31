"""Render the PNG app icons (same design as static/icons/icon.svg) using only the standard library."""
import math
import os
import struct
import zlib

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'static', 'icons')
BG, FG = (15, 118, 110), (255, 255, 255)


def seg_dist(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def coverage(x, y):
    """x, y in 0..512 icon units -> (inside rounded square, inside white mark)."""
    r = 112
    cx, cy = min(max(x, r), 512 - r), min(max(y, r), 512 - r)
    in_bg = math.hypot(x - cx, y - cy) <= r
    ring = abs(math.hypot(x - 256, y - 256) - 164) <= 18
    hands = seg_dist(x, y, 256, 150, 256, 262) <= 18 or seg_dist(x, y, 256, 262, 328, 306) <= 18
    return in_bg, ring or hands


def render(size):
    ss = 3
    rows = []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            bg = fg = 0
            for sy in range(ss):
                for sx in range(ss):
                    x = (px + (sx + 0.5) / ss) * 512 / size
                    y = (py + (sy + 0.5) / ss) * 512 / size
                    b, f = coverage(x, y)
                    bg += b
                    fg += b and f
            n = ss * ss
            a, w = bg / n, (fg / bg if bg else 0)
            col = [round(BG[i] * (1 - w) + FG[i] * w) for i in range(3)]
            row += bytes(col + [round(255 * a)])
        rows.append(bytes(row))
    raw = zlib.compress(b''.join(rows), 9)

    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
    png += chunk(b'IDAT', raw) + chunk(b'IEND', b'')
    with open(os.path.join(OUT, f'icon-{size}.png'), 'wb') as fh:
        fh.write(png)


if __name__ == '__main__':
    os.makedirs(OUT, exist_ok=True)
    for s in (192, 512):
        render(s)
    print('icons written to', os.path.normpath(OUT))
