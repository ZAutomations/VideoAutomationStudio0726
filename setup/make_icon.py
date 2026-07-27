#!/usr/bin/env python
"""Generate the app icon (assets/app_icon.ico) for Video Automation Studio.

Draws a modern rounded-tile icon: an indigo->magenta diagonal gradient with a
film-strip motif down each side and a white play triangle in the centre. Saved
as a multi-resolution .ico (16/32/48/64/128/256) so it looks crisp everywhere
(taskbar, desktop shortcut, Alt-Tab).

Run once (checked-in result travels with the repo)::

    .venv\\Scripts\\python.exe setup\\make_icon.py
"""
from pathlib import Path
from PIL import Image, ImageDraw

S = 256  # master render size (downscaled into the .ico)


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def render(size=S):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # --- diagonal gradient background (indigo -> magenta) ---------------
    top = (99, 60, 233)     # #633CE9 indigo
    bot = (232, 62, 168)    # #E83EA8 magenta
    grad = Image.new("RGB", (size, size), top)
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        for_x = y / (size - 1)
        gd.line([(0, y), (size, y)], fill=_lerp(top, bot, for_x))

    # rounded-tile mask
    radius = int(size * 0.22)
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    img.paste(grad, (0, 0), mask)

    # Semi-transparent details go on a separate overlay so they COMPOSITE
    # over the gradient (drawing RGBA straight onto img would replace pixels).
    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    # --- film-strip perforations down both sides ------------------------
    strip_w = int(size * 0.11)
    hole_w = int(size * 0.055)
    hole_h = int(size * 0.075)
    gap = int(size * 0.045)
    y = gap
    hole_col = (255, 255, 255, 90)
    while y + hole_h < size - gap:
        # left strip
        lx = int(strip_w / 2 - hole_w / 2)
        od.rounded_rectangle([lx, y, lx + hole_w, y + hole_h],
                             radius=int(hole_w * 0.35), fill=hole_col)
        # right strip
        rx = size - strip_w // 2 - hole_w // 2
        od.rounded_rectangle([rx, y, rx + hole_w, y + hole_h],
                             radius=int(hole_w * 0.35), fill=hole_col)
        y += hole_h + gap

    # --- soft circle behind the play triangle ---------------------------
    cx, cy = size / 2, size / 2
    r = size * 0.27
    od.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(255, 255, 255, 60))

    img = Image.alpha_composite(img, overlay)

    # --- centre play triangle (fully opaque white, on top) --------------
    d = ImageDraw.Draw(img)
    t = size * 0.15   # triangle half-height
    off = size * 0.025  # optical centring nudge
    tri = [
        (cx - t * 0.72 + off, cy - t),
        (cx - t * 0.72 + off, cy + t),
        (cx + t + off, cy),
    ]
    d.polygon(tri, fill=(255, 255, 255, 255))

    return img


def main():
    here = Path(__file__).resolve().parent
    assets = here.parent / "assets"
    assets.mkdir(exist_ok=True)
    out = assets / "app_icon.ico"

    master = render(S)
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    master.save(out, format="ICO", sizes=sizes)
    # Also drop a PNG preview for docs / non-Windows use.
    master.save(assets / "app_icon.png", format="PNG")
    print(f"[OK] wrote {out} ({out.stat().st_size} bytes) + app_icon.png")


if __name__ == "__main__":
    main()
