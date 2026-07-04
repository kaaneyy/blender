"""Connection-hardware generator v2: engineered joints, not decorations.

For every place two *different* components genuinely intersect, this emits
hardware the way a fabricator would detail it:

* **Through-bolt assemblies** — shaft spans the actual joint (overlap depth
  plus up to 25 mm of embedment into each member), hex head + flat washer on
  one face, flat washer + hex nut on the opposite face. Bolt diameter scales
  with the smaller face dimension of the joint (M8–M22 territory), and wide
  joints get 2- or 4-bolt patterns with realistic edge distances instead of
  one center bolt.
* **Band clamps** — when a horizontal round member meets a vertical pole
  (mast arms, banner brackets), a saddle band wraps the pole at the joint
  height (radius follows the pole's taper) with two side through-bolts,
  matching how pole fittings clamp in the field.

Accuracy notes: rotated cylinders/cones use their true oriented bounding box
(axis from the Euler rotation), so bolts only appear where parts really
touch; joints with a face too thin to drill (<10 mm) are skipped; joint
count is capped. Mirrored 1:1 in frontend/src/builders/hardware.ts.

Enabled by the spec toggle ``connection_hardware``.
"""
from __future__ import annotations

import math
from typing import List, Sequence, Tuple

from .base import Primitive
from .shapes import profile_bounds, resolve_profile

#: kinds treated as round members for band-clamp detection
ROUND_KINDS = ("cylinder", "cone", "sweep", "tube")

MAX_JOINTS = 24
EMBED = 0.025      # max bolt embedment into each member beyond the joint, m
MIN_FACE = 0.010   # skip joints whose bolt face is thinner than this, m
GRID = 0.06        # joint dedupe grid, m

#: bolt axis -> rotation that maps a Z-axis cylinder onto that axis
_AXIS_ROT = {
    0: (0.0, math.pi / 2, 0.0),  # X
    1: (math.pi / 2, 0.0, 0.0),  # Y
    2: (0.0, 0.0, 0.0),          # Z
}


def _cylinder_axis(rotation: Sequence[float]) -> Tuple[float, float, float]:
    """Unit axis of a cylinder/cone after a Blender XYZ Euler rotation."""
    rx, ry, rz = rotation
    x, y, z = 0.0, -math.sin(rx), math.cos(rx)
    x, z = x * math.cos(ry) + z * math.sin(ry), -x * math.sin(ry) + z * math.cos(ry)
    x, y = x * math.cos(rz) - y * math.sin(rz), x * math.sin(rz) + y * math.cos(rz)
    return (x, y, z)


def _aabb(p: Primitive) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
    """(center, half_extents) of the primitive's world AABB. Exact for
    boxes/spheres/tubes and arbitrarily rotated cylinders/cones
    (oriented-cylinder AABB); path/profile-based for sweeps and lathes;
    conservative cube for rotated constructed kinds."""
    loc = p.location

    def rotated_cube(h):
        m = max(h)
        return (loc, (m, m, m))

    if p.kind == "box":
        sx, sy, sz = p.params["size"]
        h = (sx / 2, sy / 2, sz / 2)
        if any(abs(a) > 1e-6 for a in p.rotation):
            return rotated_cube(h)
        return (loc, h)
    if p.kind == "sphere":
        r = p.params["radius"]
        return (loc, (r, r, r))
    if p.kind == "lathe":
        pts = resolve_profile(
            p.params["profile"], radius=p.params.get("radius"),
            depth=p.params.get("depth"),
        )
        max_r, z0, z1 = profile_bounds(pts)
        h = (max_r, max_r, (z1 - z0) / 2)
        center = (loc[0], loc[1], loc[2] + (z0 + z1) / 2)
        if any(abs(a) > 1e-6 for a in p.rotation):
            return rotated_cube(h)
        return (center, h)
    if p.kind == "sweep":
        r = max(p.params["radius"], p.params.get("radius_end", 0.0))
        xs = [pt[0] for pt in p.params["path"]]
        ys = [pt[1] for pt in p.params["path"]]
        zs = [pt[2] for pt in p.params["path"]]
        h = (
            (max(xs) - min(xs)) / 2 + r,
            (max(ys) - min(ys)) / 2 + r,
            (max(zs) - min(zs)) / 2 + r,
        )
        center = (
            loc[0] + (max(xs) + min(xs)) / 2,
            loc[1] + (max(ys) + min(ys)) / 2,
            loc[2] + (max(zs) + min(zs)) / 2,
        )
        if any(abs(a) > 1e-6 for a in p.rotation):
            return rotated_cube(h)
        return (center, h)
    if p.kind == "loft":
        ps, pe = p.params["profile_start"], p.params["profile_end"]
        h = (
            max(ps["w"], pe["w"]) / 2,
            max(ps["h"], pe["h"]) / 2,
            p.params["depth"] / 2,
        )
        if any(abs(a) > 1e-6 for a in p.rotation):
            return rotated_cube(h)
        return (loc, h)
    # cylinder / cone / tube
    r = p.params.get("radius") or max(
        p.params.get("radius_bottom", 0.0), p.params.get("radius_top", 0.0)
    )
    hd = p.params["depth"] / 2
    u = _cylinder_axis(p.rotation)
    return (
        loc,
        tuple(
            hd * abs(u[k]) + r * math.sqrt(max(0.0, 1.0 - u[k] * u[k]))
            for k in range(3)
        ),
    )


def _half_extents(p: Primitive) -> Tuple[float, float, float]:
    """Back-compat wrapper — extents only (center may differ for sweeps)."""
    return _aabb(p)[1]


def _radius_at_z(prim: Primitive, z: float) -> float:
    """Radius of an upright cylinder/tube/cone at world height z."""
    if prim.kind in ("cylinder", "tube"):
        return prim.params["radius"]
    rb = prim.params["radius_bottom"]
    rt = prim.params["radius_top"]
    depth = prim.params["depth"]
    t = (z - (prim.location[2] - depth / 2)) / depth if depth else 0.0
    return rb + (rt - rb) * min(1.0, max(0.0, t))


def _is_upright_round(p: Primitive) -> bool:
    return p.kind in ("cylinder", "cone", "tube") and all(
        abs(a) < 1e-3 for a in p.rotation
    )


def _pos(center: Sequence[float], axis: int, along: float) -> Tuple[float, float, float]:
    out = list(center)
    out[axis] = along
    return tuple(out)


def _bolt(joint: int, idx: int, center: Sequence[float], axis: int,
          shaft_r: float, span_lo: float, span_hi: float) -> List[Primitive]:
    """Through-bolt: washer+hex head at span_hi, washer+hex nut at span_lo."""
    rot = _AXIS_ROT[axis]
    head_r = 1.8 * shaft_r
    head_h = max(1.2 * shaft_r, 0.004)
    nut_r = 1.6 * shaft_r
    nut_h = max(shaft_r, 0.003)
    w_r = 2.2 * shaft_r
    w_h = 0.002
    depth = max(span_hi - span_lo, 0.012) + 2 * w_h
    mid = (span_lo + span_hi) / 2
    name = f"joint{joint}_bolt{idx}"

    def prim(kind_name: str, along: float, radius: float, d: float, segments=None):
        params = {"radius": radius, "depth": d}
        if segments:
            params["segments"] = segments
        return Primitive(
            kind="cylinder", name=f"{name}_{kind_name}", component="hardware",
            location=_pos(center, axis, along), rotation=rot,
            material_slot="hardware", params=params,
        )

    return [
        prim("shaft", mid, shaft_r, depth),
        prim("washer_h", span_hi + w_h / 2, w_r, w_h),
        prim("head", span_hi + w_h + head_h / 2, head_r, head_h, segments=6),
        prim("washer_n", span_lo - w_h / 2, w_r, w_h),
        prim("nut", span_lo - w_h - nut_h / 2, nut_r, nut_h, segments=6),
    ]


def _band_clamp(joint: int, vert: Primitive, center_z: float, axis_h: int) -> List[Primitive]:
    """Saddle band around a vertical pole with two side through-bolts —
    how mast arms / banner brackets attach to poles in the field."""
    r = _radius_at_z(vert, center_z) + 0.006
    cx, cy = vert.location[0], vert.location[1]
    prims = [
        Primitive(
            kind="cylinder", name=f"joint{joint}_band", component="hardware",
            location=(cx, cy, center_z), material_slot="hardware",
            params={"radius": r, "depth": 0.05},
        )
    ]
    perp_h = 1 - axis_h  # the other horizontal axis
    shaft_r = 0.005
    for i, side in enumerate((1.0, -1.0), start=1):
        center = [cx, cy, center_z]
        center[perp_h] += side * r * 0.85
        span_lo = center[axis_h] - r * 0.8
        span_hi = center[axis_h] + r * 0.8
        prims.extend(_bolt(joint, i, center, axis_h, shaft_r, span_lo, span_hi))
    return prims


#: C6: fastener sizing scales with the connection's tributary load tier —
#: derived from the larger joined member's bounding volume (heuristic
#: fabrication convention, not FEA).
LOAD_FACTOR = {"light": 0.75, "standard": 1.0, "heavy": 1.35}


def _load_class(pa: Primitive, pb: Primitive) -> str:
    def volume(p: Primitive) -> float:
        _, h = _aabb(p)
        return 8.0 * h[0] * h[1] * h[2]

    v = max(volume(pa), volume(pb))
    if v > 0.15:
        return "heavy"
    if v < 0.01:
        return "light"
    return "standard"


def _bolt_pattern(d1: float, d2: float, head_r: float) -> List[Tuple[float, float]]:
    """Bolt offsets on the joint face: 1 center bolt for small faces, a
    2-bolt row along a long face, a 4-bolt pattern for plate-like faces —
    all with real edge distances (>= 1.5d from the overlap edge)."""
    edge = 1.5 * head_r
    big1 = d1 >= 0.22 and d1 / 2 - 0.3 * d1 >= edge
    big2 = d2 >= 0.22 and d2 / 2 - 0.3 * d2 >= edge
    if big1 and big2:
        return [(-0.3 * d1, -0.3 * d2), (0.3 * d1, -0.3 * d2),
                (-0.3 * d1, 0.3 * d2), (0.3 * d1, 0.3 * d2)]
    if big1:
        return [(-0.3 * d1, 0.0), (0.3 * d1, 0.0)]
    if big2:
        return [(0.0, -0.3 * d2), (0.0, 0.3 * d2)]
    return [(0.0, 0.0)]


#: preset names treated as structural metal for fastener appropriateness.
METAL_PRESETS = {
    "galvanized_steel", "cast_iron", "brushed_aluminum",
    "powder_coat_black", "powder_coat_green",
}


def _is_soft(slot: str, spec) -> bool:
    """True when a member's material is non-metal (wood/concrete/lens). With
    no spec (direct test calls) everything is treated as metal, preserving
    the pre-material-awareness behavior."""
    if spec is None:
        return False
    from .base import material_preset_name

    return material_preset_name(spec, slot) not in METAL_PRESETS


def compute_hardware(prims: List[Primitive], spec=None) -> List[Primitive]:
    """Emit visible connection hardware at inter-component joints. Joints
    between two non-metal members (wood↔wood, wood↔concrete) get no bolts —
    real furniture uses concealed joinery, so a wooden table never sprouts
    the industrial anchor bolts a steel pole needs. Metal↔metal and
    metal↔wood joints (e.g. a bench's wood slat bolted to its steel frame)
    keep their fasteners."""
    boxes = [
        (p, *_aabb(p))
        for p in prims
        if p.component != "hardware" and not p.cut
    ]
    out: List[Primitive] = []
    seen: set = set()
    joint = 0

    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            pa, ca, ha = boxes[i]
            pb, cb, hb = boxes[j]
            if pa.component == pb.component:
                continue

            lo = [max(ca[k] - ha[k], cb[k] - hb[k]) for k in range(3)]
            hi = [min(ca[k] + ha[k], cb[k] + hb[k]) for k in range(3)]
            if any(hi[k] <= lo[k] for k in range(3)):
                continue  # parts don't actually touch

            if _is_soft(pa.material_slot, spec) and _is_soft(pb.material_slot, spec):
                continue  # non-structural joint — concealed joinery, no bolts

            center = [(lo[k] + hi[k]) / 2 for k in range(3)]
            key = tuple(round(c / GRID) for c in center)
            if key in seen:
                continue
            seen.add(key)

            axis = min(range(3), key=lambda k: hi[k] - lo[k])
            perp = [k for k in range(3) if k != axis]
            d1 = hi[perp[0]] - lo[perp[0]]
            d2 = hi[perp[1]] - lo[perp[1]]
            if min(d1, d2) < MIN_FACE:
                continue  # face too thin to drill — not a real joint

            joint += 1

            # horizontal round member meeting an upright pole -> band clamp
            upright = pa if _is_upright_round(pa) else pb if _is_upright_round(pb) else None
            other = pb if upright is pa else pa
            if (
                axis != 2
                and upright is not None
                and not _is_upright_round(other)
                and other.kind in ROUND_KINDS
            ):
                out.extend(_band_clamp(joint, upright, center[2], axis))
            else:
                factor = LOAD_FACTOR[_load_class(pa, pb)]
                shaft_r = min(max(0.22 * min(d1, d2) * factor, 0.004), 0.014)
                above = max(ca[axis] + ha[axis], cb[axis] + hb[axis]) - hi[axis]
                below = lo[axis] - min(ca[axis] - ha[axis], cb[axis] - hb[axis])
                span_hi = hi[axis] + min(above, EMBED)
                span_lo = lo[axis] - min(below, EMBED)
                for idx, (o1, o2) in enumerate(
                    _bolt_pattern(d1, d2, 1.8 * shaft_r), start=1
                ):
                    c = list(center)
                    c[perp[0]] += o1
                    c[perp[1]] += o2
                    out.extend(_bolt(joint, idx, c, axis, shaft_r, span_lo, span_hi))

            if joint >= MAX_JOINTS:
                return out
    return out
