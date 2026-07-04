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
