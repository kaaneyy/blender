"""Material takeoff: what the thing actually weighs.

A fabrication drawing states a weight ("APPROXIMATE WEIGHT: 34.6 LB") because
it drives shipping, handling, and anchorage. This module computes that from
the built primitives: real per-kind volume (hollow where the part is hollow)
times the material density implied by each part's material slot.

Deliberately approximate, exactly like the drawings it mirrors:

* bevels, welds, and fillets are ignored — they are finish, not mass;
* drilled holes (``cut=True`` negative space) are NOT subtracted; they remove
  a fraction of a percent and the cutters are authored over-long so they
  punch cleanly, which would over-subtract;
* swept and lofted members use the standard prismatic approximations below.

Mirrored verbatim in ``frontend/src/builders/takeoff.ts`` — see CLAUDE.md.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

from .base import Primitive
from .shapes import resolve_profile

#: kg per cubic metre, by material family. Steel/iron dominate these assets;
#: the aluminium and stainless spreads are close enough that one metal figure
#: keeps the estimate honest without pretending to alloy-level precision.
DENSITY_KG_M3 = {
    "metal": 7850.0,
    "concrete": 2400.0,
    "wood": 600.0,
    "plastic": 1200.0,
    "glass": 2500.0,
    "other": 1000.0,
}

KG_PER_LB = 0.45359237

#: preset name → material family. Anything unknown is assumed metal, which is
#: the conservative (heavier) read for a street-furniture asset.
_FAMILY_BY_PRESET = {
    "galvanized_steel": "metal",
    "cast_iron": "metal",
    "brushed_aluminum": "metal",
    "powder_coat_black": "metal",
    "powder_coat_green": "metal",
    "stainless": "metal",
    "concrete": "concrete",
    "wood_slat": "wood",
    "lamp_lens": "glass",
    "glass": "glass",
    "plastic": "plastic",
}


def material_family(preset: Optional[str]) -> str:
    """Density family for a material preset name."""
    if not preset:
        return "metal"
    if preset in _FAMILY_BY_PRESET:
        return _FAMILY_BY_PRESET[preset]
    for token, family in (("wood", "wood"), ("oak", "wood"), ("teak", "wood"),
                          ("cedar", "wood"), ("timber", "wood"), ("ipe", "wood"),
                          ("concrete", "concrete"), ("glass", "glass"),
                          ("lens", "glass"), ("plastic", "plastic")):
        if token in preset:
            return family
    return "metal"


def _path_length(path: Sequence[Sequence[float]]) -> float:
    total = 0.0
    for a, b in zip(path, path[1:]):
        total += math.dist(a, b)
    return total


def _lathe_volume(params: dict) -> float:
    """Disc method over the resolved profile: a solid of revolution is the
    sum of pi*r^2*dz over its (r, z) samples."""
    pts = resolve_profile(params["profile"], radius=params.get("radius"),
                          depth=params.get("depth"))
    total = 0.0
    for (r0, z0), (r1, z1) in zip(pts, pts[1:]):
        dz = abs(z1 - z0)
        if dz <= 0:
            continue
        # frustum between the two sampled radii
        total += math.pi * dz * (r0 * r0 + r0 * r1 + r1 * r1) / 3.0
    return total


def _profile_area(profile: dict) -> float:
    """Cross-section area of a loft profile: rect is w*h, ellipse pi/4*w*h."""
    w = float(profile.get("w", 0.0))
    h = float(profile.get("h", 0.0))
    if profile.get("shape") == "rect":
        return w * h
    return math.pi * 0.25 * w * h


def solid_volume(prim: Primitive) -> float:
    """Volume of one primitive in cubic metres, hollow where it is hollow."""
    p = prim.params
    kind = prim.kind

    if kind == "box":
        sx, sy, sz = p["size"]
        vol = float(sx) * float(sy) * float(sz)
    elif kind == "sphere":
        r = float(p["radius"])
        vol = 4.0 / 3.0 * math.pi * r ** 3
    elif kind == "cylinder":
        vol = math.pi * float(p["radius"]) ** 2 * float(p["depth"])
    elif kind == "cone":
        rb, rt = float(p["radius_bottom"]), float(p["radius_top"])
        vol = math.pi * float(p["depth"]) * (rb * rb + rb * rt + rt * rt) / 3.0
    elif kind == "tube":
        r = float(p["radius"])
        wall = float(p.get("wall", 0.0) or 0.0)
        depth = float(p["depth"])
        inner = max(0.0, r - wall)
        if p.get("section") == "square":
            # square hollow section: outer 2r across flats, wall each side
            vol = depth * (4.0 * r * r - 4.0 * inner * inner)
        else:
            vol = math.pi * depth * (r * r - inner * inner)
        if wall <= 0:  # a "tube" with no wall is modelled solid
            vol = (4.0 * r * r if p.get("section") == "square"
                   else math.pi * r * r) * depth
    elif kind == "sweep":
        r0 = float(p["radius"])
        r1 = float(p.get("radius_end", r0) or r0)
        r_avg = (r0 + r1) / 2.0
        vol = math.pi * r_avg * r_avg * _path_length(p["path"])
    elif kind == "loft":
        a0 = _profile_area(p["profile_start"])
        a1 = _profile_area(p["profile_end"])
        vol = (a0 + a1) / 2.0 * float(p["depth"])
    elif kind == "lathe":
        vol = _lathe_volume(p)
    else:
        return 0.0

    # a sheet/cast part is a shell, not a billet: keep only the skin. The core
    # is the same solid shrunk inward by the shell thickness — computed with
    # the SAME formulas rather than a blanket ratio, because the right ratio
    # differs per kind (a cone's is nothing like a sphere's).
    shell = p.get("shell")
    if isinstance(shell, (int, float)) and shell > 0 and kind != "tube":
        vol = max(0.0, vol - _core_volume(prim, float(shell)))
    return max(0.0, vol)


def _shrink(value: float, by: float) -> float:
    return max(0.0, float(value) - by)


def _core_volume(prim: Primitive, shell: float) -> float:
    """Volume of the hollow core left by solidifying `prim` inward by
    `shell` — i.e. the same solid with every dimension pulled in."""
    p = prim.params
    kind = prim.kind
    if kind == "box":
        sx, sy, sz = p["size"]
        return (_shrink(sx, 2 * shell) * _shrink(sy, 2 * shell)
                * _shrink(sz, 2 * shell))
    if kind == "sphere":
        return 4.0 / 3.0 * math.pi * _shrink(p["radius"], shell) ** 3
    if kind == "cylinder":
        return math.pi * _shrink(p["radius"], shell) ** 2 * _shrink(p["depth"], 2 * shell)
    if kind == "cone":
        rb = _shrink(p["radius_bottom"], shell)
        rt = _shrink(p["radius_top"], shell)
        return math.pi * _shrink(p["depth"], 2 * shell) * (rb * rb + rb * rt + rt * rt) / 3.0
    if kind == "loft":
        def shrunk(profile: dict) -> dict:
            return {"shape": profile.get("shape"),
                    "w": _shrink(profile.get("w", 0.0), 2 * shell),
                    "h": _shrink(profile.get("h", 0.0), 2 * shell)}
        a0 = _profile_area(shrunk(p["profile_start"]))
        a1 = _profile_area(shrunk(p["profile_end"]))
        return (a0 + a1) / 2.0 * _shrink(p["depth"], 2 * shell)
    if kind == "sweep":
        r0 = _shrink(p["radius"], shell)
        r1 = _shrink(p.get("radius_end", p["radius"]) or p["radius"], shell)
        r_avg = (r0 + r1) / 2.0
        return math.pi * r_avg * r_avg * _path_length(p["path"])
    if kind == "lathe":
        # radial shrink only — the profile's own z extent is kept
        pts = resolve_profile(p["profile"], radius=p.get("radius"),
                              depth=p.get("depth"))
        total = 0.0
        for (r0, z0), (r1, z1) in zip(pts, pts[1:]):
            dz = abs(z1 - z0)
            if dz <= 0:
                continue
            a, b = _shrink(r0, shell), _shrink(r1, shell)
            total += math.pi * dz * (a * a + a * b + b * b) / 3.0
        return total
    return 0.0


class PartWeight:
    """One built part's contribution to the takeoff."""

    __slots__ = ("component", "name", "slot", "family", "volume_m3", "kg")

    def __init__(self, component: str, name: str, slot: str, family: str,
                 volume_m3: float, kg: float):
        self.component = component
        self.name = name
        self.slot = slot
        self.family = family
        self.volume_m3 = volume_m3
        self.kg = kg

    def as_dict(self) -> dict:
        return {"component": self.component, "name": self.name,
                "slot": self.slot, "family": self.family,
                "volume_m3": self.volume_m3, "kg": self.kg,
                "lb": self.kg / KG_PER_LB}


def compute_takeoff(primitives: List[Primitive], spec: Optional[dict] = None) -> dict:
    """Per-part, per-component and total mass for a built asset.

    ``cut=True`` primitives are negative space and contribute nothing (see the
    module docstring on why holes are not subtracted)."""
    from .base import material_preset_name

    parts: List[PartWeight] = []
    for prim in primitives:
        if getattr(prim, "cut", False):
            continue
        vol = solid_volume(prim)
        if vol <= 0:
            continue
        preset = (material_preset_name(spec, prim.material_slot)
                  if spec is not None else None)
        family = material_family(preset)
        kg = vol * DENSITY_KG_M3[family]
        parts.append(PartWeight(prim.component, prim.name, prim.material_slot,
                                family, vol, kg))

    by_component: Dict[str, float] = {}
    for part in parts:
        by_component[part.component] = by_component.get(part.component, 0.0) + part.kg
    total_kg = sum(part.kg for part in parts)
    return {
        "parts": [p.as_dict() for p in parts],
        "by_component": [
            {"component": c, "kg": kg, "lb": kg / KG_PER_LB}
            for c, kg in sorted(by_component.items(), key=lambda kv: -kv[1])
        ],
        "total_kg": total_kg,
        "total_lb": total_kg / KG_PER_LB,
    }


def stock_callout(prim: Primitive, imperial: bool = True) -> Optional[str]:
    """Shop description of a member's stock, e.g. "2.0 SQ x 0.188 wall tube"
    or "3.5 OD x 0.125 wall pipe" — None for parts that aren't stock shapes."""
    p = prim.params
    if prim.kind == "sweep":
        # a bent member's shop note is its diameter and its called-out radius
        bend = p.get("bend_radius")
        if not bend:
            return None
        across = 2.0 * float(p["radius"])
        r = float(bend)
        if imperial:
            return f"{across / 0.0254:.3g} OD bent tube, R {r / 0.0254:.3g} bend"
        return f"{across * 1000:.3g} mm OD bent tube, R {r * 1000:.3g} mm bend"
    if prim.kind != "tube":
        return None
    wall = float(p.get("wall", 0.0) or 0.0)
    across = 2.0 * float(p["radius"])
    if imperial:
        across, wall = across / 0.0254, wall / 0.0254
        unit = ""
    else:
        across, wall = across * 1000.0, wall * 1000.0
        unit = "mm "
    if p.get("section") == "square":
        return f"{across:.3g} {unit}SQ x {wall:.3g} {unit}wall tube"
    return f"{across:.3g} {unit}OD x {wall:.3g} {unit}wall pipe"
