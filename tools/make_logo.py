"""Draw an original monochrome raven pictogram and preview."""

from pathlib import Path
import math
from PIL import Image, ImageDraw


SILHOUETTE = [
    (96, 36), (83, 26), (76, 17), (66, 14), (58, 20), (52, 30),
    (40, 40), (26, 54), (12, 74), (4, 92), (14, 90), (30, 80),
    (44, 80), (58, 76), (68, 64), (72, 50), (74, 42), (80, 41), (91, 40),
]
EYE = (68, 25, 3)
WING = ((60, 52), (44, 56), (22, 70))
WING_WIDTH = 3
SHARP = {(96, 36), (4, 92)}
ROOT = Path(__file__).resolve().parents[1]
SIZE = 2048


def smooth(points):
    for _ in range(2):
        refined = []
        for index, point in enumerate(points):
            if point in SHARP:
                refined.append(point)
                continue
            previous, following = points[index - 1], points[(index + 1) % len(points)]
            refined.extend([
                tuple(0.15 * a + 0.85 * b for a, b in zip(previous, point)),
                tuple(0.85 * a + 0.15 * b for a, b in zip(point, following)),
            ])
        points = refined
    return points


def wing_points():
    start, control, end = WING
    result = []
    for step in range(129):
        t = step / 128
        result.append(tuple((1 - t) ** 2 * a + 2 * (1 - t) * t * b + t * t * c
                            for a, b, c in zip(start, control, end)))
    return result


def wing_outline():
    points = wing_points()
    normals = []
    for index in range(len(points)):
        a, b = points[max(0, index - 1)], points[min(len(points) - 1, index + 1)]
        angle = math.atan2(b[1] - a[1], b[0] - a[0]) + math.pi / 2
        normals.append(angle)
    radius = WING_WIDTH / 2
    def offset(point, angle):
        return (point[0] + radius * math.cos(angle), point[1] + radius * math.sin(angle))
    outline = [offset(p, angle) for p, angle in zip(points, normals)]
    outline += [offset(points[-1], normals[-1] - math.pi * i / 16) for i in range(1, 17)]
    outline += [offset(p, angle + math.pi) for p, angle in reversed(list(zip(points, normals))) ]
    outline += [offset(points[0], normals[0] + math.pi - math.pi * i / 16) for i in range(1, 17)]
    return outline


def glyph_mask(points, transform):
    mask = Image.new("L", (SIZE, SIZE), 0)
    draw = ImageDraw.Draw(mask)
    draw.polygon([transform(p) for p in points], fill=255)
    x, y, radius = EYE
    draw.ellipse((*transform((x - radius, y - radius)),
                  *transform((x + radius, y + radius))), fill=0)
    draw.polygon([transform(p) for p in wing_outline()], fill=0)
    return mask


def path_data(points):
    return "M " + " L ".join(f"{x:.5f},{y:.5f}" for x, y in points) + " Z"


def svg_text(points, colour):
    x, y, radius = EYE
    eye = (f"M {x - radius},{y} a {radius},{radius} 0 1,0 {2 * radius},0 "
           f"a {radius},{radius} 0 1,0 {-2 * radius},0 Z")
    data = f"{path_data(points)} {eye} {path_data(wing_outline())}"
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">\n'
            f'  <path fill="{colour}" fill-rule="evenodd" d="{data}"/>\n</svg>\n')


def verify(path, badge):
    with Image.open(path) as image:
        corners = [(0, 0), (image.width - 1, 0), (0, image.height - 1),
                   (image.width - 1, image.height - 1)]
        alpha = tuple(image.getpixel(p)[3] for p in corners)
        print(f"{path.relative_to(ROOT).as_posix()}: format={image.format}, size={image.size}, mode={image.mode}")
        print(f"  corner alpha (TL, TR, BL, BR)={alpha}; file size={path.stat().st_size / 1024:.2f} KB")
        if badge:
            assert alpha == (0, 0, 0, 0)
        else:
            print("  preview strips are intentionally opaque")


def main():
    assets, data = ROOT / "assets", ROOT / "data"
    assets.mkdir(exist_ok=True)
    data.mkdir(exist_ok=True)
    produced, pngs = set(), []

    def save(name, content, badge=False):
        path = ROOT / name
        try:
            if isinstance(content, str):
                path.write_text(content, encoding="utf-8")
            else:
                content.save(path)
        except PermissionError:
            path = path.with_stem(path.stem + "_new")
            print(f"ERROR: overwrite blocked for {name}; writing {path.relative_to(ROOT).as_posix()}")
            if isinstance(content, str):
                path.write_text(content, encoding="utf-8")
            else:
                content.save(path)
        produced.add(path)
        if path.suffix == ".png":
            pngs.append((path, badge))

    points = smooth(SILHOUETTE)
    min_x, max_x = min(p[0] for p in points), max(p[0] for p in points)
    min_y, max_y = min(p[1] for p in points), max(p[1] for p in points)
    scale = 0.70 / max(max_x - min_x, max_y - min_y)
    center = ((min_x + max_x) / 2, (min_y + max_y) / 2)
    def transform(point):
        return tuple(round((0.5 + (value - mid) * scale) * SIZE)
                     for value, mid in zip(point, center))
    mask = glyph_mask(points, transform)
    badge = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(badge).rounded_rectangle((0, 0, SIZE - 1, SIZE - 1),
                                           radius=round(SIZE * 0.22), fill="#000000")
    badge.paste((255, 255, 255, 255), (0, 0), mask)
    badge = badge.resize((512, 512), Image.Resampling.LANCZOS)
    for name in ("raven_icon.png", "raven.png", "raven_light.png"):
        save("assets/" + name, badge, True)
    save("assets/favicon.png", badge.resize((64, 64), Image.Resampling.LANCZOS), True)
    save("assets/raven_white.svg", svg_text(points, "#FFFFFF"))
    for name in ("raven_black.svg", "raven.svg"):
        save("assets/" + name, svg_text(points, "#000000"))
    preview = Image.new("RGBA", (1056, 1088), "#0A0B10")
    ImageDraw.Draw(preview).rectangle((0, 544, 1055, 1087), fill="#EEF0F6")
    for row in (0, 544):
        for size, x in ((512, 16), (128, 552), (48, 704), (32, 776)):
            tile = badge.resize((size, size), Image.Resampling.LANCZOS)
            preview.alpha_composite(tile, (x, row + (544 - size) // 2))
    black_glyph = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 255))
    black_glyph.putalpha(mask)
    preview.alpha_composite(black_glyph.resize((192, 192), Image.Resampling.LANCZOS), (840, 720))
    save("data/logo_preview.png", preview)
    for path, is_badge in pngs:
        verify(path, is_badge)
    print("assets/ inventory:")
    for path in sorted(assets.rglob("*")):
        if path.is_file():
            status = "produced" if path in produced else "OTHER: not produced by tools/make_logo.py"
            print(f"  {path.relative_to(ROOT).as_posix()} [{status}]")


if __name__ == "__main__":
    main()
