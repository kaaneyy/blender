"""Buildability / load-path validator (the machine check behind the
CONNECTION RULES prompt).

Builds a component-level contact graph from primitive AABB overlaps and
verifies the structure a fabricator cares about *before* anything ships:

* every component must reach the ground (z=0) through touching parts —
  floating components are reported with the nearest support and the gap in
  millimeters, so the LLM retry (and the "Check & fix connections" quick
  fix) gets a deterministic, actionable finding instead of "try again";
* nothing may extend below grade;
* every declared connection must correspond to a real contact.

Python-only by design: findings have no visual output, so there is no TS
mirror to keep in lockstep. Findings are plain dicts shaped like
standards.validator.Violation.to_dict() (parameter_id "__buildability__")
so existing violation plumbing can carry them.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from .base import Primitive
from .hardware import _aabb

#: parts closer than this (m) are considered in contact
CONTACT_TOL = 0.0005
#: a component whose lowest point is within this of z=0 is grounded
GROUND_TOL = 0.005
#: geometry below -this (m) is flagged as below grade
BELOW_TOL = 0.005

# --------------------------------------------------------------------------
# Scale-sanity thresholds (check_scale_sanity). Chosen against the bundled
# examples (park_bench/pergola/street_light/bike_rack/planter): tight enough
# to catch the "0.3 m solar panel on a 3.7 m pergola" defect, loose enough
# that no legitimate part (finial, cap, slat, rail) trips them.
# --------------------------------------------------------------------------
#: a component's max dimension must be at least asset_max/this to escape
#: "toy-scale" — the WHY repro (0.3 m panel on a 3.81 m pergola envelope) is
#: ~12.7x smaller. NOTE: an initial guess of 25 here left legitimate small
#: components (street_light's base_plate at ~19x, luminaire at ~12.3x the
#: asset's max dimension) statistically indistinguishable from the repro on
#: dimension ratio alone — 10 clears the repro (12.7x) with margin while the
#: paired volume gate below (with a *much* wider margin: base_plate/luminaire
#: sit at ~350-400x asset volume, the repro panel at ~12600x) is what
#: actually keeps those legitimate parts clean
SCALE_OUTLIER_RATIO = 10.0
#: ALSO require the component's envelope volume be under asset_volume/this —
#: paired with SCALE_OUTLIER_RATIO so thin-but-long parts (slats, rails,
#: poles) AND small-but-legitimate hardware-scale parts (base plates,
#: luminaire heads) that fail the dimension ratio alone don't false-positive
SCALE_OUTLIER_VOLUME_RATIO = 2000.0
#: a component whose max dimension exceeds this many times the max dimension
#: of the rest of the asset (i.e. computed WITHOUT that component) is a
#: giant outlier — the WHY repro's 12 m panel on the same pergola
SCALE_GIANT_RATIO = 2.5
#: overall asset envelope sanity bounds (m) — nothing this repo builds
#: should be smaller than a doorknob or larger than a city block
ENVELOPE_MAX = 30.0
ENVELOPE_MIN = 0.2


def _finding(kind: str, message: str, component: str = "",
             severity: str = "error") -> dict:
    """Violation.to_dict()-shaped finding so existing plumbing carries it."""
    return {
        "parameter_id": "__buildability__",
        "value": 0.0,
        "unit": "m",
        "limit_type": kind,
        "limit_value": 0.0,
        "limit_unit": "m",
        "corrected_value": None,
        "code_ref": "buildability",
        "source": f"contact-graph check ({component})" if component else "contact-graph check",
        "message": message,
        "severity": severity,
    }


def _gap(a: Tuple, b: Tuple) -> float:
    """Euclidean gap between two AABBs ((center, half) tuples); 0 if they
    overlap or touch."""
    (ca, ha), (cb, hb) = a, b
    d2 = 0.0
    for k in range(3):
        g = abs(ca[k] - cb[k]) - (ha[k] + hb[k])
        if g > 0:
            d2 += g * g
    return math.sqrt(d2)


def _boxes_by_component(prims: List[Primitive]) -> Dict[str, List[Tuple]]:
    out: Dict[str, List[Tuple]] = {}
    for p in prims:
        if p.cut or p.component == "hardware":
            continue
        out.setdefault(p.component, []).append(_aabb(p))
    return out


def _component_gap(boxes_a: List[Tuple], boxes_b: List[Tuple]) -> float:
    return min(_gap(a, b) for a in boxes_a for b in boxes_b)


def check_buildability(prims: List[Primitive], spec=None) -> List[dict]:
    """Contact-graph findings for the computed primitives: floating
    components (error), below-grade geometry (warning), and declared
    connections with no real contact (warning)."""
    by_comp = _boxes_by_component(prims)
    comps = sorted(by_comp)
    if not comps:
        return []

    findings: List[dict] = []

    # ---------------------------------------------------------- contacts
    touching: Dict[str, set] = {c: set() for c in comps}
    for i in range(len(comps)):
        for j in range(i + 1, len(comps)):
            if _component_gap(by_comp[comps[i]], by_comp[comps[j]]) <= CONTACT_TOL:
                touching[comps[i]].add(comps[j])
                touching[comps[j]].add(comps[i])

    # ---------------------------------------------------------- grounding
    grounded = {
        c for c in comps
        if min(box[0][2] - box[1][2] for box in by_comp[c]) <= GROUND_TOL
    }
    reached = set(grounded)
    frontier = list(grounded)
    while frontier:
        for nxt in touching[frontier.pop()]:
            if nxt not in reached:
                reached.add(nxt)
                frontier.append(nxt)

    for c in comps:
        if c in reached:
            continue
        # nearest supported component + gap, so the fix is unambiguous
        supported = [s for s in comps if s in reached]
        if supported:
            nearest = min(supported, key=lambda s: _component_gap(by_comp[c], by_comp[s]))
            gap_mm = _component_gap(by_comp[c], by_comp[nearest]) * 1000
            detail = f"nearest support '{nearest}' is {gap_mm:.0f}mm away"
        else:
            detail = "no component reaches the ground at all"
        findings.append(_finding(
            "floating",
            f"Component '{c}' floats — it has no load path to the ground "
            f"(z=0): {detail}. Extend or move parts so they interpenetrate "
            f"10-20mm with their support.",
            component=c,
        ))

    # ---------------------------------------------------------- below grade
    for c in comps:
        low = min(box[0][2] - box[1][2] for box in by_comp[c])
        if low < -BELOW_TOL:
            findings.append(_finding(
                "below_ground",
                f"Component '{c}' extends {abs(low) * 1000:.0f}mm below grade "
                f"(z=0) — nothing may go below the ground plane.",
                component=c, severity="warning",
            ))

    # ---------------------------------------------------------- declarations
    if isinstance(spec, dict):
        prim_boxes = [
            (p, _aabb(p)) for p in prims if not p.cut and p.component != "hardware"
        ]
        # components the spec KNOWS about even when their toggle hides them —
        # a declaration for an off-toggle feature is dormant, not dead
        declared_components = set(spec.get("components") or [])
        for rp in spec.get("primitives") or []:
            if isinstance(rp, dict) and rp.get("component"):
                declared_components.add(rp["component"])

        def side_boxes(ref: str) -> List[Tuple]:
            return [
                box for p, box in prim_boxes
                if ref == p.component or ref == f"{p.component}/{p.name}"
            ]

        for d in spec.get("connections") or []:
            if not isinstance(d, dict):
                continue
            a, b = d.get("a"), d.get("b")
            if not isinstance(a, str) or not isinstance(b, str):
                continue
            if a == "ground" or b == "ground":
                continue  # anchorage declarations have no pair contact
            boxes_a, boxes_b = side_boxes(a), side_boxes(b)
            if not boxes_a or not boxes_b:
                missing_ref = a if not boxes_a else b
                if missing_ref.split("/")[0] in declared_components:
                    continue  # component exists but is toggled off — dormant
                findings.append(_finding(
                    "unmatched_declaration",
                    f"Declared connection {a!r} ↔ {b!r} references parts "
                    f"that don't exist in the geometry.",
                    severity="warning",
                ))
                continue
            gap = _component_gap(boxes_a, boxes_b)
            if gap > CONTACT_TOL:
                findings.append(_finding(
                    "unmatched_declaration",
                    f"Declared connection {a!r} ↔ {b!r} has no real "
                    f"contact — the parts are {gap * 1000:.0f}mm apart, so no "
                    f"hardware can be placed. Make them interpenetrate "
                    f"10-20mm.",
                    severity="warning",
                ))

    return findings


def buildability_errors(findings: List[dict]) -> List[dict]:
    return [f for f in findings if f.get("severity") == "error"]


# --------------------------------------------------------------------------
# Scale sanity
# --------------------------------------------------------------------------

def _scale_finding(kind: str, message: str, component: Optional[str] = None) -> dict:
    """Contract dict for check_scale_sanity — deliberately NOT the
    Violation.to_dict() shape _finding() produces above (no parameter_id /
    limit_type plumbing needed here): {severity, kind, message, component}."""
    return {
        "severity": "warning",
        "kind": kind,
        "message": message,
        "component": component,
    }


def _envelope_boxes_by_component(prims: List[Primitive]) -> Dict[str, List[Tuple]]:
    """Same non-cut, non-hardware filter as _boxes_by_component, kept
    separate so scale-sanity stays independent of the load-path check."""
    out: Dict[str, List[Tuple]] = {}
    for p in prims:
        if p.cut or p.component == "hardware":
            continue
        out.setdefault(p.component, []).append(_aabb(p))
    return out


def _union_box(boxes: List[Tuple]) -> Tuple:
    """Union AABB ((center, half) tuple) enclosing all given AABBs."""
    lo = [min(c[k] - h[k] for c, h in boxes) for k in range(3)]
    hi = [max(c[k] + h[k] for c, h in boxes) for k in range(3)]
    center = tuple((lo[k] + hi[k]) / 2 for k in range(3))
    half = tuple((hi[k] - lo[k]) / 2 for k in range(3))
    return (center, half)


def _envelope_dims(box: Tuple) -> Tuple[float, float, float]:
    """Full (not half) extents of an AABB."""
    _, half = box
    return (2 * half[0], 2 * half[1], 2 * half[2])


def _envelope_volume(box: Tuple) -> float:
    dx, dy, dz = _envelope_dims(box)
    return dx * dy * dz


def check_scale_sanity(prims: List[Primitive], spec=None) -> List[dict]:
    """Flags scale-inconsistent components: a component whose envelope is
    wildly small (scale_outlier) or wildly large (scale_giant) relative to
    the rest of the asset, or an asset whose overall envelope is outside a
    plausible fabrication range (envelope_extreme). Placement-only checks
    (check_buildability, audit.py) never catch this — a 0.3 m "solar panel"
    on a 3.7 m pergola sits fine, floats nothing, and breaks no declared
    connection; it is just the wrong size. Never raises for a valid
    primitive list — an empty/degenerate asset simply yields no findings."""
    by_comp = _envelope_boxes_by_component(prims)
    comps = sorted(by_comp)
    if not comps:
        return []

    envelopes = {c: _union_box(boxes) for c, boxes in by_comp.items()}

    findings: List[dict] = []

    # ---------------------------------------------------------- envelope
    asset_box = _union_box(list(envelopes.values()))
    asset_max = max(_envelope_dims(asset_box))
    asset_volume = _envelope_volume(asset_box)

    if asset_max > ENVELOPE_MAX or asset_max < ENVELOPE_MIN:
        findings.append(_scale_finding(
            "envelope_extreme",
            f"Overall asset envelope is {asset_max:.2f} m across the "
            f"longest dimension — outside the plausible "
            f"{ENVELOPE_MIN:.1f}-{ENVELOPE_MAX:.0f} m range for a "
            f"fabricated asset.",
        ))

    # ---------------------------------------------------------- per component
    for c in comps:
        box = envelopes[c]
        comp_max = max(_envelope_dims(box))
        comp_volume = _envelope_volume(box)

        if (comp_max < asset_max / SCALE_OUTLIER_RATIO
                and comp_volume < asset_volume / SCALE_OUTLIER_VOLUME_RATIO):
            findings.append(_scale_finding(
                "scale_outlier",
                f"'{c}' spans {comp_max:.2f} m on a {asset_max:.2f} m asset "
                f"— toy-scale relative to the structure; real-world "
                f"features keep their real dimensions.",
                component=c,
            ))

        if len(comps) >= 2:
            rest_box = _union_box([envelopes[o] for o in comps if o != c])
            rest_max = max(_envelope_dims(rest_box))
            if rest_max > 0 and comp_max > SCALE_GIANT_RATIO * rest_max:
                findings.append(_scale_finding(
                    "scale_giant",
                    f"'{c}' spans {comp_max:.2f} m — {comp_max / rest_max:.1f}x "
                    f"the {rest_max:.2f} m extent of the rest of the asset "
                    f"— giant-scale relative to the structure; real-world "
                    f"features keep their real dimensions.",
                    component=c,
                ))

    return findings
