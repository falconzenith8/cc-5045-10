"""Real Monte Carlo path tracer (pure stdlib).

Renders square-tile chunks into a shared-memory accumulator so a job can be
progressively refined across many independently scheduled chunks. No third
party libraries: vectors are tuples, PNGs are encoded by hand.
"""

import hashlib
import json
import math
import random
import struct
import zlib

EPS = 1e-4
MAX_DEPTH = 4


# ---------------------------------------------------------------- vectors
def vadd(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def vsub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def vmul(a, k):
    return (a[0] * k, a[1] * k, a[2] * k)


def vdot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def vcross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def vnorm(a):
    l = math.sqrt(vdot(a, a))
    return (a[0] / l, a[1] / l, a[2] / l)


def reflect(d, n):
    k = 2.0 * vdot(d, n)
    return vsub(d, vmul(n, k))


def refract(d, n, ior):
    cosi = -vdot(d, n)
    eta = 1.0 / ior if cosi > 0 else ior
    nn = n if cosi > 0 else vmul(n, -1.0)
    k = 1.0 - eta * eta * (1.0 - cosi * cosi)
    if k < 0:
        return None
    return vadd(vmul(d, eta), vmul(nn, eta * cosi - math.sqrt(k)))


# ---------------------------------------------------------------- geometry
def _hit_sphere(ray, prim, t_min, t_max):
    oc = vsub(ray[0], prim["center"])
    b = vdot(oc, ray[1])
    c = vdot(oc, oc) - prim["radius"] ** 2
    disc = b * b - c
    if disc < 0:
        return None
    sq = math.sqrt(disc)
    t = -b - sq
    if t <= t_min or t >= t_max:
        t = -b + sq
        if t <= t_min or t >= t_max:
            return None
    p = vadd(ray[0], vmul(ray[1], t))
    n = vnorm(vsub(p, prim["center"]))
    return t, p, n, prim["material"]


def _hit_plane(ray, prim, t_min, t_max):
    denom = vdot(ray[1], prim["normal"])
    if abs(denom) < 1e-8:
        return None
    t = vdot(vsub(prim["point"], ray[0]), prim["normal"]) / denom
    if t <= t_min or t >= t_max:
        return None
    p = vadd(ray[0], vmul(ray[1], t))
    return t, p, prim["normal"], prim["material"]


def scene_hit(scene, ray, t_min, t_max):
    closest = None
    for prim in scene["primitives"]:
        if prim["type"] == "sphere":
            h = _hit_sphere(ray, prim, t_min, t_max)
        else:
            h = _hit_plane(ray, prim, t_min, t_max)
        if h and (closest is None or h[0] < closest[0]):
            closest = h
    return closest


# ---------------------------------------------------------------- shading
def _sky(ray, top, bottom):
    t = 0.5 * (ray[1][1] + 1.0)
    return (
        bottom[0] * (1 - t) + top[0] * t,
        bottom[1] * (1 - t) + top[1] * t,
        bottom[2] * (1 - t) + top[2] * t,
    )


def trace(scene, ray, depth, rng):
    hit = scene_hit(scene, ray, EPS, 1e9)
    if hit is None:
        return _sky(ray, scene["sky_top"], scene["sky_bottom"])
    _, p, n, mat = hit
    mtype = mat["type"]
    if mtype == "emissive":
        return mat["emission"]
    if depth <= 0:
        return (0.0, 0.0, 0.0)

    if mtype == "lambert":
        # cosine-weighted hemisphere sample
        r1 = rng.random() * 2.0 * math.pi
        r2 = rng.random()
        r2s = math.sqrt(r2)
        w = n
        u = vnorm(vcross((0, 1, 0) if abs(n[1]) < 0.9 else (1, 0, 0), w))
        v = vcross(w, u)
        d = vnorm(
            vadd(
                vmul(u, math.cos(r1) * r2s),
                vadd(vmul(v, math.sin(r1) * r2s), vmul(w, math.sqrt(1 - r2))),
            )
        )
        li = trace(scene, (p, d), depth - 1, rng)
        a = mat["albedo"]
        return (a[0] * li[0], a[1] * li[1], a[2] * li[2])

    if mtype == "metal":
        d = reflect(ray[1], n)
        if mat.get("roughness", 0.0) > 0:
            d = vnorm(vadd(d, vmul(_random_unit(rng), mat["roughness"])))
        if vdot(d, n) <= 0:
            return (0.0, 0.0, 0.0)
        li = trace(scene, (p, d), depth - 1, rng)
        a = mat["albedo"]
        return (a[0] * li[0], a[1] * li[1], a[2] * li[2])

    # dielectric glass
    ior = mat.get("ior", 1.5)
    cosi = -vdot(ray[1], n)
    r0 = ((1 - ior) / (1 + ior)) ** 2
    rprob = r0 + (1 - r0) * (1 - max(cosi, 0.0)) ** 5
    if rng.random() < rprob:
        d = reflect(ray[1], n)
    else:
        d = refract(ray[1], n, ior)
        if d is None:
            d = reflect(ray[1], n)
    li = trace(scene, (p, vnorm(d)), depth - 1, rng)
    a = mat["albedo"]
    return (a[0] * li[0], a[1] * li[1], a[2] * li[2])


def _random_unit(rng):
    while True:
        v = (rng.random() * 2 - 1, rng.random() * 2 - 1, rng.random() * 2 - 1)
        if vdot(v, v) < 1:
            return vnorm(v)


# ---------------------------------------------------------------- camera
def make_camera(cam):
    pos = cam["pos"]
    w = vnorm(vsub(pos, cam["target"]))
    u = vnorm(vcross(cam.get("up", (0, 1, 0)), w))
    v = vcross(w, u)
    half_h = math.tan(math.radians(cam["fov"]) * 0.5)
    half_w = half_h * cam["_aspect"]
    return pos, u, v, w, half_w, half_h


def camera_ray(camera, sx, sy, rng):
    pos, u, v, w, half_w, half_h, width, height = camera
    jx = rng.random()
    jy = rng.random()
    dx = (2 * (sx + jx) / width - 1) * half_w
    dy = (1 - 2 * (sy + jy) / height) * half_h
    d = vnorm(vadd(vmul(u, dx), vadd(vmul(v, dy), vmul(w, -1.0))))
    return (pos, d)


# ---------------------------------------------------------------- rendering
def render_chunk(scene, cam, width, height, tile, spp, accum, counts,
                 lock=None, cancel_event=None, progress_cb=None):
    """Render one tile with `spp` samples/pixel, accumulating shared arrays.

    accum: cdouble array of width*height*3, running sum of radiance.
    counts: cint array of width*height, samples accumulated.
    Tiles cover disjoint pixels, so no lock is needed for the accumulator;
    `lock` is accepted for API symmetry but unused.

    Returns the number of full rows finished. Cancellation is only observed
    between rows, so the return value is exactly resumable: the caller may
    re-dispatch the tile with its top edge moved down by this many rows and no
    pixel is sampled twice or skipped. That row boundary is the bounded
    non-preemptible phase of a chunk.
    """
    cam = dict(cam)
    cam["_aspect"] = width / height
    camera = make_camera(cam) + (width, height)
    x0, y0, x1, y1 = tile
    rows = 0
    for y in range(y0, y1):
        if cancel_event is not None and cancel_event.is_set():
            return rows
        for x in range(x0, x1):
            seed_src = f"{x},{y},{counts[y * width + x]},{x0},{y0}".encode()
            rng = random.Random(int.from_bytes(
                hashlib.blake2b(seed_src, digest_size=8).digest(), "big"))
            col = [0.0, 0.0, 0.0]
            for _ in range(spp):
                ray = camera_ray(camera, x, y, rng)
                c = trace(scene, ray, MAX_DEPTH, rng)
                col[0] += c[0]
                col[1] += c[1]
                col[2] += c[2]
            idx = y * width + x
            base = idx * 3
            accum[base] += col[0]
            accum[base + 1] += col[1]
            accum[base + 2] += col[2]
            counts[idx] += spp
        rows += 1
        if progress_cb:
            progress_cb(rows)
    return rows


# ---------------------------------------------------------------- tonemap / png
def _tonemap(c):
    # ACES filmic approximation
    a = 2.51
    b = 0.03
    cc = 2.43
    d = 0.59
    e = 0.14
    out = []
    for x in c:
        x = max(0.0, x)
        v = (x * (a * x + b)) / (x * (cc * x + d) + e)
        out.append(max(0.0, min(1.0, v)) ** (1 / 2.2))
    return out


def snapshot_png(width, height, accum, counts):
    """Read shared accumulator and produce PNG bytes of the current mean."""
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0
        for x in range(width):
            idx = y * width + x
            n = counts[idx]
            base = idx * 3
            if n == 0:
                rgb = (12, 12, 16)
            else:
                rgb = [
                    int(255 * v + 0.5)
                    for v in _tonemap(
                        (accum[base] / n, accum[base + 1] / n, accum[base + 2] / n)
                    )
                ]
            raw.extend(rgb)
    return encode_png(width, height, bytes(raw))


def encode_png(width, height, rgb_rowdata):
    def chunk(tag, data):
        c = tag + data
        return (
            struct.pack(">I", len(data))
            + c
            + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rgb_rowdata, 6))
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------- config
def config_hash(config):
    blob = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()[:12]
