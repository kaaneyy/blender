"""Standard fabrication connections (Part C) — pure primitive layer.

These helpers emit the joint details a fabricator would actually draw, so
builders stop placing bare cones on plates:

* :func:`ground_connection` (C1/C7) — how a vertical member meets the
  ground, in three correct-looking variants:
    - ``flange``   : grout pad + round base flange + anchor-bolt circle on a
                     real BCD (count by load class) + triangular gusset webs
                     + a weld bead where the pole lands on the flange (C3).
    - ``burial``   : direct-burial backfill collar flaring out of the grade.
    - ``embedded`` : cast concrete pier the member is embedded into.
* :func:`weld_fillet` (C3) — a revolved fillet ring where two round members
  are welded.

Mirrored 1:1 in frontend/src/builders/connections.ts. Sizing is heuristic
fabrication convention, not FEA — see the honest note in docs/BUILD_PLAN.md.
"""
from __future__ import annotations

import math
from typing import List

from .base import Primitive

#: anchor bolts on the circle, by connection load class (C6)
BOLT_COUNT = {"light": 4, "standard": 4, "heavy": 6}
BOLT_R = {"light": 0.008, "standard": 0.011, "heavy": 0.014}


def weld_fillet(radius: float, size: float, z: float, component: str,
                slot: str, name: str = "weld_bead") -> Primitive:
    """C3: a small revolved fillet ring around a round member at height z —
    the bead that sells 'welded'."""
    return Primitive(
        kind="lathe",
        name=name,
        component=component,
        location=(0.0, 0.0, z),
        material_slot=slot,
        params={
            "profile": (
                (radius + size, 0.0),
                (radius + size * 0.25, size * 0.15),
                (radius, size),
            ),
        },
    )


def ground_connection(
    pole_radius: float,
    mount: str = "flange",
    load_class: str = "standard",
    component: str = "base_plate",
    slot: str = "base",
) -> List[Primitive]:
    """C1/C7: the standard ground connection for a vertical round member of
    ``pole_radius`` at grade. Returns pure primitives; z=0 is grade."""
    if mount == "burial":
        # direct burial: backfill collar flaring out of the ground
        return [
            Primitive(
                kind="lathe", name="backfill_collar", component=component,
                location=(0.0, 0.0, 0.0), material_slot=slot,
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
                kind="cylinder", name="concrete_pier", component=component,
                location=(0.0, 0.0, pier_h / 2), material_slot=slot,
                params={"radius": pier_r, "depth": pier_h},
            ),
            weld_fillet(pole_radius, pole_radius * 0.35, pier_h,
                        component, slot, name="grout_ring"),
        ]

    # ------------------------------------------------------------------ flange
    n_bolts = BOLT_COUNT.get(load_class, 4)
    bolt_r = BOLT_R.get(load_class, 0.011)
    flange_r = max(pole_radius * 2.1, pole_radius + 0.09)
    flange_t = 0.028
    grout_t = 0.024
    flange_top = grout_t + flange_t
    bolt_circle_r = (pole_radius + flange_r) / 2 + 0.01

    prims: List[Primitive] = [
        Primitive(  # grout pad under the flange
            kind="cylinder", name="grout_pad", component=component,
            location=(0.0, 0.0, grout_t / 2), material_slot=slot,
            params={"radius": flange_r * 1.12, "depth": grout_t},
        ),
        Primitive(  # round base flange
            kind="cylinder", name="flange", component=component,
            location=(0.0, 0.0, grout_t + flange_t / 2), material_slot=slot,
            params={"radius": flange_r, "depth": flange_t},
        ),
        # C3: weld bead where the pole lands on the flange
        weld_fillet(pole_radius, max(0.012, pole_radius * 0.18), flange_top,
                    component, slot),
    ]

    # anchor-bolt circle on a real BCD, count/diameter by load class (C6)
    washer_t = 0.003
    nut_h = bolt_r * 1.1
    for i in range(n_bolts):
        a = 2.0 * math.pi * i / n_bolts
        x = bolt_circle_r * math.cos(a)
        y = bolt_circle_r * math.sin(a)
        proj = 0.03  # bolt projection above the flange
        shaft_depth = flange_top + proj
        prims.extend([
            Primitive(
                kind="cylinder", name=f"anchor_bolt_{i + 1}", component=component,
                location=(x, y, shaft_depth / 2), material_slot="hardware",
                params={"radius": bolt_r, "depth": shaft_depth},
            ),
            Primitive(
                kind="cylinder", name=f"anchor_washer_{i + 1}", component=component,
                location=(x, y, flange_top + washer_t / 2), material_slot="hardware",
                params={"radius": bolt_r * 2.2, "depth": washer_t},
            ),
            Primitive(
                kind="cylinder", name=f"anchor_nut_{i + 1}", component=component,
                location=(x, y, flange_top + washer_t + nut_h / 2),
                material_slot="hardware",
                params={"radius": bolt_r * 1.7, "depth": nut_h, "segments": 6},
            ),
        ])

    # triangular gusset webs between the pole and the flange edge, placed
    # between the bolts (loft wedge: tall at the pole, thin at the rim)
    gusset_h = max(0.08, pole_radius * 1.1)
    gusset_len = flange_r - pole_radius - 0.006
    mid_r = pole_radius + gusset_len / 2
    for i in range(n_bolts):
        a = 2.0 * math.pi * (i + 0.5) / n_bolts
        prims.append(
            Primitive(
                kind="loft", name=f"gusset_{i + 1}", component=component,
                location=(mid_r * math.cos(a), mid_r * math.sin(a),
                          flange_top + gusset_h / 2),
                # local Z -> radial: tilt Z onto X, then spin to the angle
                rotation=(0.0, math.pi / 2, a),
                material_slot=slot,
                params={
                    "depth": gusset_len,
                    "profile_start": {"shape": "rect", "w": gusset_h, "h": 0.008},
                    "profile_end": {"shape": "rect", "w": 0.016, "h": 0.008},
                },
            )
        )
    return prims
