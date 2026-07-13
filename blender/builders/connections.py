"""Standard fabrication connections — pure primitive emitter library.

These helpers emit the joint details a fabricator would actually draw. The
orchestrator in :mod:`hardware` detects/matches joints and dispatches here;
curated builders may also call these directly (street_light's ground mount).

* :func:`ground_connection` (C1/C7) — how a vertical member meets the
  ground: ``flange`` (grout pad + base flange + anchor-bolt circle + weld
  bead + gusset webs), ``burial`` (backfill collar), ``embedded`` (concrete
  pier). Round and square members supported; placeable at any (x, y).
* :func:`weld_fillet` (C3) — a revolved fillet ring where two round members
  are welded.
* :func:`through_bolt_assembly` — shaft + washers + hex head/nut spanning a
  joint.
* :func:`carriage_bolt_assembly` — dome head on the timber face (no washer),
  washer + hex nut on the far face; how wood decks bolt to steel frames.
* :func:`lag_screw_assembly` — hex head + washer one side, threaded shank
  embedded in the far member (no nut).
* :func:`slip_fitter` — collar + radial set screws; how post-top luminaires
  grip a pole tenon.
* :func:`split_band_clamp` — two-piece saddle band with ear tabs bolted
  through the ears (not through the pole); how arms clamp to poles on site.
* :func:`flange_splice` — two mating discs + a bolt circle; how pole
  sections join end-to-end.

Mirrored 1:1 in frontend/src/builders/connections.ts. Sizing is heuristic
fabrication convention, not FEA — see the honest note in docs/BUILD_PLAN.md.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from .base import Primitive

#: anchor bolts on the circle, by connection load class (C6)
BOLT_COUNT = {"light": 4, "standard": 4, "heavy": 6}
BOLT_R = {"light": 0.008, "standard": 0.011, "heavy": 0.014}

#: bolt axis -> rotation that maps a Z-axis cylinder onto that axis
AXIS_ROT = {
    0: (0.0, math.pi / 2, 0.0),  # X
    1: (math.pi / 2, 0.0, 0.0),  # Y
    2: (0.0, 0.0, 0.0),          # Z
}


def _pos(center: Sequence[float], axis: int, along: float) -> Tuple[float, float, float]:
    out = list(center)
    out[axis] = along
    return tuple(out)


def gusset_plate(
    name: str,
    component: str,
    slot: str,
    center_xy: Tuple[float, float],
    angle: float,
    attach_r: float,
    reach_r: float,
    flush_z: float,
    hug: str = "bottom",
    height: float = 0.1,
    tip_ratio: float = 0.35,
    thickness: float = 0.008,
) -> List[Primitive]:
    """A triangular stiffener plate in the vertical plane through ``angle``,
    radiating from a member of radius ``attach_r`` at ``center_xy`` out to
    ``reach_r``.

    Lofts bridge two centered cross-sections, so a plain tapered loft is a
    symmetric wedge whose edges both slope — the "floating arrowhead" look.
    This helper tilts the wedge by half its taper angle so ONE long edge
    lies perfectly flat, and shifts it so the raked tall edge is buried
    inside the member (the visible junction is a clean weld line):

    * ``hug="bottom"`` — bottom edge flat ON ``flush_z`` (base-plate gusset:
      sits flush on the flange, hypotenuse slopes down toward the rim).
    * ``hug="top"`` — top edge flat AT ``flush_z`` (knee brace under an arm:
      hugs the arm's underside, hypotenuse slopes up from the pole).

    Returns [] when the radial run is too short for a plate."""
    w0 = height
    w1 = max(tip_ratio * height, 0.012)
    taper = (w0 - w1) / 2.0
    run = reach_r - attach_r
    if run < 0.02 or height <= 0.0:
        return []
    # solve the plate run d and tilt t so the far tip lands at reach_r with
    # the flush edge level (fixed point; 4 rounds converge well under 0.1mm)
    d = run
    t = 0.0
    for _ in range(4):
        t = math.atan2(taper, d)
        d = (run + math.sin(t) * taper) / math.cos(t)
    s = 1.0 if hug == "bottom" else -1.0
    loc_r = attach_r + math.cos(t) * d / 2 - math.sin(t) * w0 / 2
    loc_z = flush_z + s * (math.cos(t) * w0 / 2 - math.sin(t) * d / 2)
    cx, cy = center_xy
    return [
        Primitive(
            kind="loft", name=name, component=component,
            location=(cx + loc_r * math.cos(angle),
                      cy + loc_r * math.sin(angle), loc_z),
            # local Z -> radial (tilted by the half-taper), spun to the angle
            rotation=(0.0, math.pi / 2 + s * t, angle),
            material_slot=slot,
            params={
                "depth": d,
                "profile_start": {"shape": "rect", "w": w0, "h": thickness},
                "profile_end": {"shape": "rect", "w": w1, "h": thickness},
            },
        )
    ]


def weld_fillet(radius: float, size: float, z: float, component: str,
                slot: str, name: str = "weld_bead",
                center: Tuple[float, float] = (0.0, 0.0)) -> Primitive:
    """C3: a small revolved fillet ring around a round member at height z —
    the bead that sells 'welded'. ``center`` is the member's (x, y)."""
    return Primitive(
        kind="lathe",
        name=name,
        component=component,
        location=(center[0], center[1], z),
        material_slot=slot,
        params={
            "profile": (
                (radius + size, 0.0),
                (radius + size * 0.25, size * 0.15),
                (radius, size),
            ),
        },
    )


def through_bolt_assembly(joint: int, idx: int, center: Sequence[float], axis: int,
                          shaft_r: float, span_lo: float, span_hi: float) -> List[Primitive]:
    """Through-bolt: washer+hex head at span_hi, washer+hex nut at span_lo."""
    rot = AXIS_ROT[axis]
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


def carriage_bolt_assembly(joint: int, idx: int, center: Sequence[float], axis: int,
                           shaft_r: float, span_lo: float, span_hi: float,
                           dome_at_hi: bool = True) -> List[Primitive]:
    """Carriage bolt: smooth dome head half-proud of the timber face (no
    washer under it — the square shank grips the wood), flat washer + hex
    nut on the opposite (steel) face."""
    rot = AXIS_ROT[axis]
    dome_r = 1.6 * shaft_r
    nut_r = 1.6 * shaft_r
    nut_h = max(shaft_r, 0.003)
    w_r = 2.2 * shaft_r
    w_h = 0.002
    depth = max(span_hi - span_lo, 0.012)
    mid = (span_lo + span_hi) / 2
    name = f"joint{joint}_bolt{idx}"
    dome_end, nut_end, nut_dir = (
        (span_hi, span_lo, -1.0) if dome_at_hi else (span_lo, span_hi, 1.0)
    )

    def cyl(kind_name: str, along: float, radius: float, d: float, segments=None):
        params = {"radius": radius, "depth": d}
        if segments:
            params["segments"] = segments
        return Primitive(
            kind="cylinder", name=f"{name}_{kind_name}", component="hardware",
            location=_pos(center, axis, along), rotation=rot,
            material_slot="hardware", params=params,
        )

    return [
        cyl("shaft", mid, shaft_r, depth),
        Primitive(
            kind="sphere", name=f"{name}_dome", component="hardware",
            location=_pos(center, axis, dome_end), rotation=rot,
            material_slot="hardware", params={"radius": dome_r},
        ),
        cyl("washer_n", nut_end + nut_dir * w_h / 2, w_r, w_h),
        cyl("nut", nut_end + nut_dir * (w_h + nut_h / 2), nut_r, nut_h, segments=6),
    ]


def lag_screw_assembly(joint: int, idx: int, center: Sequence[float], axis: int,
                       shaft_r: float, span_lo: float, span_hi: float) -> List[Primitive]:
    """Lag screw: hex head + washer at span_hi, threaded shank embedded in
    the far member — no nut on the blind side."""
    rot = AXIS_ROT[axis]
    head_r = 1.8 * shaft_r
    head_h = max(1.2 * shaft_r, 0.004)
    w_r = 2.2 * shaft_r
    w_h = 0.002
    depth = max(span_hi - span_lo, 0.012)
    mid = (span_lo + span_hi) / 2
    name = f"joint{joint}_bolt{idx}"

    def cyl(kind_name: str, along: float, radius: float, d: float, segments=None):
        params = {"radius": radius, "depth": d}
        if segments:
            params["segments"] = segments
        return Primitive(
            kind="cylinder", name=f"{name}_{kind_name}", component="hardware",
            location=_pos(center, axis, along), rotation=rot,
            material_slot="hardware", params=params,
        )

    return [
        cyl("shaft", mid, shaft_r, depth),
        cyl("washer_h", span_hi + w_h / 2, w_r, w_h),
        cyl("head", span_hi + w_h + head_h / 2, head_r, head_h, segments=6),
    ]


def slip_fitter(joint: int, outer_r: float, center_xy: Tuple[float, float],
                center_z: float, screw_r: float = 0.004) -> List[Primitive]:
    """Slip-fitter: a collar gripping a round-over-round telescoping fit
    (post-top luminaire over a pole tenon) with 3 radial set screws at 120°.
    ``outer_r`` is the outer member's radius at the fit; ``screw_r`` is the
    set-screw shaft radius — the orchestrator passes a catalog size scaled
    to the fit so the drawn screw is the scheduled screw (the default
    reproduces the original fixed M8)."""
    cx, cy = center_xy
    collar_r = outer_r + 0.004
    collar_d = min(max(1.2 * outer_r, 0.04), 0.12)
    prims = [
        Primitive(
            kind="tube", name=f"joint{joint}_fitter", component="hardware",
            location=(cx, cy, center_z), material_slot="hardware",
            params={"radius": collar_r, "wall": 0.004, "depth": collar_d},
        )
    ]
    screw_len = max(0.03, 7.5 * screw_r)
    head_h = 1.25 * screw_r
    for i in range(3):
        a = 2.0 * math.pi * i / 3.0
        # radial screw: local Z tilted onto X (pi/2 about Y), spun to angle a
        mid_r = collar_r + screw_len / 2 - 0.012  # tip embedded 12mm into the fit
        prims.append(
            Primitive(
                kind="cylinder", name=f"joint{joint}_setscrew{i + 1}",
                component="hardware",
                location=(cx + mid_r * math.cos(a), cy + mid_r * math.sin(a), center_z),
                rotation=(0.0, math.pi / 2, a), material_slot="hardware",
                params={"radius": screw_r, "depth": screw_len},
            )
        )
        head_r_dist = collar_r + screw_len - 0.012 + head_h / 2
        prims.append(
            Primitive(
                kind="cylinder", name=f"joint{joint}_setscrew{i + 1}_head",
                component="hardware",
                location=(cx + head_r_dist * math.cos(a),
                          cy + head_r_dist * math.sin(a), center_z),
                rotation=(0.0, math.pi / 2, a), material_slot="hardware",
                params={"radius": 1.75 * screw_r, "depth": head_h, "segments": 6},
            )
        )
    return prims


def split_band_clamp(joint: int, pole_r: float, center_xy: Tuple[float, float],
                     center_z: float, axis_h: int, arm_r: float,
                     shaft_r: Optional[float] = None) -> List[Primitive]:
    """Two-piece saddle band: a split band wraps the pole, ear tabs protrude
    on both sides perpendicular to the arm, and one bolt per ear pair clamps
    the halves together — through the EARS, not through the pole. Band width
    and bolt size scale with the clamped arm; the orchestrator passes its
    catalog-snapped ``shaft_r`` so the drawn ear bolts match the scheduled
    fastener (None falls back to the raw un-snapped formula)."""
    cx, cy = center_xy
    band_r = pole_r + 0.006
    band_w = min(max(3.0 * arm_r, 0.03), 0.08)
    prims = [
        Primitive(
            kind="tube", name=f"joint{joint}_band", component="hardware",
            location=(cx, cy, center_z), material_slot="hardware",
            params={"radius": band_r, "wall": 0.003, "depth": band_w},
        )
    ]
    perp_h = 1 - axis_h  # the other horizontal axis: where the ears live
    ear_len = 0.025
    ear_thick = 0.024  # the two mating tabs, modeled as one block
    if shaft_r is None:
        shaft_r = min(max(0.4 * arm_r, 0.004), 0.008)
    for i, side in enumerate((1.0, -1.0), start=1):
        ear_center = [cx, cy, center_z]
        ear_center[perp_h] += side * (band_r + ear_len / 2)
        size = [0.0, 0.0, 0.0]
        size[axis_h] = ear_thick
        size[perp_h] = ear_len
        size[2] = band_w * 0.8
        prims.append(
            Primitive(
                kind="box", name=f"joint{joint}_ear{i}", component="hardware",
                location=tuple(ear_center), material_slot="hardware",
                params={"size": tuple(size)},
            )
        )
        span_lo = ear_center[axis_h] - ear_thick / 2 - 0.002
        span_hi = ear_center[axis_h] + ear_thick / 2 + 0.002
        prims.extend(
            through_bolt_assembly(joint, i, ear_center, axis_h, shaft_r,
                                  span_lo, span_hi)
        )
    return prims


def flange_splice(joint: int, center: Sequence[float], axis: int,
                  member_r: float, n_bolts: int = 6,
                  shaft_r: Optional[float] = None) -> List[Primitive]:
    """Bolted flange splice: two mating discs at the joint plane with a bolt
    circle through both — how pole/mast sections join end-to-end. The
    orchestrator passes its catalog-snapped ``shaft_r`` so the drawn bolts
    match the scheduled fastener (None falls back to the raw formula)."""
    rot = AXIS_ROT[axis]
    disc_r = max(member_r * 1.6, member_r + 0.03)
    disc_t = 0.010
    bcr = (member_r + disc_r) / 2
    if shaft_r is None:
        shaft_r = min(max(0.35 * member_r, 0.005), 0.012)
    prims = []
    for i, side in enumerate((-1.0, 1.0), start=1):
        prims.append(
            Primitive(
                kind="cylinder", name=f"joint{joint}_flange{i}", component="hardware",
                location=_pos(center, axis, center[axis] + side * disc_t / 2),
                rotation=rot, material_slot="hardware",
                params={"radius": disc_r, "depth": disc_t},
            )
        )
    # perpendicular directions spanning the disc plane
    perp = [k for k in range(3) if k != axis]
    for i in range(n_bolts):
        a = 2.0 * math.pi * i / n_bolts
        c = list(center)
        c[perp[0]] += bcr * math.cos(a)
        c[perp[1]] += bcr * math.sin(a)
        span_lo = center[axis] - disc_t - 0.002
        span_hi = center[axis] + disc_t + 0.002
        prims.extend(
            through_bolt_assembly(joint, i + 1, c, axis, shaft_r, span_lo, span_hi)
        )
    return prims


def ground_connection(
    pole_radius: float,
    mount: str = "flange",
    load_class: str = "standard",
    component: str = "base_plate",
    slot: str = "base",
    center: Tuple[float, float] = (0.0, 0.0),
    shape: str = "round",
    name_prefix: str = "",
) -> List[Primitive]:
    """C1/C7: the standard ground connection for a vertical member of
    ``pole_radius`` (half the max horizontal extent for square posts) at
    grade. ``center`` is the member's (x, y); ``shape`` is "round" or
    "square" (square posts get a box plate with corner anchor bolts and no
    weld ring). Returns pure primitives; z=0 is grade. Defaults reproduce
    the original street_light flange byte-for-byte."""
    cx, cy = center
    n = name_prefix

    if mount == "burial":
        # direct burial: backfill collar flaring out of the ground
        return [
            Primitive(
                kind="lathe", name=f"{n}backfill_collar", component=component,
                location=(cx, cy, 0.0), material_slot=slot,
                params={"profile": "flared_base",
                        "radius": pole_radius * 2.0,
                        "depth": max(0.10, pole_radius * 1.4)},
            )
        ]
    if mount == "embedded":
        # embedded base: exposed cast concrete pier with a chamfered top
        pier_r = pole_radius * 2.6
        pier_h = max(0.15, pole_radius * 2.0)
        return [
            Primitive(
                kind="cylinder", name=f"{n}concrete_pier", component=component,
                location=(cx, cy, pier_h / 2), material_slot=slot,
                params={"radius": pier_r, "depth": pier_h},
            ),
            weld_fillet(pole_radius, pole_radius * 0.35, pier_h,
                        component, slot, name=f"{n}grout_ring", center=center),
        ]

    # ------------------------------------------------------------------ flange
    n_bolts = BOLT_COUNT.get(load_class, 4)
    bolt_r = BOLT_R.get(load_class, 0.011)
    flange_r = max(pole_radius * 2.1, pole_radius + 0.09)
    flange_t = 0.028
    grout_t = 0.024
    flange_top = grout_t + flange_t
    square = shape == "square"

    prims: List[Primitive] = []
    if square:
        side = 2.0 * flange_r
        prims.append(Primitive(  # grout pad under the plate
            kind="box", name=f"{n}grout_pad", component=component,
            location=(cx, cy, grout_t / 2), material_slot=slot,
            params={"size": (side * 1.12, side * 1.12, grout_t)},
        ))
        prims.append(Primitive(  # square base plate
            kind="box", name=f"{n}flange", component=component,
            location=(cx, cy, grout_t + flange_t / 2), material_slot=slot,
            params={"size": (side, side, flange_t)},
        ))
    else:
        prims.append(Primitive(  # grout pad under the flange
            kind="cylinder", name=f"{n}grout_pad", component=component,
            location=(cx, cy, grout_t / 2), material_slot=slot,
            params={"radius": flange_r * 1.12, "depth": grout_t},
        ))
        prims.append(Primitive(  # round base flange
            kind="cylinder", name=f"{n}flange", component=component,
            location=(cx, cy, grout_t + flange_t / 2), material_slot=slot,
            params={"radius": flange_r, "depth": flange_t},
        ))
        # C3: weld bead where the pole lands on the flange (round only)
        prims.append(
            weld_fillet(pole_radius, max(0.012, pole_radius * 0.18), flange_top,
                        component, slot, name=f"{n}weld_bead", center=center)
        )

    # anchor bolts: circle on a real BCD for round; corner pattern for square
    washer_t = 0.003
    nut_h = bolt_r * 1.1
    if square:
        inset = max(0.02, 3.0 * bolt_r)
        d = flange_r - inset
        anchor_xy = [(cx + sx * d, cy + sy * d)
                     for sx, sy in ((1, 1), (-1, 1), (-1, -1), (1, -1))]
    else:
        bolt_circle_r = (pole_radius + flange_r) / 2 + 0.01
        anchor_xy = [
            (cx + bolt_circle_r * math.cos(2.0 * math.pi * i / n_bolts),
             cy + bolt_circle_r * math.sin(2.0 * math.pi * i / n_bolts))
            for i in range(n_bolts)
        ]
    for i, (x, y) in enumerate(anchor_xy):
        proj = 0.03  # bolt projection above the flange
        shaft_depth = flange_top + proj
        prims.extend([
            Primitive(
                kind="cylinder", name=f"{n}anchor_bolt_{i + 1}", component=component,
                location=(x, y, shaft_depth / 2), material_slot="hardware",
                params={"radius": bolt_r, "depth": shaft_depth},
            ),
            Primitive(
                kind="cylinder", name=f"{n}anchor_washer_{i + 1}", component=component,
                location=(x, y, flange_top + washer_t / 2), material_slot="hardware",
                params={"radius": bolt_r * 2.2, "depth": washer_t},
            ),
            Primitive(
                kind="cylinder", name=f"{n}anchor_nut_{i + 1}", component=component,
                location=(x, y, flange_top + washer_t + nut_h / 2),
                material_slot="hardware",
                params={"radius": bolt_r * 1.7, "depth": nut_h, "segments": 6},
            ),
        ])

    # triangular gusset webs between the member and the plate edge — flat on
    # the flange, tall edge buried in the member, hypotenuse down to the rim
    # (between the bolts for round plates, at face midpoints for square ones)
    gusset_h = max(0.08, pole_radius * 1.1)
    gusset_angles = (
        [2.0 * math.pi * i / 4 for i in range(4)] if square
        else [2.0 * math.pi * (i + 0.5) / n_bolts for i in range(n_bolts)]
    )
    for i, a in enumerate(gusset_angles):
        prims.extend(gusset_plate(
            f"{n}gusset_{i + 1}", component, slot, center, a,
            attach_r=pole_radius, reach_r=flange_r - 0.004,
            flush_z=flange_top, hug="bottom", height=gusset_h,
        ))
    return prims
