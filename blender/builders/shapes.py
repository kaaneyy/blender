"""Shared 2D shape vocabulary (B1/B4/B6): named lathe profiles and loft
cross-section rings. Pure math — mirrored 1:1 in frontend/src/shapes.ts so
the preview lathe/loft geometry matches the Blender build.
"""
from __future__ import annotations

import math
from typing import List, Sequence, Tuple

#: Named revolve profiles as normalized (r, z) points, r in 0..1 of the
#: nominal radius, z in 0..1 of the nominal height (z=0 at the bottom).
#: These are what make lamps look like lamps (B1).
PROFILES_2D = {
    "acorn": [
        (0.02, 0.0), (0.42, 0.02), (0.62, 0.12), (0.78, 0.30), (0.85, 0.50),
        (0.80, 0.68), (0.62, 0.85), (0.35, 0.96), (0.02, 1.0),
    ],
    "teardrop": [
        (0.02, 0.0), (0.55, 0.05), (0.85, 0.25), (0.90, 0.45), (0.75, 0.68),
        (0.45, 0.88), (0.18, 0.97), (0.02, 1.0),
    ],
    "dome": [
        (1.0, 0.0), (0.96, 0.30), (0.85, 0.55), (0.60, 0.80), (0.30, 0.95),
        (0.02, 1.0),
    ],
    "finial": [
        (0.25, 0.0), (0.42, 0.12), (0.30, 0.30), (0.50, 0.50), (0.28, 0.72),
        (0.12, 0.85), (0.02, 1.0),
    ],
    "flared_base": [
        (1.0, 0.0), (0.85, 0.15), (0.55, 0.45), (0.42, 0.75), (0.40, 1.0),
    ],
    "vase": [
        (0.55, 0.0), (0.75, 0.12), (0.92, 0.35), (0.95, 0.55), (0.80, 0.75),
        (0.60, 0.90), (0.62, 1.0),
    ],
}


def resolve_profile(
    profile, radius: float | None = None, depth: float | None = None
) -> List[Tuple[float, float]]:
    """Named profile scaled by radius/depth, or a raw [(r, z), ...] list in
    meters passed through."""
    if isinstance(profile, str):
        try:
            pts = PROFILES_2D[profile]
        except KeyError:
            raise ValueError(
                f"Unknown profile {profile!r}; known: {', '.join(sorted(PROFILES_2D))}"
            ) from None
        if radius is None or depth is None:
            raise ValueError(f"Named profile {profile!r} needs radius and depth")
        return [(r * radius, z * depth) for r, z in pts]
    return [(float(r), float(z)) for r, z in profile]


def ring_points(shape: str, w: float, h: float, n: int = 32) -> List[Tuple[float, float]]:
    """Cross-section ring for lofts: 'ellipse' or 'rect' (superellipse with
    softly rounded corners), centered, n points counter-clockwise."""
    k = 1.0 if shape == "ellipse" else 0.35
    pts = []
    for i in range(n):
        t = 2.0 * math.pi * i / n
        c, s = math.cos(t), math.sin(t)
        pts.append((
            (w / 2.0) * math.copysign(abs(c) ** k, c),
            (h / 2.0) * math.copysign(abs(s) ** k, s),
        ))
    return pts


def profile_bounds(points: Sequence[Tuple[float, float]]) -> Tuple[float, float, float]:
    """(max_r, z_min, z_max) of a resolved lathe profile."""
    max_r = max(abs(r) for r, _ in points)
    zs = [z for _, z in points]
    return max_r, min(zs), max(zs)


def fillet_path(path: Sequence[Sequence[float]], radius: float,
                segments: int = 8) -> List[Tuple[float, float, float]]:
    """Replace each interior corner of a 3D polyline with a circular arc of
    ``radius`` — a specified bend, the way a drawing calls one out, instead
    of whatever a spline happens to do through the same points.

    At a corner P between neighbours A and C: the arc is tangent to both
    legs, so it starts a tangent distance ``r / tan(theta/2)`` back along each
    (theta being the interior angle at P). That distance is clamped to half of
    the shorter leg, so a radius too large for its corner tightens instead of
    overshooting into the neighbouring segment. Endpoints are never moved.

    Degenerate corners are left alone: a straight run has nothing to fillet,
    and a doubled-back one has no tangent solution. Mirrored 1:1 in
    ``frontend/src/shapes.ts``.
    """
    pts = [tuple(float(v) for v in p) for p in path]
    if radius <= 0 or len(pts) < 3:
        return [(p[0], p[1], p[2]) for p in pts]

    out: List[Tuple[float, float, float]] = [pts[0]]
    for i in range(1, len(pts) - 1):
        a, p, c = pts[i - 1], pts[i], pts[i + 1]
        v1 = [a[k] - p[k] for k in range(3)]
        v2 = [c[k] - p[k] for k in range(3)]
        l1 = math.dist(a, p)
        l2 = math.dist(c, p)
        if l1 < 1e-9 or l2 < 1e-9:
            out.append(p)
            continue
        u1 = [v / l1 for v in v1]
        u2 = [v / l2 for v in v2]
        cos_t = max(-1.0, min(1.0, sum(u1[k] * u2[k] for k in range(3))))
        theta = math.acos(cos_t)
        # straight through (theta ~ pi) or doubled back (theta ~ 0): no arc
        if theta < 1e-6 or abs(math.pi - theta) < 1e-6:
            out.append(p)
            continue
        tan_half = math.tan(theta / 2.0)
        t = min(radius / tan_half, l1 / 2.0, l2 / 2.0)
        r_eff = t * tan_half  # the radius that distance actually buys
        t1 = tuple(p[k] + u1[k] * t for k in range(3))
        t2 = tuple(p[k] + u2[k] * t for k in range(3))
        # arc centre: along the corner bisector, r/sin(theta/2) from P
        bis = [u1[k] + u2[k] for k in range(3)]
        bis_len = math.sqrt(sum(v * v for v in bis))
        if bis_len < 1e-9:
            out.append(p)
            continue
        bis = [v / bis_len for v in bis]
        d = r_eff / math.sin(theta / 2.0)
        centre = [p[k] + bis[k] * d for k in range(3)]
        # slerp the arc from t1 to t2 about the centre
        w1 = [t1[k] - centre[k] for k in range(3)]
        w2 = [t2[k] - centre[k] for k in range(3)]
        n1 = math.sqrt(sum(v * v for v in w1))
        n2 = math.sqrt(sum(v * v for v in w2))
        if n1 < 1e-9 or n2 < 1e-9:
            out.append(p)
            continue
        cos_phi = max(-1.0, min(1.0, sum(w1[k] * w2[k] for k in range(3)) / (n1 * n2)))
        phi = math.acos(cos_phi)
        if phi < 1e-9:
            out.append(p)
            continue
        sin_phi = math.sin(phi)
        for s in range(segments + 1):
            f = s / segments
            k1 = math.sin((1.0 - f) * phi) / sin_phi
            k2 = math.sin(f * phi) / sin_phi
            out.append(tuple(
                centre[k] + w1[k] * k1 + w2[k] * k2 for k in range(3)
            ))
    out.append(pts[-1])
    return out
