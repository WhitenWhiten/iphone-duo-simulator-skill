#!/usr/bin/env python3
"""Real-3D compositor for the iPhone Duo book pose.

The flat bezel composite (Apple's "Inner Open Landscape" art with the
screenshot pasted into the screen hole) is mapped onto the actual developable
surface of a partially folded inner display and rendered with a small
software rasterizer.  No empirical lift/scale fudge factors: a real pinhole
camera, per-pixel normals and a z-buffer.

Geometry
    mid-surface = plane / circular arc / plane with constant arc length.
    The bend region keeps its flat width ``bend`` (40 pt = 120 px, the
    simulator's own crease width), so at fold angle ``deg`` the radius is
    r = bend / radians(180 - deg).  The slab is extruded to ``thick`` and the
    silhouette outline (rounded corners, camera plateau, hinge slits) is
    traced sub-pixel from the bezel art's alpha channel, so the side walls
    join the art exactly.

Camera
    pinhole perspective, slight elevation, distance as a multiple of the
    device width.  Mild by design; product shots are near-orthographic.

Shading
    matches Apple's own simulator chrome: the face is unlit.  The screen keeps
    its source pixels and the bezel keeps the art's baked lighting, so the
    fold reads from geometry alone (foreshortening, the edge profile, the
    hinge notch, the side walls that come into view).  Only the walls, which
    the flat art does not cover, are shaded: a studio environment from the
    site background, a key light from the upper left, a soft metal specular.
    No crease line, sheen or reflection is ever drawn.

Pure numpy + Pillow.  ``render`` returns a straight-alpha RGBA image at
master resolution; duo_mockup.finish() adds the background and shadow.

From https://foldhunt.com/guides/everything-your-agent-needs-for-iphone-duo/
"""

import math
import sys
import time

import numpy as np
from PIL import Image

# --------------------------------------------------------------------------
# Device metrics, measured from the Night Sky bezel art (art pixels).
# Inner Open Landscape.png is 3093x2247; Outer Open.png is 3056x2194.
# --------------------------------------------------------------------------
DEV_X0, DEV_Y0 = 57.79, 57.90          # device rect origin inside the art
DEV_W, DEV_H = 2976.4, 2130.5          # device rect size (1 px = 1/3 pt)
BACK_X1, BACK_Y0 = 3010.7, 30.5        # back art: rect right edge / top
ART_PAD = 14.0                         # mesh overhang past the rect (bump)

DEF_DEG = 128.0        # fold angle, the simulator's book pose
DEF_BEND = 108.0       # flat width of the bend region (36 pt, ~5 mm)
DEF_THICK = 68.0       # leaf thickness (matches the simulator's side wall)
DEF_ELEV = 0.0         # camera elevation, degrees (simulator: straight on)
DEF_DIST = 5.3         # camera distance, in device widths (simulator: ~5.3)

# shading constants (linear space)
KEY_DIR = np.array([-0.07, 0.60, 0.78])  # key light: upper left, in front
KEY_KD = 0.62                          # diffuse weight
AMB_K = 0.55                           # ambient weight
SB_AMP = 0.80                          # softbox strength
SB_POW = 7.0                           # softbox lobe width
AO_AMP = 0.22                          # fold ambient occlusion depth
WALL_GAIN = 2.1                        # wall brightness vs bezel body
WALL_KS = 0.9                          # wall metal specular
WALL_POW = 3.5
WALL_INSET = 30.0                      # wall albedo sample inset, px
WALL_TINT = np.array([0.84, 0.91, 1.12], np.float32)  # Night Sky blue sheen
BACK_DIM = 0.85                        # back face (not visible by default)
BG_TOP = (250, 250, 251)               # same studio as duo_mockup.BG_*
BG_BOT = (232, 233, 238)


def _srgb_to_lin_lut():
    i = np.arange(256, dtype=np.float64) / 255.0
    lut = np.where(i <= 0.04045, i / 12.92, ((i + 0.055) / 1.055) ** 2.4)
    return lut.astype(np.float32)


def _lin_to_srgb_lut(n=8192):
    i = np.linspace(0.0, 1.0, n)
    lut = np.where(i <= 0.0031308, i * 12.92,
                   1.055 * np.clip(i, 1e-9, None) ** (1.0 / 2.4) - 0.055)
    return np.clip(lut * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)


_LIN = _srgb_to_lin_lut()
_SRGB = _lin_to_srgb_lut()
_LIN3 = _LIN[np.array(BG_TOP, np.int32)].astype(np.float32)
_BOT3 = _LIN[np.array(BG_BOT, np.int32)].astype(np.float32)
_KEY = KEY_DIR / np.linalg.norm(KEY_DIR)


# --------------------------------------------------------------------------
# screen hole mask
# --------------------------------------------------------------------------
def screen_mask_from_alpha(alpha):
    """Screen hole of a bezel art as a float 0..1 mask.

    The screen is the widest enclosed transparent region; hinge slits,
    camera rings and antenna gaps are narrower and are excluded, so they stay
    transparent (the 3D render shows hardware behind them; the flat poses
    show the dark underlay)."""
    a = np.asarray(alpha)
    if a.dtype != np.uint8:
        a = np.clip(a * 255.0 + 0.5, 0, 255).astype(np.uint8)
    inside = a < 128
    H, W = inside.shape
    runs = []
    for y in range(H):
        xs = np.where(inside[y])[0]
        if xs.size == 0:
            continue
        starts = np.concatenate(([xs[0]], xs[1:][np.diff(xs) > 1]))
        ends = np.concatenate((xs[:-1][np.diff(xs) > 1], [xs[-1]]))
        for s, e in zip(starts.tolist(), ends.tolist()):
            if s > 0 and e < W - 1 and e - s > 8:
                runs.append((y, s, e))
    if not runs:
        return np.zeros((H, W), np.float32)
    max_w = max(e - s + 1 for _, s, e in runs)
    thr = max(0.25 * max_w, 100.0)
    mask = np.zeros((H, W), np.float32)
    for y, s, e in runs:
        if e - s + 1 >= thr:
            mask[y, s:e + 1] = 1.0
    return mask


def interior_mask(alpha, thr=128):
    """Everything inside the outer silhouette (for the dark underlay)."""
    a = np.asarray(alpha)
    if a.dtype != np.uint8:
        a = np.clip(a * 255.0 + 0.5, 0, 255).astype(np.uint8)
    solid = a >= thr
    H, W = solid.shape
    out = np.zeros((H, W), np.uint8)
    for y in range(H):
        xs = np.where(solid[y])[0]
        if xs.size:
            out[y, xs[0]:xs[-1] + 1] = 255
    return out


# --------------------------------------------------------------------------
# silhouette contour (marching squares, sub-pixel)
# --------------------------------------------------------------------------
def trace_outer_contour(alpha, n_points=1600, smooth=5):
    """Trace the outer 0.5-contour of the art alpha and resample it
    uniformly.  Returns an (n, 2) array of art-pixel coordinates."""
    a = np.asarray(alpha, np.float32)
    if a.max() > 1.0:
        a = a / 255.0
    ap = np.zeros((a.shape[0] + 2, a.shape[1] + 2), np.float32)
    ap[1:-1, 1:-1] = a
    Hp, Wp = ap.shape
    b = ap >= 0.5
    hl = b[:, :-1] != b[:, 1:]
    ys, xs = np.where(hl)
    t = (0.5 - ap[ys, xs]) / (ap[ys, xs + 1] - ap[ys, xs])
    hpt = np.stack([xs + t, ys.astype(np.float32)], 1)
    hidx = np.full((Hp, Wp), -1, np.int64)
    hidx[ys, xs] = np.arange(ys.size)
    vl = b[:-1, :] != b[1:, :]
    ys2, xs2 = np.where(vl)
    t2 = (0.5 - ap[ys2, xs2]) / (ap[ys2 + 1, xs2] - ap[ys2, xs2])
    vpt = np.stack([xs2.astype(np.float32), ys2 + t2], 1)
    vidx = np.full((Hp, Wp), -1, np.int64)
    vidx[ys2, xs2] = np.arange(ys2.size)
    OFF = Hp * Wp

    def eid(name, y, x):
        if name == 0:      # top edge
            return y * Wp + x
        if name == 1:      # bottom
            return (y + 1) * Wp + x
        if name == 2:      # left
            return OFF + y * Wp + x
        return OFF + y * Wp + (x + 1)      # right

    case = ((b[:-1, :-1].astype(np.int8) << 3) | (b[:-1, 1:] << 2)
            | (b[1:, 1:] << 1) | b[1:, :-1])
    ctr = (ap[:-1, :-1] + ap[:-1, 1:] + ap[1:, 1:] + ap[1:, :-1]) * 0.25
    TABLE = {1: [(2, 1)], 2: [(1, 3)], 3: [(2, 3)], 4: [(0, 3)], 6: [(0, 1)],
             7: [(0, 2)], 8: [(0, 2)], 9: [(0, 1)], 11: [(0, 3)],
             12: [(2, 3)], 13: [(1, 3)], 14: [(2, 1)]}
    segs_a, segs_b = [], []
    for k, pairs in TABLE.items():
        yy, xx = np.where(case == k)
        for e1, e2 in pairs:
            segs_a.append(eid(e1, yy, xx))
            segs_b.append(eid(e2, yy, xx))
    for k, (ph, pl) in ((5, (((0, 3), (2, 1)), ((0, 2), (1, 3)))),
                        (10, (((0, 2), (1, 3)), ((0, 3), (2, 1))))):
        yy, xx = np.where(case == k)
        hi = ctr[yy, xx] >= 0.5
        for (e1, e2), sel in ((ph[0], hi), (ph[1], hi), (pl[0], ~hi), (pl[1], ~hi)):
            yv, xv = yy[sel], xx[sel]
            segs_a.append(eid(e1, yv, xv))
            segs_b.append(eid(e2, yv, xv))
    A = np.concatenate(segs_a)
    B = np.concatenate(segs_b)
    nxt = {}
    for p, q in zip(A.tolist(), B.tolist()):
        nxt.setdefault(p, []).append(q)
        nxt.setdefault(q, []).append(p)

    def point(e):
        if e < OFF:
            y, x = divmod(e, Wp)
            return hpt[hidx[y, x]]
        y, x = divmod(e - OFF, Wp)
        return vpt[vidx[y, x]]

    used = set()
    best, best_area = None, 0.0
    for start in list(nxt.keys()):
        if start in used:
            continue
        loop = []
        cur = start
        while cur not in used:
            used.add(cur)
            loop.append(cur)
            cand = [q for q in nxt[cur] if q not in used]
            if not cand:
                break
            cur = cand[0]
        if len(loop) < 16:
            continue
        pts = np.array([point(e) for e in loop], np.float32)
        area = 0.5 * abs(np.dot(pts[:, 0], np.roll(pts[:, 1], 1))
                         - np.dot(pts[:, 1], np.roll(pts[:, 0], 1)))
        if area > best_area:
            best, best_area = pts, area
    pts = best - 1.0
    d = np.sqrt(((np.roll(pts, -1, 0) - pts) ** 2).sum(1))
    s = np.concatenate([[0.0], np.cumsum(d)])
    per = s[-1]
    tgt = np.linspace(0.0, per, n_points, endpoint=False)
    ext = np.concatenate([pts, pts[:1]])
    out = np.stack([np.interp(tgt, s, ext[:, 0]),
                    np.interp(tgt, s, ext[:, 1])], 1)
    if smooth and smooth > 1:
        k = np.ones(smooth, np.float32) / smooth
        pad = smooth // 2
        for c in range(2):
            col = np.concatenate([out[-pad:, c], out[:, c], out[:pad, c]])
            out[:, c] = np.convolve(col, k, "same")[pad:pad + out.shape[0]]
    return out.astype(np.float32)


# --------------------------------------------------------------------------
# fold geometry
# --------------------------------------------------------------------------
def fold_profile(u, theta, bend):
    """Mid-surface of the folded display.

    u      unrolled offset from the fold centre (px)
    theta  total bend angle in radians (180 - deg)
    bend   flat width of the bend region (px)
    Returns x, z and the front normal (nx, nz).  The display is on the
    inside of the fold: the hinge (u = 0, z = 0) is the point farthest from
    the viewer and both leaves come forward (+z) like the pages of a book.
    Arc length is preserved: |dP/du| = 1 everywhere."""
    r = bend / theta
    half = 0.5 * bend
    t2 = 0.5 * theta
    psi = np.clip(u / r, -t2, t2)
    au = np.abs(u)
    flat = au > half
    x = np.where(flat,
                 np.sign(u) * (r * math.sin(t2) + (au - half) * math.cos(t2)),
                 r * np.sin(psi))
    z = np.where(flat,
                 r * (1.0 - math.cos(t2)) + (au - half) * math.sin(t2),
                 r * (1.0 - np.cos(psi)))
    return x, z, -np.sin(psi), np.cos(psi)


def _bilinear(img, xs, ys):
    """Bilinear sample of (H, W, C) float32 at float coords; clamped."""
    H, W = img.shape[0], img.shape[1]
    xs = np.clip(xs, 0.0, W - 1.001)
    ys = np.clip(ys, 0.0, H - 1.001)
    x0 = xs.astype(np.int32)
    y0 = ys.astype(np.int32)
    fx = (xs - x0)[:, None]
    fy = (ys - y0)[:, None]
    c00 = img[y0, x0]
    c10 = img[y0, x0 + 1]
    c01 = img[y0 + 1, x0]
    c11 = img[y0 + 1, x0 + 1]
    return (c00 * (1 - fx) * (1 - fy) + c10 * fx * (1 - fy)
            + c01 * (1 - fx) * fy + c11 * fx * fy)


# --------------------------------------------------------------------------
# renderer
# --------------------------------------------------------------------------
def render(front_rgba, back_rgba, deg=DEF_DEG, bend=DEF_BEND,
           thick=DEF_THICK, elev=DEF_ELEV, dist=DEF_DIST, out_w=3200, ss=2,
           margin=0.055, band_rows=512, verbose=False):
    """Render the folded device.  front_rgba/back_rgba: PIL RGBA bezel art
    (front already carries the screenshot).  Returns a PIL RGBA image at
    master resolution."""
    # numpy on macOS/Accelerate reports spurious FP flags from matmul
    with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
        return _render(front_rgba, back_rgba, deg, bend, thick, elev, dist,
                       out_w, ss, margin, band_rows, verbose)


def _render(front_rgba, back_rgba, deg, bend, thick, elev, dist, out_w, ss,
            margin, band_rows, verbose):
    t0 = time.time()
    theta = max(math.radians(180.0 - float(deg)), math.radians(0.4))
    T2 = 0.5 * thick
    W2, H2 = DEV_W * 0.5, DEV_H * 0.5
    sin_half = math.sin(0.5 * theta)

    front = _LIN[np.asarray(front_rgba.convert("RGBA"), np.uint8)]
    back = _LIN[np.asarray(back_rgba.convert("RGBA"), np.uint8)]

    # ---- silhouette contour -> device coords, outward normals, albedo
    contour = trace_outer_contour(front[..., 3])
    contour = np.concatenate([contour, contour[:1]])       # close the loop
    N = contour.shape[0]                                   # N+1 points
    cu = (contour[:, 0] - (DEV_X0 + W2)).astype(np.float32)
    cv = ((DEV_Y0 + H2) - contour[:, 1]).astype(np.float32)
    tang = np.roll(contour, -1, 0) - np.roll(contour, 1, 0)
    tang /= np.maximum(np.linalg.norm(tang, axis=1, keepdims=True), 1e-6)
    nrm = np.stack([tang[:, 1], -tang[:, 0]], 1)           # art coords, y down
    cen = contour.mean(0)
    flip = (np.sum((contour - cen) * nrm, 1) < 0)
    nrm[flip] *= -1.0
    alb = _bilinear(front, contour[:, 0] - WALL_INSET * nrm[:, 0],
                    contour[:, 1] - WALL_INSET * nrm[:, 1])[:, :3]
    wall_n2 = np.stack([nrm[:, 0], -nrm[:, 1]], 1)         # device coords
    mx, mz, mnx, mnz = fold_profile(cu, theta, bend)
    psi_c = np.clip(cu / (bend / theta), -0.5 * theta, 0.5 * theta)
    t_hat = np.stack([np.cos(psi_c), np.zeros(N, np.float32), np.sin(psi_c)], -1)
    wall_n3 = (wall_n2[:, 0:1] * t_hat
               + np.stack([np.zeros(N, np.float32), wall_n2[:, 1],
                           np.zeros(N, np.float32)], -1))
    wall_n3 /= np.maximum(np.linalg.norm(wall_n3, axis=1, keepdims=True), 1e-6)

    # ---- meshes
    hb = 0.5 * bend
    us_front = np.concatenate([
        np.linspace(-W2, -hb, 220, endpoint=False),
        np.linspace(-hb, hb, 150, endpoint=False),
        np.linspace(hb, W2, 220, endpoint=False), [W2]])
    us_back = np.linspace(-W2, W2, 201)
    v_rows = np.array([-H2 - ART_PAD, H2 + ART_PAD], np.float32)

    def surf(u, v, side):
        x, z, nx, nz = fold_profile(u, theta, bend)
        return np.stack([x + side * nx * T2, v, z + side * nz * T2], -1)

    def build_face(us, side, tag):
        verts = []
        uvs = []
        for v in v_rows:
            verts.append(surf(us, np.full_like(us, v), side))
            if tag == 0:
                uvs.append(np.stack([us + W2 + DEV_X0,
                                     np.full_like(us, DEV_Y0 + H2 - v)], -1))
            else:
                uvs.append(np.stack([BACK_X1 - (us + W2),
                                     np.full_like(us, BACK_Y0 + H2 - v)], -1))
        verts = np.concatenate(verts).astype(np.float32)
        uvs = np.concatenate(uvs).astype(np.float32)
        n = us.size
        i0 = np.arange(n - 1)
        f = np.stack([np.concatenate([i0, i0]),
                      np.concatenate([i0 + 1, i0 + n + 1]),
                      np.concatenate([i0 + n + 1, i0 + n])], 1)
        return verts, uvs, f, tag

    fv, fuv, ff, _ = build_face(us_front, +1, 0)
    bv, buv, bf, _ = build_face(us_back, -1, 1)

    wall_verts = np.empty((2 * N, 3), np.float32)
    wall_verts[0::2] = np.stack([mx + mnx * T2, cv, mz + mnz * T2], -1)
    wall_verts[1::2] = np.stack([mx - mnx * T2, cv, mz - mnz * T2], -1)
    i0 = np.arange(N - 1)
    i1 = i0 + 1
    wf = np.stack([np.concatenate([2 * i0, 2 * i0]),
                   np.concatenate([2 * i1, 2 * i1 + 1]),
                   np.concatenate([2 * i1 + 1, 2 * i0 + 1])], 1)

    verts = np.concatenate([fv, bv, wall_verts])
    if not np.isfinite(verts).all():
        raise RuntimeError("duo3d: non-finite mesh vertices")
    n_f, n_b = fv.shape[0], bv.shape[0]
    faces = np.concatenate([ff, bf + n_f, wf + n_f + n_b])
    tags = np.concatenate([np.zeros(ff.shape[0], np.int8),
                           np.ones(bf.shape[0], np.int8),
                           np.full(wf.shape[0], 2, np.int8)])
    uvs_all = np.concatenate([fuv, buv, np.zeros((2 * N, 2), np.float32)])
    wall_idx = np.repeat(np.arange(N, dtype=np.float32), 2)  # s = point index
    wall_t = np.tile(np.array([0.0, 1.0], np.float32), N)  # front / back
    key = np.where(tags == 1, 0, np.where(tags == 2, 1, 2))
    order = np.argsort(key, kind="stable")                 # back, walls, front
    faces_o = faces[order]
    tags_o = tags[order]

    # ---- camera
    D = dist * DEV_W
    el = math.radians(elev)
    C = np.array([0.0, D * math.sin(el), D * math.cos(el)], np.float32)
    fwd = (-C / np.linalg.norm(C)).astype(np.float32)
    right = np.cross(fwd, np.array([0.0, 1.0, 0.0], np.float32))
    right /= np.linalg.norm(right)
    up = np.cross(right, fwd)
    rel = verts - C
    zc_all = rel @ fwd
    tx = (rel @ right) / zc_all
    ty = (rel @ up) / zc_all
    x0f, x1f = tx.min(), tx.max()
    y0f, y1f = ty.min(), ty.max()
    S = out_w * (1 - 2 * margin) / (x1f - x0f)
    mw = int(out_w)
    mh = int(round((y1f - y0f) * S / (1 - 2 * margin)))
    mh += (-mh) % ss
    SW, SH = mw * ss, mh * ss
    cxf = mw * 0.5 - 0.5 * (x0f + x1f) * S
    cyf = mh * 0.5 + 0.5 * (y0f + y1f) * S
    sp = np.stack([(tx * S + cxf) * ss, (cyf - ty * S) * ss], 1)
    iw_all = (1.0 / zc_all).astype(np.float32)

    # ---- environment
    V0 = -C / np.linalg.norm(C)
    n0 = np.array([0.0, 0.0, 1.0], np.float32)
    R0 = V0 - 2 * (V0 @ n0) * n0
    SB_DIR = (R0 / np.linalg.norm(R0)).astype(np.float32)

    def env(d):
        d = d / np.maximum(np.linalg.norm(d, axis=-1, keepdims=True), 1e-9)
        t = (0.5 * d[..., 1] + 0.5)[..., None]
        base = _BOT3 + (_LIN3 - _BOT3) * t
        lobe = np.clip(d @ SB_DIR, 0.0, None) ** SB_POW
        return base + lobe[..., None] * SB_AMP

    # wall shading is normalised to a flat page of the folded device (tilted
    # by theta/2), so a wall facing the camera matches the bezel body.
    n_page = np.array([-math.sin(0.5 * theta), 0.0, math.cos(0.5 * theta)],
                      np.float32)
    DENOM = (env(n_page[None, :])[0] * AMB_K
             + KEY_KD * max(0.0, float(n_page @ _KEY)))

    ctx = dict(env=env, DENOM=DENOM, theta=theta, bend=bend, T2=T2,
               sin_half=sin_half, C=C, fwd=fwd, front=front, back=back,
               uvs=uvs_all, wall_verts_data=(cu, cv, alb, wall_n3),
               wall_idx=wall_idx, wall_t=wall_t, N=N)

    # ---- banded rasterization
    master = np.zeros((mh, mw, 4), np.float32)
    n_bands = (SH + band_rows - 1) // band_rows
    for band in range(n_bands):
        ys0 = band * band_rows
        ys1 = min(ys0 + band_rows, SH)
        bh = ys1 - ys0
        zbuf = np.full((bh, SW), np.inf, np.float32)
        col = np.zeros((bh, SW, 3), np.float32)
        acc = np.zeros((bh, SW), np.float32)
        for fi in range(faces_o.shape[0]):
            vid = faces_o[fi]
            tag = tags_o[fi]
            px = sp[vid, 0]
            py = sp[vid, 1] - ys0
            ix0 = max(0, int(np.floor(px.min() - 0.5)))
            ix1 = min(SW, int(np.ceil(px.max() + 0.5)) + 1)
            iy0 = max(0, int(np.floor(py.min() - 0.5)))
            iy1 = min(bh, int(np.ceil(py.max() + 0.5)) + 1)
            if ix1 <= ix0 or iy1 <= iy0:
                continue
            x0, y0 = float(px[0]), float(py[0])
            x1, y1 = float(px[1]), float(py[1])
            x2, y2 = float(px[2]), float(py[2])
            det = (x1 - x0) * (y2 - y0) - (y1 - y0) * (x2 - x0)
            if abs(det) < 1e-9:
                continue
            if det < 0:
                x1, y1, x2, y2 = x2, y2, x1, y1
                vid = vid[[0, 2, 1]]
                det = -det
            X = np.arange(ix0, ix1, dtype=np.float32) + 0.5
            Y = np.arange(iy0, iy1, dtype=np.float32) + 0.5
            XX, YY = np.meshgrid(X, Y)
            l0 = ((y1 - y2) * (XX - x2) + (x2 - x1) * (YY - y2)) / det
            l1 = ((y2 - y0) * (XX - x2) + (x0 - x2) * (YY - y2)) / det
            l2 = 1.0 - l0 - l1
            ins = (l0 >= -1e-4) & (l1 >= -1e-4) & (l2 >= -1e-4)
            if not ins.any():
                continue
            yl, xl = np.nonzero(ins)
            iw0, iw1, iw2 = iw_all[vid]
            L0 = l0[yl, xl]
            L1 = l1[yl, xl]
            L2 = l2[yl, xl]
            yy = yl + iy0                      # band-local buffer rows
            xx = xl + ix0
            if tag == 2:
                wv = vid - (n_f + n_b)        # local wall vertex indices
                iw = L0 * iw0 + L1 * iw1 + L2 * iw2
                s = (L0 * wall_idx[wv[0]] * iw0 + L1 * wall_idx[wv[1]] * iw1
                     + L2 * wall_idx[wv[2]] * iw2) / iw
                t = (L0 * wall_t[wv[0]] * iw0 + L1 * wall_t[wv[1]] * iw1
                     + L2 * wall_t[wv[2]] * iw2) / iw
                _shade_wall(s, t, yy, xx, zbuf, col, acc, ctx)
            elif tag == 0:
                _shade_face(L0, L1, L2, iw0, iw1, iw2, yy, xx, vid, zbuf, col,
                            acc, ctx, True)
            else:
                _shade_face(L0, L1, L2, iw0, iw1, iw2, yy, xx, vid, zbuf, col,
                            acc, ctx, False)
        blk = col.reshape(bh // ss, ss, SW // ss, ss, 3).mean(axis=(1, 3))
        ba = acc.reshape(bh // ss, ss, SW // ss, ss).mean(axis=(1, 3))
        r0 = ys0 // ss
        master[r0:r0 + bh // ss, :, :3] = blk
        master[r0:r0 + bh // ss, :, 3] = ba
        if verbose:
            print("  band %d/%d  %.1fs" % (band + 1, n_bands, time.time() - t0))

    a = np.clip(master[..., 3:4], 0.0, 1.0)
    rgb = master[..., :3] / np.maximum(a, 1e-6)
    idx = np.clip(rgb * (_SRGB.size - 1) + 0.5, 0, _SRGB.size - 1).astype(np.int32)
    out = np.empty(master.shape, np.uint8)
    out[..., :3] = _SRGB[idx]
    out[..., 3] = np.clip(a[..., 0] * 255.0 + 0.5, 0, 255).astype(np.uint8)
    if verbose:
        print("  render %dx%d ss=%d  %.1fs" % (mw, mh, ss, time.time() - t0))
    return Image.fromarray(out, "RGBA")


def _attr_interp(a0, a1, a2, L0, L1, L2, iw0, iw1, iw2):
    iw = L0 * iw0 + L1 * iw1 + L2 * iw2
    return (L0 * a0 * iw0 + L1 * a1 * iw1 + L2 * a2 * iw2) / iw, iw


def _shade_face(L0, L1, L2, iw0, iw1, iw2, yy, xx, vid, zbuf, col, acc, ctx,
                is_front):
    """Front/back face: unlit texture lookup on the exact folded surface."""
    uvs = ctx["uvs"]
    au, iw = _attr_interp(uvs[vid[0], 0], uvs[vid[1], 0], uvs[vid[2], 0],
                          L0, L1, L2, iw0, iw1, iw2)
    av, _ = _attr_interp(uvs[vid[0], 1], uvs[vid[1], 1], uvs[vid[2], 1],
                         L0, L1, L2, iw0, iw1, iw2)
    theta, bend, T2 = ctx["theta"], ctx["bend"], ctx["T2"]
    if is_front:
        uu = au - (DEV_X0 + DEV_W * 0.5)
        vv = (DEV_Y0 + DEV_H * 0.5) - av
    else:
        uu = (BACK_X1 - DEV_W * 0.5) - au
        vv = (BACK_Y0 + DEV_H * 0.5) - av
    x_, z_, nx, nz = fold_profile(uu, theta, bend)
    side = 1.0 if is_front else -1.0
    C, fwd = ctx["C"], ctx["fwd"]
    zc = ((x_ + side * nx * T2 - C[0]) * fwd[0] + (vv - C[1]) * fwd[1]
          + (z_ + side * nz * T2 - C[2]) * fwd[2])
    if is_front:
        zc = zc - 0.05                    # front face wins coincident ties
    vis = zc < zbuf[yy, xx]
    if not vis.any():
        return
    yy, xx, zc = yy[vis], xx[vis], zc[vis]
    tex = _bilinear(ctx["front"] if is_front else ctx["back"], au[vis], av[vis])
    a = tex[:, 3]
    # Apple's own chrome renders the face unlit: the screen keeps its source
    # pixels and the bezel its baked lighting; the fold reads from geometry.
    rgb = tex[:, :3] if is_front else tex[:, :3] * BACK_DIM
    if is_front:
        op = a >= 0.5
        if op.any():
            i2 = (yy[op], xx[op])
            col[i2] = rgb[op]
            acc[i2] = 1.0
            zbuf[i2] = zc[op]
        pa = (~op) & (a > 0.003)
        if pa.any():
            i3 = (yy[pa], xx[pa])
            ca = acc[i3]
            a_p = a[pa]
            col[i3] = col[i3] * (1 - a_p)[:, None] + rgb[pa] * a_p[:, None]
            acc[i3] = ca + (1 - ca) * a_p
    else:
        i2 = (yy, xx)
        col[i2] = col[i2] * (1 - a)[:, None] + rgb * a[:, None]
        acc[i2] = acc[i2] + (1 - acc[i2]) * a
        zbuf[i2] = zc


def _shade_wall(s, t, yy, xx, zbuf, col, acc, ctx):
    cu, cv, alb, wall_n3 = ctx["wall_verts_data"]
    theta, bend, T2 = ctx["theta"], ctx["bend"], ctx["T2"]
    i = np.clip(np.floor(s).astype(np.int32), 0, ctx["N"] - 1)
    f = np.clip(s - i, 0.0, 1.0)[:, None]
    u = cu[i] * (1 - f[:, 0]) + cu[i + 1] * f[:, 0]
    v = cv[i] * (1 - f[:, 0]) + cv[i + 1] * f[:, 0]
    albedo = alb[i] * (1 - f) + alb[i + 1] * f
    n_w = wall_n3[i] * (1 - f) + wall_n3[i + 1] * f
    n_w /= np.maximum(np.linalg.norm(n_w, axis=1, keepdims=True), 1e-9)
    x_, z_, nx, nz = fold_profile(u, theta, bend)
    off = T2 * (1.0 - 2.0 * t)
    P = np.stack([x_ + nx * off, v, z_ + nz * off], -1)
    C, fwd = ctx["C"], ctx["fwd"]
    rel = P - C
    zc = rel @ fwd
    vis = zc < zbuf[yy, xx]
    if not vis.any():
        return
    yy, xx = yy[vis], xx[vis]
    u, t, zc = u[vis], t[vis], zc[vis]
    albedo, n_w = albedo[vis], n_w[vis]
    rel = rel[vis]
    V = rel / np.maximum(np.linalg.norm(rel, axis=1, keepdims=True), 1e-9)
    env, DENOM = ctx["env"], ctx["DENOM"]
    ndl = np.clip(n_w @ _KEY, 0.0, None)
    ao = 1.0 - AO_AMP * np.exp(-(u / (0.62 * bend)) ** 2) * ctx["sin_half"]
    m = (env(n_w) * (AMB_K * ao)[:, None] + (KEY_KD * ndl)[:, None]) / DENOM
    H = _KEY[None, :] - V
    H /= np.maximum(np.linalg.norm(H, axis=1, keepdims=True), 1e-9)
    spec = (WALL_KS * np.clip((n_w * H).sum(1), 0.0, 1.0) ** WALL_POW)[:, None]
    rgb = albedo * m * WALL_GAIN + spec * env(H) * WALL_TINT
    i2 = (yy, xx)
    col[i2] = rgb
    acc[i2] = 1.0
    zbuf[i2] = zc


def main():
    if len(sys.argv) < 5:
        print("usage: duo3d.py <ring.png> <back.png> <shot.png> <out.png>"
              " [deg] [out_w] [ss]")
        return 1
    ring = Image.open(sys.argv[1]).convert("RGBA")
    back = Image.open(sys.argv[2]).convert("RGBA")
    shot = Image.open(sys.argv[3]).convert("RGB")
    mask = screen_mask_from_alpha(np.asarray(ring.split()[3]))
    ys, xs = np.where(mask > 0.5)
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    if shot.size != (x1 - x0, y1 - y0):
        shot = shot.resize((x1 - x0, y1 - y0), Image.LANCZOS)
    comp = ring.copy()
    lay = Image.new("RGBA", ring.size, (0, 0, 0, 0))
    lay.paste(shot.convert("RGBA"), (int(x0), int(y0)))
    m = Image.fromarray((mask * 255).astype(np.uint8))
    comp.alpha_composite(Image.composite(lay, Image.new("RGBA", ring.size), m))
    deg = float(sys.argv[5]) if len(sys.argv) > 5 else DEF_DEG
    out_w = int(sys.argv[6]) if len(sys.argv) > 6 else 3200
    ss = int(sys.argv[7]) if len(sys.argv) > 7 else 2
    img = render(comp, back, deg=deg, out_w=out_w, ss=ss, verbose=True)
    img.save(sys.argv[4])
    print(sys.argv[4], img.size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
