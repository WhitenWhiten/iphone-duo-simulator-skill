#!/usr/bin/env python3
"""Set iPhone Duo simulator captures in Apple's product bezels, flat or folded.

  python3 duo_mockup.py cover.png  out.png --pose closed
  python3 duo_mockup.py open.png   out.png --pose open
  python3 duo_mockup.py open.png   out.png --pose book --deg 128
  python3 duo_mockup.py upright.png out.png --pose upright
  python3 duo_mockup.py upright.png out.png --pose laptop --deg 110

closed / open / upright paste the capture into the screen hole of Apple's flat bezel
art. Apple publishes no art for a part-folded Duo, so book and laptop fold the flat
composite in duo3d.py, a small software renderer (next to this file): a developable
plane-arc-plane surface with constant arc length, an extruded slab, the silhouette
traced from the art, a straight-on pinhole camera, and the face left unlit like the
simulator's own chrome. laptop turns an upright capture on its side, folds it like
the book, and turns the render back.

Bring your own bezels. This kit contains no Apple artwork: download the iPhone Duo
product bezels from Apple Design Resources (https://developer.apple.com/design/resources/)
and put them in ~/Library/Application Support/apple-duo-bezels/ (or pass --bezels).
Files are read by Apple's names, e.g. "iPhone Duo - Night Sky - Inner Open Landscape.png".
Apple's licence allows them in mock-ups of your app; do not republish the PNGs.

Captures come straight from `simctl io screenshot` (duo.py shot): cover 1398x2034,
inner 2007x2853 sideways or 2853x2007 upright. Needs numpy and Pillow.

From https://foldhunt.com/guides/everything-your-agent-needs-for-iphone-duo/
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import duo3d  # noqa: E402

BG_TOP = (250, 250, 251)
BG_BOT = (232, 233, 238)

BEZEL_DIR = os.path.expanduser(
    os.environ.get("DUO_BEZELS", "~/Library/Application Support/apple-duo-bezels"))
FINISH = "Night Sky"


def vgrad(size, stops):
    """Vertical gradient. stops: [(pos0..1, (r,g,b) or int), ...]"""
    w, h = size
    gray = not isinstance(stops[0][1], tuple)
    img = Image.new("L" if gray else "RGB", (1, h))
    px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        for i in range(len(stops) - 1):
            p0, c0 = stops[i]
            p1, c1 = stops[i + 1]
            if p0 <= t <= p1:
                f = (t - p0) / max(1e-6, p1 - p0)
                px[0, y] = round(c0 + (c1 - c0) * f) if gray else \
                    tuple(round(a + (b - a) * f) for a, b in zip(c0, c1))
                break
    return img.resize((w, h), Image.BILINEAR)


def hgrad(size, stops):
    """Horizontal gradient."""
    return vgrad((size[1], size[0]), stops).transpose(Image.ROTATE_90)


def find_coeffs(dst, src):
    """Perspective coefficients for PIL: maps dst quad pixels to src quad."""
    m = []
    for (x, y), (u, v) in zip(dst, src):
        m.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        m.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    b = [c for pair in src for c in pair]
    return list(np.linalg.solve(np.array(m, float), np.array(b, float)))


def spine(width, height):
    """Cylindrical hinge barrel, vertical."""
    s = hgrad((width, height), [
        (0.00, (14, 15, 18)), (0.22, (48, 51, 60)), (0.42, (132, 138, 152)),
        (0.50, (176, 182, 196)), (0.58, (112, 117, 130)), (0.80, (38, 40, 46)),
        (1.00, (12, 13, 16))]).convert("RGBA")
    m = Image.new("L", s.size, 0)
    ImageDraw.Draw(m).rounded_rectangle(
        [0, 0, width - 1, height - 1], radius=int(width * 0.5), fill=255)
    s.putalpha(m)
    return s


def bezel(name):
    path = os.path.join(BEZEL_DIR, f"iPhone Duo - {FINISH} - {name}.png")
    if not os.path.exists(path):
        raise SystemExit(
            f"Missing bezel art: {path}\n"
            "Download the iPhone Duo product bezels from Apple Design Resources\n"
            "(https://developer.apple.com/design/resources/), accept the licence, and put\n"
            f"the PNGs for the '{FINISH}' finish in {BEZEL_DIR} (or pass --bezels).")
    return Image.open(path).convert("RGBA")


def paste_screen(ring, screen):
    """Screenshot into the bezel's screen hole, ring on top.

    The screen hole is the widest enclosed transparent region of the art
    (duo3d.screen_mask_from_alpha), so narrow transparent details - hinge
    slits, the camera ring, antenna gaps - are left alone.  A dark underlay
    fills the device interior first, so those details read as shadowed
    hardware instead of showing the page background through them.  When the
    capture matches the hole 1:1 (it does for simctl output) nothing is
    resampled at all."""
    alpha = np.array(ring.split()[3])
    mask = duo3d.screen_mask_from_alpha(alpha)
    ys, xs = np.where(mask > 0.5)
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    shot = screen.convert("RGBA")
    if shot.size != (x1 - x0, y1 - y0):
        shot = shot.resize((x1 - x0, y1 - y0), Image.LANCZOS)
    out = Image.new("RGBA", ring.size, (0, 0, 0, 0))
    dark = Image.new("RGBA", ring.size, (10, 11, 13, 255))
    dark.putalpha(Image.fromarray(duo3d.interior_mask(alpha)))
    out.alpha_composite(dark)
    lay = Image.new("RGBA", ring.size, (0, 0, 0, 0))
    lay.paste(shot, (int(x0), int(y0)))
    m = Image.fromarray((mask * 255).astype(np.uint8))
    out.alpha_composite(Image.composite(lay, Image.new("RGBA", ring.size), m))
    out.alpha_composite(ring)
    return out


def finish(rgba, pad, shadow_off=(0, 50), blur=72):
    W, H = rgba.size[0] + pad * 2, rgba.size[1] + pad * 2
    canvas = vgrad((W, H), [(0.0, BG_TOP), (1.0, BG_BOT)]).convert("RGBA")
    alpha = rgba.split()[3]
    full = Image.new("L", canvas.size, 0)
    full.paste(alpha, (pad, pad))
    off = Image.new("L", canvas.size, 0)
    off.paste(alpha, (pad + shadow_off[0], pad + shadow_off[1]))
    sh = Image.new("RGBA", canvas.size, (20, 21, 26, 0))
    sh.putalpha(off.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: v * 92 // 255))
    canvas.alpha_composite(sh)
    canvas.alpha_composite(rgba, (pad, pad))
    return canvas.convert("RGB")


def shot_closed(screen):
    ring = bezel("Outer Closed Portrait")
    return finish(paste_screen(ring, screen), pad=170)


def shot_open(screen):
    ring = bezel("Inner Open Landscape")
    return finish(paste_screen(ring, screen), pad=160)


def shot_book(screen, deg=128.0):
    """Book pose: the flat inner-display composite folded by the real-3D
    renderer in duo3d.py (developable surface, extruded slab, traced
    silhouette, straight-on pinhole camera, unlit face, lit side walls).
    deg is the opening angle between the two leaves; 128 matches the
    simulator's book pose."""
    ring = bezel("Inner Open Landscape")
    comp = paste_screen(ring, screen)
    rgba = duo3d.render(comp, bezel("Outer Open"), deg=deg)
    return finish(rgba, pad=180)


def shot_upright(screen):
    ring = bezel("Inner Open Portrait")
    return finish(paste_screen(ring, screen), pad=170)


def shot_laptop(screen, deg=110.0):
    """Laptop pose: the book renderer on the capture turned sideways, the
    result turned back upright."""
    ring = bezel("Inner Open Landscape")
    comp = paste_screen(ring, screen.transpose(Image.ROTATE_90))
    rgba = duo3d.render(comp, bezel("Outer Open"), deg=deg)
    return finish(rgba.transpose(Image.ROTATE_270), pad=180)


def main():
    global BEZEL_DIR, FINISH
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("capture", help="simctl screenshot of the panel")
    parser.add_argument("out", help="output image (PNG, or anything Pillow writes)")
    parser.add_argument("--pose", required=True,
                        choices=["closed", "open", "book", "upright", "laptop"])
    parser.add_argument("--deg", type=float,
                        help="opening angle for book (default 128) and laptop (default 110)")
    parser.add_argument("--size", type=int, default=1600, help="longest side of the output, px")
    parser.add_argument("--bezels", help="folder holding Apple's iPhone Duo bezel PNGs")
    parser.add_argument("--finish", default=FINISH, help='bezel finish, e.g. "Night Sky"')
    args = parser.parse_args()
    if args.bezels:
        BEZEL_DIR = os.path.expanduser(args.bezels)
    FINISH = args.finish

    screen = Image.open(args.capture).convert("RGB")
    if args.pose == "book":
        img = shot_book(screen, args.deg or 128.0)
    elif args.pose == "laptop":
        img = shot_laptop(screen, args.deg or 110.0)
    elif args.pose == "upright":
        img = shot_upright(screen)
    elif args.pose == "closed":
        img = shot_closed(screen)
    else:
        img = shot_open(screen)
    img.thumbnail((args.size, args.size), Image.LANCZOS)
    img.save(args.out)
    print(args.out, img.size)


if __name__ == "__main__":
    main()
