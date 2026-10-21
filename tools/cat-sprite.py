#!/usr/bin/env python3
"""Cắt bảng sprite do model vẽ ảnh tạo ra (nền xanh bạc hà đặc) thành từng PNG nền trong suốt.

Tách nền bằng loang từ mép ảnh theo độ gần màu nền (không xoá màu bạc hà bên trong hình),
rồi gom vùng liền nhau; mảnh nhỏ (dấu "!", tia sáng) gộp vào hình lớn gần nhất.
Chạy lại khi vẽ lại ảnh trong web/assets/raw/ (ảnh gốc không nằm trong repo):
    python3 tools/cat-sprite.py
"""
import os
from collections import deque

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "..", "web", "assets", "raw")
OUT = os.path.join(HERE, "..", "web", "assets")

# Tên theo thứ tự đọc (hàng trên xuống, trái sang phải).
SHEETS = {
    "nhan-vien.png": ["nv-xanh-lam", "nv-tim-lam", "nv-la-lam", "nv-hong-lam",
                      "nv-xanh-hoi", "nv-tim-hoi", "nv-la-hoi", "nv-hong-hoi"],
    "robot.png": ["robot-lam", "robot-hoi", "tro-ly"],
    "do-dac.png": ["cay-1", "cay-2", "bang-trang", "may-cafe", "sofa", "cay-nuoc", "ke-sach", "may-chu",
                   "tham-tron", "den", "tu-ho-so", "ban-cafe"],
}
# Nhóm phải cùng khung (bàn đứng yên khi nhân vật đổi tư thế): căn đáy và mép phải.
UNIFORM = [["nv-xanh-lam", "nv-tim-lam", "nv-la-lam", "nv-hong-lam",
            "nv-xanh-hoi", "nv-tim-hoi", "nv-la-hoi", "nv-hong-hoi"], ["robot-lam", "robot-hoi"]]
TOL = 38          # độ lệch màu tối đa so với nền để tính là nền
MAX_SIDE = 320    # cạnh dài nhất của sprite sau khi thu nhỏ


def bg_color(img):
    w, h = img.size
    px = img.load()
    samples = [px[x, y] for x in (2, w // 2, w - 3) for y in (2, h - 3)]
    return tuple(sorted(c[i] for c in samples)[len(samples) // 2] for i in range(3))


def near(c, bg):
    return abs(c[0] - bg[0]) + abs(c[1] - bg[1]) + abs(c[2] - bg[2]) <= TOL


def remove_bg(img):
    img = img.convert("RGBA")
    w, h = img.size
    px = img.load()
    bg = bg_color(img)
    seen = bytearray(w * h)
    q = deque()
    for x in range(w):
        q.extend([(x, 0), (x, h - 1)])
    for y in range(h):
        q.extend([(0, y), (w - 1, y)])
    while q:
        x, y = q.popleft()
        i = y * w + x
        if seen[i]:
            continue
        seen[i] = 1
        if not near(px[x, y], bg):
            continue
        px[x, y] = (0, 0, 0, 0)
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx]:
                q.append((nx, ny))
    return img


def components(img):
    w, h = img.size
    a = img.getchannel("A").load()
    label = [0] * (w * h)
    boxes = []
    for y0 in range(h):
        for x0 in range(w):
            if a[x0, y0] == 0 or label[y0 * w + x0]:
                continue
            n = len(boxes) + 1
            box = [x0, y0, x0, y0, 0]
            q = deque([(x0, y0)])
            label[y0 * w + x0] = n
            while q:
                x, y = q.popleft()
                box[0], box[1] = min(box[0], x), min(box[1], y)
                box[2], box[3] = max(box[2], x), max(box[3], y)
                box[4] += 1
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if 0 <= nx < w and 0 <= ny < h and a[nx, ny] and not label[ny * w + nx]:
                        label[ny * w + nx] = n
                        q.append((nx, ny))
            boxes.append(box)
    return boxes


def gap(b, c):
    dx = max(c[0] - b[2], b[0] - c[2], 0)
    dy = max(c[1] - b[3], b[1] - c[3], 0)
    return (dx * dx + dy * dy) ** 0.5


def cut(sheet, names):
    img = remove_bg(Image.open(os.path.join(RAW, sheet)))
    boxes = components(img)
    big_area = max(b[4] for b in boxes)
    bigs = [b[:4] for b in boxes if b[4] > big_area * 0.08]
    for b in boxes:
        if b[4] <= big_area * 0.08 and b[4] > 12:
            target = min(bigs, key=lambda c: gap(b, c))
            if gap(b, target) < 90:
                target[0], target[1] = min(target[0], b[0]), min(target[1], b[1])
                target[2], target[3] = max(target[2], b[2]), max(target[3], b[3])
    # Thứ tự đọc: gom theo hàng dựa trên tâm dọc.
    bigs.sort(key=lambda b: (b[1] + b[3]) / 2)
    rows, row = [], []
    for b in bigs:
        if row and (b[1] + b[3]) / 2 - (row[-1][1] + row[-1][3]) / 2 > 120:
            rows.append(row)
            row = []
        row.append(b)
    rows.append(row)
    ordered = [b for r in rows for b in sorted(r, key=lambda b: b[0])]
    if len(ordered) != len(names):
        raise SystemExit(f"{sheet}: tìm thấy {len(ordered)} hình, cần {len(names)}")
    crops = {name: img.crop((max(0, x0 - 4), max(0, y0 - 4), x1 + 5, y1 + 5))
             for name, (x0, y0, x1, y1) in zip(names, ordered)}
    for group in UNIFORM:
        group = [n for n in group if n in crops]
        if not group:
            continue
        w = max(crops[n].width for n in group)
        h = max(crops[n].height for n in group)
        for n in group:
            canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            canvas.paste(crops[n], (w - crops[n].width, h - crops[n].height))
            crops[n] = canvas
    for name, crop in crops.items():
        scale = MAX_SIDE / max(crop.size)
        if scale < 1:
            crop = crop.resize((round(crop.width * scale), round(crop.height * scale)), Image.LANCZOS)
        crop.save(os.path.join(OUT, name + ".png"), optimize=True)
        print(f"{name}.png {crop.size}")


def tile(src, name, size=256):
    Image.open(os.path.join(RAW, src)).convert("RGB").resize((size, size), Image.LANCZOS) \
        .save(os.path.join(OUT, name + ".png"), optimize=True)
    print(f"{name}.png ({size}x{size})")


if __name__ == "__main__":
    for sheet, names in SHEETS.items():
        cut(sheet, names)
    tile("san-go.png", "san-go")
    tile("tham-xanh.png", "tham-xanh")
