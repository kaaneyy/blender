"""Street light builder (first T3.2 builder): tapered pole, curved mast arm,
cobra-head luminaire, base plate, plus anchor-bolt / double-arm /
banner-bracket toggles.

Only the pure primitive layer lives here; realization happens in base.build.
All dimensions are meters (spec values arrive in ft/in and are converted by
``spec_params``).
"""
from __future__ import annotations

import math
from typing import List

from .base import Primitive, mirror_x, register, spec_params, spec_selects, spec_toggles
from .connections import ground_connection, gusset_plate

ARM_SEGMENTS = 6
ARM_RADIUS = 0.035  # m, mast-arm tube radius
FT = 0.3048
IN = 0.0254

#: cobra-head housing (m): length along the arm, width across it, height at
#: the door end. The lens is DERIVED from these (see _luminaire_primitives),
#: so resizing the housing keeps the light fitting its casing.
HEAD_LEN = 0.75
HEAD_W = 0.32
HEAD_H = 0.17
#: door-to-nose shrink factors of the lofted housing (width, height)
NOSE_W = 0.7
NOSE_H = 0.6
#: where the drop lens sits along the housing (0 = door end, 1 = nose)
LENS_STATION = 0.63
#: lens disc thickness and how far its top tucks up into the housing
LENS_DEPTH = 0.02
LENS_RECESS = 0.008

DEFAULTS_M = {
    "pole_height": 30 * FT,
    "arm_length": 8 * FT,
    "pole_base_diameter": 8 * IN,
    "pole_top_diameter": 4 * IN,
}

#: Parameter/toggle/select ids this builder's geometry actually reads
#: (spec_params/spec_toggles/spec_selects below) — the EXACT vocabulary,
#: nothing more. Kept in exact sync with backend/app/spec_ai.py's
#: BUILTIN_BUILDERS entry (ids only, unit suffixes stripped — test-enforced)
#: and consumed by connectivity.check_dead_controls to flag any OTHER
#: parameter/toggle a spec invents for asset_type "street_light" as
#: geometry-inert (a slider/toggle the UI shows but that drives nothing).
CONSUMED_PARAMS = ("pole_height", "arm_length", "pole_base_diameter", "pole_top_diameter")
CONSUMED_TOGGLES = ("double_arm", "banner_bracket")
CONSUMED_SELECTS = {"mounting": ("flange", "burial", "embedded")}


def _arm_points(arm_length: float, attach_z: float, rise: float):
    """Points along the mast arm: starts at the pole, rises quadratically to
    the tip so the luminaire sits at the highest point."""
    pts = []
    for i in range(ARM_SEGMENTS + 1):
        t = i / ARM_SEGMENTS
        pts.append((t * arm_length, attach_z + rise * t * t))
    return pts


def _arm_primitives(arm_length: float, pole_height: float,
                    pole_r_at_attach: float) -> List[Primitive]:
    """One swept, tapered tube (B2), mounted with a slip-fitter collar (C2)
    and a gusset plate at the pole (C4) — the sweep stays FIRST so the
    hardware pass band-clamps the arm itself."""
    rise = min(0.15 * arm_length, 0.75)
    attach_z = pole_height - 0.25 - rise  # arm meets the pole just below the top
    path = tuple((x, 0.0, z) for x, z in _arm_points(arm_length, attach_z, rise))
    return [
        Primitive(
            kind="sweep",
            name="mast_arm",
            component="arm",
            location=(0.0, 0.0, 0.0),
            material_slot="pole",
            # mast arms are hollow tube like the pole (see the shaft's
            # `shell`): outer profile unchanged, real-world mass.
            params={"path": path, "radius": ARM_RADIUS * 1.25,
                    "radius_end": ARM_RADIUS * 0.8, "shell": 0.0048},
        ),
        Primitive(  # C2: telescoping slip-fitter collar wrapping the pole
            kind="tube",
            name="slipfitter",
            component="arm",
            location=(0.0, 0.0, attach_z + 0.02),
            material_slot="pole",
            params={"radius": pole_r_at_attach + 0.012, "wall": 0.006, "depth": 0.30},
        ),
        # C4: knee brace under the cantilever — top edge hugging the arm's
        # underside, tall edge buried in the pole, hypotenuse below
        *gusset_plate(
            "arm_gusset", "arm", "pole", (0.0, 0.0), 0.0,
            attach_r=pole_r_at_attach, reach_r=pole_r_at_attach + 0.16,
            flush_z=attach_z - 0.02, hug="top", height=0.16,
        ),
    ]


def _luminaire_primitives(arm_length: float, pole_height: float) -> List[Primitive]:
    """Cobra-head housing lofted from a rounded-rect door end to a slimmer
    elliptical nose (B4), hollow sheet-metal walls (A4 shell), plus the
    drop lens seated in its underside.

    Under the (0, pi/2, 0) roll that lays the loft axis along +X, the
    profiles' local X (their ``w``) spans world Z and local Y (``h``) spans
    world Y — so ``w`` carries the housing HEIGHT and ``h`` its WIDTH, the
    same convention gusset_plate uses. Writing the profiles the other way
    around once rendered the head 0.32 m tall by 0.17 m wide with the
    0.20 m lens poking out both sides of its casing.

    The lens is derived from the housing cross-section at its own station
    (the loft bridges its two rings linearly, so width and height
    interpolate linearly along the axis): its radius clears the shell walls
    and its top tucks LENS_RECESS up into the housing while the disc face
    stays proud below the door — the light fits the casing by construction
    at any housing size."""
    tip_z = pole_height - 0.25  # top of the arm curve (attach_z + rise)
    head_x = arm_length + HEAD_LEN / 2 - 0.15  # head overhangs the arm tip
    head_z = tip_z + HEAD_H / 2 - 0.02  # door-end underside just below the tip
    # housing cross-section at the lens station
    width = HEAD_W * (1.0 + (NOSE_W - 1.0) * LENS_STATION)
    height = HEAD_H * (1.0 + (NOSE_H - 1.0) * LENS_STATION)
    lens_r = 0.38 * width  # clears the shell walls on both sides
    return [
        Primitive(
            kind="loft",
            name="head",
            component="luminaire",
            location=(head_x, 0.0, head_z),
            # loft axis is local Z; rotate it to run along +X (arm direction)
            rotation=(0.0, math.pi / 2, 0.0),
            material_slot="luminaire",
            params={
                "depth": HEAD_LEN,
                # w = height, h = width (world axes under the roll — see above)
                "profile_start": {"shape": "rect", "w": HEAD_H, "h": HEAD_W},
                "profile_end": {"shape": "ellipse", "w": HEAD_H * NOSE_H,
                                "h": HEAD_W * NOSE_W},
                "shell": 0.003,
            },
        ),
        Primitive(
            kind="cylinder",
            name="lens",
            component="luminaire",
            location=(head_x + (LENS_STATION - 0.5) * HEAD_LEN, 0.0,
                      head_z - height / 2 + LENS_RECESS - LENS_DEPTH / 2),
            material_slot="lens",
            params={"radius": lens_r, "depth": LENS_DEPTH},
        ),
    ]


@register("street_light")
def compute_primitives(spec: dict) -> List[Primitive]:
    p = spec_params(spec)
    toggles = spec_toggles(spec)
    selects = spec_selects(spec)

    pole_height = p.get("pole_height", DEFAULTS_M["pole_height"])
    arm_length = p.get("arm_length", DEFAULTS_M["arm_length"])
    base_r = p.get("pole_base_diameter", DEFAULTS_M["pole_base_diameter"]) / 2
    top_r = p.get("pole_top_diameter", DEFAULTS_M["pole_top_diameter"]) / 2

    prims: List[Primitive] = []

    # C1/C7: engineered ground connection (flange / burial / embedded)
    mount = selects.get("mounting", "flange")
    prims.extend(
        ground_connection(base_r, mount=mount, load_class="standard",
                          component="base_plate", slot="base")
    )

    # tapered pole ---------------------------------------------------------
    prims.append(
        Primitive(
            kind="cone",
            name="shaft",
            component="pole",
            location=(0.0, 0.0, pole_height / 2),
            material_slot="pole",
            # a real tapered steel pole is HOLLOW — 3/16 in wall. `shell`
            # is a Blender solidify, so the outer profile (and every preview
            # dimension) is unchanged; it only makes the pole weigh what a
            # pole weighs instead of a solid billet.
            params={"radius_bottom": base_r, "radius_top": top_r,
                    "depth": pole_height, "shell": 0.0048},
        )
    )
    prims.append(
        Primitive(
            kind="lathe",
            name="cap",
            component="pole",
            location=(0.0, 0.0, pole_height - 0.01),
            material_slot="pole",
            params={"profile": "dome", "radius": top_r * 1.25, "depth": top_r * 1.6},
        )
    )

    # mast arm + luminaire (mirrored when double_arm is on) -----------------
    rise = min(0.15 * arm_length, 0.75)
    attach_z = pole_height - 0.25 - rise
    pole_r_at_attach = base_r + (top_r - base_r) * min(1.0, attach_z / pole_height)
    arm_side = _arm_primitives(arm_length, pole_height, pole_r_at_attach) + \
        _luminaire_primitives(arm_length, pole_height)
    prims.extend(arm_side)
    if toggles.get("double_arm", False):
        prims.extend(mirror_x(arm_side, suffix="_b"))

    # banner bracket: two horizontal pins reaching out over the sidewalk ----
    if toggles.get("banner_bracket", False):
        bracket_len = 0.9
        lower_z = min(0.45 * pole_height, 3.6)
        for name, z in (("bracket_lower", lower_z), ("bracket_upper", lower_z + 1.5)):
            prims.append(
                Primitive(
                    kind="cylinder",
                    name=name,
                    component="banner_bracket",
                    location=(0.0, bracket_len / 2, z),
                    rotation=(math.pi / 2, 0.0, 0.0),  # axis along +Y
                    material_slot="pole",
                    params={"radius": 0.016, "depth": bracket_len},
                )
            )

    return prims
