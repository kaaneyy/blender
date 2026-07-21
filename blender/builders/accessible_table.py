"""Accessible table builder — a flat top on legs whose knee/toe zone under
the accessible (front) side is open BY CONSTRUCTION, per ADA Standards for
Accessible Design 2010 Section 902 (dining/work surfaces) and Section 306
(knee/toe clearance). The DB ranges for ``surface_height``,
``knee_clearance_height`` and ``toe_clearance_depth`` live in
standards/us_codes.json under ``accessible_table``.

Only the pure primitive layer lives here; realization happens in base.build.
All dimensions are meters (spec values arrive in inches and are converted by
``spec_params``).

Design: two round legs are set back from the accessible (front, -Y) edge far
enough that the leg's own footprint never reaches into the ADA toe-clearance
strip along that edge, REGARDLESS of table_width or how narrow it is
narrowed — the guarantee comes from the legs' Y position, not from keeping
them apart in X, so it holds at any width. A single rear apron beam ties the
legs together for stiffness at that same set-back Y, so it never intrudes on
the clearance zone either. Legs interpenetrate the tabletop by PENETRATION
and each gets its own welded joint (C3, via connections.weld_fillet) at the
top's underside — the same "curated builder emits its own joint hardware"
pattern street_light uses for its ground connection.
"""
from __future__ import annotations

from typing import List

from .base import Primitive, register, spec_params
from .connections import weld_fillet

IN = 0.0254

#: round leg radius (m) — a substantial post, not a pencil leg
LEG_RADIUS = 0.025
#: default/minimum tabletop thickness (m) — ~1.5 in / ~0.75 in
TOP_THICKNESS_DEFAULT = 0.038
MIN_TOP_THICKNESS = 0.019
#: how far a leg is inset from the table's side (X) edges
LEG_INSET = 0.05
#: how far a leg center sits in from the REAR (non-accessible) edge — the
#: legs live back here so the whole front stays open regardless of width
LEG_BACK_INSET = 0.06
#: legs run up INTO the tabletop by this much (a real welded joint, not a
#: zero-thickness touch) — within the 10-20 mm fabrication convention
PENETRATION = 0.015
#: weld bead size as a fraction of the leg radius (min-clamped, mirrors the
#: proportions connections.ground_connection uses for its own weld_bead)
WELD_SIZE_RATIO = 0.3
MIN_WELD_SIZE = 0.006
#: rear apron beam (m): vertical height and depth (Y thickness)
APRON_HEIGHT = 0.05
APRON_DEPTH = 0.03

#: ADA-306.3.5 knee-clearance width (30 in) — the standards DB only carries
#: the height/depth entries for accessible_table, so this is a builder
#: constant (also the number the tests build the clear-zone box from).
KNEE_CLEARANCE_WIDTH = 0.762

DEFAULTS_M = {
    "surface_height": 30 * IN,
    "knee_clearance_height": 27 * IN,
    "toe_clearance_depth": 17 * IN,
    "table_width": 60 * IN,
    "table_depth": 30 * IN,
}


@register("accessible_table")
def compute_primitives(spec: dict) -> List[Primitive]:
    p = spec_params(spec)

    surface_height = p.get("surface_height", DEFAULTS_M["surface_height"])
    knee_clearance_height = p.get("knee_clearance_height", DEFAULTS_M["knee_clearance_height"])
    table_width = p.get("table_width", DEFAULTS_M["table_width"])
    table_depth = p.get("table_depth", DEFAULTS_M["table_depth"])

    # Resolve the tension between a thin top and a low surface_height: never
    # let the underside drop below knee_clearance_height. If the requested
    # surface_height doesn't leave room for even the minimum top thickness
    # above the knee floor, raise the EFFECTIVE surface height instead of
    # violating clearance (a thin top is preferred first; raising the top
    # only kicks in once thinning bottoms out at MIN_TOP_THICKNESS).
    eff_surface_height = max(surface_height, knee_clearance_height + MIN_TOP_THICKNESS)
    top_thickness = min(TOP_THICKNESS_DEFAULT, eff_surface_height - knee_clearance_height)
    top_thickness = max(top_thickness, MIN_TOP_THICKNESS)
    top_underside = eff_surface_height - top_thickness

    half_w = table_width / 2
    half_d = table_depth / 2
    leg_x = half_w - LEG_INSET
    leg_y = half_d - LEG_BACK_INSET  # set back near the rear edge, not the accessible front
    leg_depth = top_underside + PENETRATION
    leg_z = leg_depth / 2

    prims: List[Primitive] = []

    # tabletop --------------------------------------------------------------
    prims.append(
        Primitive(
            kind="box",
            name="top",
            component="top",
            location=(0.0, 0.0, eff_surface_height - top_thickness / 2),
            material_slot="top",
            params={"size": (table_width, table_depth, top_thickness)},
        )
    )

    # legs + their own welded joint at the top underside ---------------------
    leg_positions = [(-leg_x, leg_y), (leg_x, leg_y)]
    weld_size = max(MIN_WELD_SIZE, LEG_RADIUS * WELD_SIZE_RATIO)
    for i, (lx, ly) in enumerate(leg_positions, start=1):
        name = f"leg{i}"
        prims.append(
            Primitive(
                kind="cylinder",
                name=name,
                component="legs",
                location=(lx, ly, leg_z),
                material_slot="frame",
                params={"radius": LEG_RADIUS, "depth": leg_depth},
            )
        )
        prims.append(
            weld_fillet(
                LEG_RADIUS, weld_size, top_underside, "legs", "frame",
                name=f"{name}_weld_bead", center=(lx, ly),
            )
        )

    # rear apron beam tying the legs together — same Y as the legs, so it
    # never reaches into the accessible front's knee/toe zone -------------
    apron_span = 2 * leg_x
    prims.append(
        Primitive(
            kind="box",
            name="apron_back",
            component="apron",
            location=(0.0, leg_y, top_underside - APRON_HEIGHT / 2),
            material_slot="frame",
            params={"size": (apron_span, APRON_DEPTH, APRON_HEIGHT)},
        )
    )

    return prims
