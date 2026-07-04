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
from .connections import ground_connection

ARM_SEGMENTS = 6
ARM_RADIUS = 0.035  # m, mast-arm tube radius
FT = 0.3048
IN = 0.0254

DEFAULTS_M = {
    "pole_height": 30 * FT,
    "arm_length": 8 * FT,
    "pole_base_diameter": 8 * IN,
    "pole_top_diameter": 4 * IN,
}


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
    gusset_len = 0.16
    return [
        Primitive(
            kind="sweep",
            name="mast_arm",
            component="arm",
            location=(0.0, 0.0, 0.0),
            material_slot="pole",
            params={"path": path, "radius": ARM_RADIUS * 1.25, "radius_end": ARM_RADIUS * 0.8},
        ),
        Primitive(  # C2: telescoping slip-fitter collar wrapping the pole
            kind="tube",
            name="slipfitter",
            component="arm",
            location=(0.0, 0.0, attach_z + 0.02),
            material_slot="pole",
            params={"radius": pole_r_at_attach + 0.012, "wall": 0.006, "depth": 0.30},
        ),
        Primitive(  # C4: gusset wedge under the cantilever, tall at the pole
            kind="loft",
            name="arm_gusset",
            component="arm",
            location=(pole_r_at_attach + gusset_len / 2, 0.0, attach_z - 0.10),
            rotation=(0.0, math.pi / 2, 0.0),  # loft axis -> radial (+X)
            material_slot="pole",
            params={
                "depth": gusset_len,
                "profile_start": {"shape": "rect", "w": 0.16, "h": 0.008},
                "profile_end": {"shape": "rect", "w": 0.02, "h": 0.008},
            },
        ),
    ]


def _luminaire_primitives(arm_length: float, pole_height: float) -> List[Primitive]:
    """Cobra-head housing lofted from a rounded-rect door end to a slimmer
    elliptical nose (B4), hollow sheet-metal walls (A4 shell)."""
    tip_z = pole_height - 0.25  # top of the arm curve (attach_z + rise)
    head_len, head_w, head_h = 0.75, 0.32, 0.17
    head_x = arm_length + head_len / 2 - 0.15  # head overhangs the arm tip
    return [
        Primitive(
            kind="loft",
            name="head",
            component="luminaire",
            location=(head_x, 0.0, tip_z + head_h / 2 - 0.02),
            # loft axis is local Z; rotate it to run along +X (arm direction)
            rotation=(0.0, math.pi / 2, 0.0),
            material_slot="luminaire",
            params={
                "depth": head_len,
                "profile_start": {"shape": "rect", "w": head_w, "h": head_h},
                "profile_end": {"shape": "ellipse", "w": head_w * 0.7, "h": head_h * 0.6},
                "shell": 0.003,
            },
        ),
        Primitive(
            kind="cylinder",
            name="lens",
            component="luminaire",
            location=(head_x + 0.1, 0.0, tip_z - 0.03),
            material_slot="lens",
            params={"radius": 0.10, "depth": 0.02},
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
            params={"radius_bottom": base_r, "radius_top": top_r, "depth": pole_height},
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
