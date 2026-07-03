"""Connection-hardware generator: turns the joints between components into
visible bolt/nut assemblies (hex heads = 6-segment cylinders).

Heuristic: wherever primitives from two *different* components overlap, the
center of the overlap box is a connection point; a bolt (hex head + shaft +
hex nut) is placed there along the tightest overlap axis. Rotated primitives
use conservative bounding boxes — this is representative hardware for
visualization and export, not shop drawings.

Enabled by the spec toggle ``connection_hardware``; mirrored 1:1 in
frontend/src/builders/hardware.ts.
"""
from __future__ import annotations

import math
from typing import List, Tuple

from .base import Primitive

HEAD_R = 0.016
HEAD_H = 0.010
SHAFT_R = 0.007
SHAFT_LEN = 0.026
NUT_R = 0.014
NUT_H = 0.008
MAX_CONNECTIONS = 24
GRID = 0.06  # dedupe grid, meters

#: bolt axis -> rotation that maps a Z-axis cylinder onto that axis
_AXIS_ROT = {
    0: (0.0, math.pi / 2, 0.0),  # X
    1: (math.pi / 2, 0.0, 0.0),  # Y
    2: (0.0, 0.0, 0.0),          # Z
}


def _half_extents(p: Primitive) -> Tuple[float, float, float]:
    if p.kind == "box":
        sx, sy, sz = p.params["size"]
        hx, hy, hz = sx / 2, sy / 2, sz / 2
    elif p.kind in ("cylinder", "cone"):
        r = p.params.get("radius") or max(
            p.params.get("radius_bottom", 0.0), p.params.get("radius_top", 0.0)
        )
        hx, hy, hz = r, r, p.params["depth"] / 2
    else:  # sphere
        r = p.params["radius"]
        hx = hy = hz = r
    if any(abs(a) > 1e-6 for a in p.rotation):
        m = max(hx, hy, hz)  # conservative for rotated parts
        return (m, m, m)
    return (hx, hy, hz)


def _bolt(index: int, center: Tuple[float, float, float], axis: int) -> List[Primitive]:
    rot = _AXIS_ROT[axis]
    def along(dist: float) -> Tuple[float, float, float]:
        out = list(center)
        out[axis] += dist
        return tuple(out)

    return [
        Primitive(
            kind="cylinder", name=f"bolt_{index}_shaft", component="hardware",
            location=center, rotation=rot, material_slot="hardware",
            params={"radius": SHAFT_R, "depth": SHAFT_LEN},
        ),
        Primitive(
            kind="cylinder", name=f"bolt_{index}_head", component="hardware",
            location=along(SHAFT_LEN / 2 + HEAD_H / 2), rotation=rot,
            material_slot="hardware",
            params={"radius": HEAD_R, "depth": HEAD_H, "segments": 6},
        ),
        Primitive(
            kind="cylinder", name=f"bolt_{index}_nut", component="hardware",
            location=along(-(SHAFT_LEN / 2 + NUT_H / 2)), rotation=rot,
            material_slot="hardware",
            params={"radius": NUT_R, "depth": NUT_H, "segments": 6},
        ),
    ]


def compute_hardware(prims: List[Primitive]) -> List[Primitive]:
    boxes = [(p, p.location, _half_extents(p)) for p in prims if p.component != "hardware"]
    out: List[Primitive] = []
    seen: set = set()
    n = 0

    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            pa, ca, ha = boxes[i]
            pb, cb, hb = boxes[j]
            if pa.component == pb.component:
                continue

            lo = [max(ca[k] - ha[k], cb[k] - hb[k]) for k in range(3)]
            hi = [min(ca[k] + ha[k], cb[k] + hb[k]) for k in range(3)]
            if any(hi[k] <= lo[k] for k in range(3)):
                continue  # no overlap

            center = tuple((lo[k] + hi[k]) / 2 for k in range(3))
            key = tuple(round(c / GRID) for c in center)
            if key in seen:
                continue
            seen.add(key)

            axis = min(range(3), key=lambda k: hi[k] - lo[k])
            n += 1
            out.extend(_bolt(n, center, axis))
            if n >= MAX_CONNECTIONS:
                return out
    return out
