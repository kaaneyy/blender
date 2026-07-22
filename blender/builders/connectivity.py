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
import re
from typing import Dict, List, Optional, Tuple

from .base import Primitive
from .hardware import _aabb
from . import accessible_table, street_light

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

# --------------------------------------------------------------------------
# Sliver-member thresholds (also check_scale_sanity, but per-PRIMITIVE, not
# per-component). The envelope math above only measures a component's size
# relative to the REST of the asset — a tall, wire-thin post has a tall
# envelope just like a real one, so it sails through scale_outlier/
# scale_giant/envelope_extreme untouched: nothing above ever weighs a
# member's cross-section against its own length. This is the WHY repro
# ("15 ft victorian post" built as a ~1cm-diameter cylinder): buildability
# passes (the wire reaches grade), scale_outlier/giant pass (its envelope is
# asset-scale, not toy- or giant-scale). Chosen against that repro (4.57m
# span, 20mm cross-section) AND every legitimately slender member the
# bundled examples ship, so a real post/rail/pin never false-positives:
#   - park_bench legs/rails/back_posts: <=0.5m span, under SLIVER_MIN_SPAN
#     entirely — a bench leg is inherently too short to read as a "post"
#   - street_light's tapered pole shaft: 9.14m span, 203mm (8in) diameter —
#     a real fluted lamp-post section, caught by neither ratio nor cap
#   - street_light's mast arm: a curved sweep whose own bbox is 453mm wide
#     (the curve's rise, not the 70mm tube), nowhere near either threshold
#   - street_light's banner-bracket pin (toggle-only): 0.9m reach, 32mm
#     (1-1/4in) diameter, ~28:1 aspect — genuinely slender hardware, not a
#     structural post; this is what pins SLIVER_RATIO at 40 rather than the
#     repro's own ~230:1 aspect (any ratio in [29, 228) would flag the repro,
#     but 40 leaves comfortable margin on both sides)
#   - pergola posts/beams/rafters, bike_rack hoops/base channels, planter
#     vessel/soil: all either short or >=100mm across
# See TestSliverMembers in test_connectivity.py for the measured numbers.
# --------------------------------------------------------------------------
#: a member must span at least this (m) before its cross-section even
#: matters — legs/rails/gussets shorter than this are inherently stubby and
#: never read as "a 15ft post modeled as a wire" regardless of thinness
SLIVER_MIN_SPAN = 0.75
#: the member's own span-to-cross-section ratio must exceed this to read as
#: a hairline WIRE rather than a legitimately slender member. Paired with
#: SLIVER_ABS_CAP below: this alone would also catch a real flagpole
#: (6m / 50mm ~= 120:1), which is why the absolute cap has to be the one
#: that lets a real flagpole through
SLIVER_RATIO = 40.0
#: AND the cross-section itself must be under this absolute size (m) —
#: freestanding post/pole bases genuinely run 3-8in / 0.08-0.20m, and even
#: this repo's slenderest legitimate tube (the 32mm banner-bracket pin,
#: saved above by SLIVER_RATIO instead) stays under it, so 40mm separates
#: "hairline" from "merely slim" without the ratio check alone
SLIVER_ABS_CAP = 0.04


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
# Dead-control detection (check_dead_controls)
# --------------------------------------------------------------------------
#: asset_type -> the curated builder module declaring CONSUMED_PARAMS /
#: CONSUMED_TOGGLES / CONSUMED_SELECTS. Imported directly (not derived from
#: base.BUILDERS) so this check is correct regardless of import order —
#: base.BUILDERS only gets populated as a side effect of importing
#: blender.builders, which nothing here can guarantee has already run.
_CURATED_VOCAB = {
    "street_light": street_light,
    "accessible_table": accessible_table,
}

#: call names in the expression grammar (expr.py's _FUNCS) — these appear as
#: identifier-shaped tokens in an expression string ("min(a, b)") but are
#: never parameter/toggle ids, so they're excluded from the referenced-id
#: scan (matching the brief's "excluding min/max/abs").
_EXPR_FUNCTION_NAMES = {"min", "max", "abs"}
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: consumed directly by base.compute_primitives itself (see base.py), not by
#: either builder path's own expression/vocabulary surface — never flagged
#: as dead on either path.
_UNIVERSAL_TOGGLES = {"connection_hardware"}


def _expr_tokens(value) -> set:
    """Identifier tokens referenced by a single expression SITE. A string is
    scanned as an expression — the identifier regex matches exactly what
    expr.py's ast.Name nodes would resolve, since Python identifier syntax
    and this regex agree on what is a valid name. A non-string (a literal
    number, or a missing/None field) has no identifiers to contribute."""
    if isinstance(value, str):
        return {t for t in _IDENT_RE.findall(value) if t not in _EXPR_FUNCTION_NAMES}
    return set()


def _referenced_ids(spec: dict) -> set:
    """Every parameter/toggle id token appearing in ANY expression site
    generic.py's build_custom/_eval_params evaluates: each primitive's
    visible_if, location, rotation, array.count/array.step, and every
    params entry — scalars (radius, depth, segments, ...), size-like
    triples, the lathe profile's raw [[r, z], ...] point list (a NAMED
    profile string like "dome" is not an expression and contributes
    nothing), the sweep path's point list, and profile_start/profile_end's
    w/h (their "shape" is a fixed enum, not an expression). Mirrors
    _eval_params' own per-key dispatch exactly so a legitimate expression
    site can never go unscanned, and no non-expression field (a
    primitive's name/component/material_slot/kind/cut) is scanned as if it
    were one."""
    referenced: set = set()
    for raw in spec.get("primitives") or []:
        if not isinstance(raw, dict):
            continue
        referenced |= _expr_tokens(raw.get("visible_if"))
        for v in raw.get("location") or ():
            referenced |= _expr_tokens(v)
        for v in raw.get("rotation") or ():
            referenced |= _expr_tokens(v)
        array = raw.get("array")
        if isinstance(array, dict):
            referenced |= _expr_tokens(array.get("count"))
            for v in array.get("step") or ():
                referenced |= _expr_tokens(v)
        params = raw.get("params")
        if not isinstance(params, dict):
            continue
        for key, value in params.items():
            if key == "profile":
                if isinstance(value, str):
                    continue  # named profile (e.g. "dome") — not an expression
                for pair in value or ():
                    if isinstance(pair, (list, tuple)):
                        for v in pair:
                            referenced |= _expr_tokens(v)
            elif key == "path":
                for point in value or ():
                    if isinstance(point, (list, tuple)):
                        for v in point:
                            referenced |= _expr_tokens(v)
            elif key in ("profile_start", "profile_end"):
                if isinstance(value, dict):
                    referenced |= _expr_tokens(value.get("w"))
                    referenced |= _expr_tokens(value.get("h"))
            elif isinstance(value, (list, tuple)):
                for v in value:
                    referenced |= _expr_tokens(v)
            else:
                referenced |= _expr_tokens(value)
    return referenced


def check_dead_controls(spec) -> List[dict]:
    """Findings for a parameter/toggle a spec exposes as a live-looking
    slider/checkbox whose value is PROVABLY never consumed by the geometry
    it is supposed to drive. Two disjoint paths, mirroring
    compute_primitives' own "PRIMITIVES ALWAYS WIN" dispatch (base.py):

    * generic path (spec has a non-empty "primitives" array): a
      parameter/toggle id is dead unless it is referenced by some
      expression site build_custom/_eval_params actually evaluates (see
      _referenced_ids).
    * curated-builder path (no primitives, and spec["asset_type"] matches a
      registered curated builder): a parameter/toggle/select id is dead
      unless it is in that builder's OWN declared vocabulary
      (CONSUMED_PARAMS / CONSUMED_TOGGLES / CONSUMED_SELECTS on the
      builder's module) — the builder only ever reads spec_params() /
      spec_toggles() / spec_selects() for those exact ids, so anything else
      is silently ignored by construction no matter how plausible it looks
      in the UI.

    The universal "connection_hardware" toggle (consumed directly by
    base.compute_primitives, not by either builder path) is never flagged
    on either path. A spec that is neither (no primitives AND no curated
    builder for its asset_type) returns [] — that combination already
    fails compute_primitives itself elsewhere in the pipeline, so it is not
    this check's job to also report on it.

    Findings are Violation.to_dict()-shaped (via _finding, kind
    "dead_control", severity "error") so they ride the same violation
    plumbing check_buildability's findings do."""
    if not isinstance(spec, dict):
        return []

    findings: List[dict] = []

    if spec.get("primitives"):
        referenced = _referenced_ids(spec)
        for p in spec.get("parameters") or []:
            pid = p.get("id") if isinstance(p, dict) else None
            if not pid or pid in referenced:
                continue
            findings.append(_finding(
                "dead_control",
                f"Parameter '{pid}' is never referenced by any primitive "
                f"expression — it drives nothing. Remove it, or reference "
                f"it from a primitive expression (a location/rotation/"
                f"params value, visible_if, or an array count/step).",
                component=pid,
            ))
        for t in spec.get("toggles") or []:
            tid = t.get("id") if isinstance(t, dict) else None
            if not tid or tid in _UNIVERSAL_TOGGLES or tid in referenced:
                continue
            findings.append(_finding(
                "dead_control",
                f"Toggle '{tid}' is never referenced by any primitive "
                f"expression (e.g. a visible_if gate) — it drives nothing. "
                f"Remove it, or reference it from a primitive expression.",
                component=tid,
            ))
        return findings

    module = _CURATED_VOCAB.get(spec.get("asset_type"))
    if module is None:
        return []

    known = (
        set(getattr(module, "CONSUMED_PARAMS", ()))
        | set(getattr(module, "CONSUMED_TOGGLES", ()))
        | set(getattr(module, "CONSUMED_SELECTS", {}))
        | _UNIVERSAL_TOGGLES
    )
    asset_type = spec.get("asset_type")
    for p in spec.get("parameters") or []:
        pid = p.get("id") if isinstance(p, dict) else None
        if not pid or pid in known:
            continue
        findings.append(_finding(
            "dead_control",
            f"Parameter '{pid}' is not one of the ids {asset_type!r}'s "
            f"curated builder consumes — it drives nothing. Remove it, or "
            f"model the feature it should control with a \"primitives\" "
            f"array instead of relying on this curated builder.",
            component=pid,
        ))
    for t in spec.get("toggles") or []:
        tid = t.get("id") if isinstance(t, dict) else None
        if not tid or tid in known:
            continue
        findings.append(_finding(
            "dead_control",
            f"Toggle '{tid}' is not one of the ids {asset_type!r}'s "
            f"curated builder consumes — it drives nothing. Remove it, or "
            f"model the feature it should control with a \"primitives\" "
            f"array instead of relying on this curated builder.",
            component=tid,
        ))
    return findings


# --------------------------------------------------------------------------
# Embedded-part detection (check_embedded_parts)
# --------------------------------------------------------------------------
#: fraction of a component's total AABB volume that must lie inside OTHER
#: components' boxes before it is flagged as rammed-through/intersected
#: geometry rather than a seated joint. A proper fabrication embed (the
#: repo's own convention: 10-20mm seating, e.g. pergola.json's beams sit
#: "post_height + beam_depth/2 - 0.02" — a 20mm overlap into a multi-meter
#: beam/post) puts a fraction on the order of 0.02-0.001, far below this;
#: park_bench.json's slat/rail embeds (also ~10-20mm into meter-scale
#: frame members) land in the same range. 0.6 leaves a wide margin between
#: legitimate joints and the "AI intersected the whole part" defect (which
#: reproduces at fractions approaching 1.0).
EMBED_OVERLAP_RATIO = 0.6

#: A component that RAMS THROUGH a thin host (poking out both sides) can
#: have a *low* self-volume-inside-host fraction (the host may be much
#: thinner than the piercing part is long) and so evades EMBED_OVERLAP_RATIO
#: entirely. This constant instead asks: does the pair's AABB overlap span
#: nearly the host's FULL extent along >=2 axes, with the piercing part
#: overrunning the host on BOTH sides along at least one of those axes?
#: Verified against the repo's own legitimate joints (see
#: TestPiercedParts in test_connectivity.py for the measured numbers):
#: pergola post<->beam (a 20mm bottom-face embed) and rafter<->beam (a
#: seated-on-top joint) each pierce only 1 host axis at ratio 1.0 in either
#: direction — never 2 — so they stay clean regardless of how tight this
#: fraction is; 0.95 leaves headroom for a joint whose face dimensions
#: aren't pixel-perfectly aligned with its host while still catching a
#: part sized to reach all the way across a host's cross-section.
PIERCE_AXIS_FRACTION = 0.95
#: A SEPARATE false positive the axis/both-sides rule alone cannot rule
#: out: a small part flush-mounted on the SIDE of a much larger box (e.g.
#: a sign bracket 20mm-embedded into a post's face) is narrower than its
#: host on the two non-insertion axes essentially by definition — its own
#: cross-section is a strict subset of the big member's, so those two axes
#: read as "spans ~100% of HOST's extent, HOST overruns on both sides"
#: with HOST and PIERCER swapped (measured: pergola's own post<->cap
#: synthetic joint gave axis ratios 1.0/1.0 on y/z from the "post pierces
#: cap" direction — mathematically identical in shape to the true lantern
#: repro, despite being a normal small-bracket-on-a-post attachment).
#: What actually differs: in a genuine ramming-through, the HOST is the
#: pre-existing, comparably-or-more substantial member — never something
#: whose own volume is dwarfed by the "piercer" it supposedly can't
#: contain. Requiring the host prim's volume be at least this fraction of
#: the piercer prim's volume (0.0126 for the post/cap false positive vs.
#: ~2.85 for the true beam/lantern repro, see TestPiercedParts) rules out
#: "the big pre-existing member's cross-section swallows a small attached
#: bracket" without weakening the axis/both-sides rule itself.
PIERCE_MIN_HOST_VOLUME_RATIO = 0.5
#: (x, y, z) axis labels used in pierced_part axis lists/messages.
_AXES = ("x", "y", "z")
#: float-noise guard so a flush-fit face (embed offset 0, coordinates
#: equal up to rounding) is never misread as "overrunning both sides" —
#: real fabrication embeds are orders of magnitude larger than this.
_PIERCE_EPS = 1e-6


def _minmax(box: Tuple) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
    """(center, half) -> (lo, hi) corners."""
    center, half = box
    lo = tuple(center[k] - half[k] for k in range(3))
    hi = tuple(center[k] + half[k] for k in range(3))
    return lo, hi


def _box_volume(minmax: Tuple) -> float:
    lo, hi = minmax
    return max(0.0, hi[0] - lo[0]) * max(0.0, hi[1] - lo[1]) * max(0.0, hi[2] - lo[2])


def _intersection_volume(a: Tuple, b: Tuple) -> float:
    """Volume of the (axis-aligned, exact) intersection of two (lo, hi) boxes."""
    (a_lo, a_hi), (b_lo, b_hi) = a, b
    vol = 1.0
    for k in range(3):
        d = min(a_hi[k], b_hi[k]) - max(a_lo[k], b_lo[k])
        if d <= 0:
            return 0.0
        vol *= d
    return vol


def _box_prims_by_component(prims: List[Primitive]) -> Dict[str, List[Tuple]]:
    """Same non-cut, non-'hardware' filter as _boxes_by_component, further
    restricted to kind == 'box' — used only by the pierced_part signature
    (see check_embedded_parts). "Extends beyond the host on both sides" is
    a geometrically EXACT claim for a box's AABB, but hardware._aabb's
    'exact' AABB for round/swept kinds (cylinder, cone, tube, sweep,
    lathe, loft, ...) is still a bounding box that is loose at the
    corners — a bracket merely touching a round pole's surface can sit
    entirely inside the pole's SQUARE bounding box without touching any
    actual pole material. That looseness reads as "pierces on 2+ axes,
    overrunning both sides" even though nothing physically ranned through
    anything (measured false positives: bike_rack.json's thin base
    channel plate vs. its round swept hoops, and street_light.json's
    round pole vs. its tube/sweep/loft-built arm mount — both clean once
    restricted to box-only pairs, see TestPiercedParts). Box-only pairs
    keep the both-sides overrun claim trustworthy while still catching the
    box-vs-box repro this signature exists for."""
    out: Dict[str, List[Tuple]] = {}
    for p in prims:
        if p.cut or p.component == "hardware" or p.kind != "box":
            continue
        out.setdefault(p.component, []).append(_aabb(p))
    return out


def _prim_pierce_axes(piercer: Tuple, host: Tuple) -> List[str]:
    """If `piercer` (lo, hi) rams through `host` (lo, hi) — its overlap with
    the host spans >= PIERCE_AXIS_FRACTION of the host's own extent on at
    least 2 axes, AND on at least one of those axes the piercer's box
    extends beyond the host on BOTH sides (not just a flush face or a
    corner-fit) — return the sorted list of pierced axis names. Returns []
    if the two boxes don't even overlap, or don't meet the pierce
    criteria (e.g. a legitimate 10-20mm face embed, which pierces at most
    1 axis at full ratio: the joint axis is flush/embedded, the other two
    are just "part sits within host's footprint on that axis", not a
    through-and-through overrun; or a small part flush-mounted on a much
    larger host's face, which fails the host-volume floor below)."""
    p_lo, p_hi = piercer
    h_lo, h_hi = host

    # a genuine host is never volumetrically dwarfed by what supposedly
    # can't contain it — see PIERCE_MIN_HOST_VOLUME_RATIO
    if _box_volume(host) < _box_volume(piercer) * PIERCE_MIN_HOST_VOLUME_RATIO:
        return []

    overlaps = [0.0, 0.0, 0.0]
    for k in range(3):
        ov = min(p_hi[k], h_hi[k]) - max(p_lo[k], h_lo[k])
        if ov <= 0:
            return []  # boxes don't actually intersect
        overlaps[k] = ov

    pierced: List[str] = []
    both_sides: List[str] = []
    for k, name in enumerate(_AXES):
        h_ext = h_hi[k] - h_lo[k]
        if h_ext <= 0:
            continue
        if overlaps[k] / h_ext >= PIERCE_AXIS_FRACTION:
            pierced.append(name)
            if p_lo[k] < h_lo[k] - _PIERCE_EPS and p_hi[k] > h_hi[k] + _PIERCE_EPS:
                both_sides.append(name)

    if len(pierced) >= 2 and both_sides:
        return pierced
    return []


def _none_declared_pairs(spec) -> set:
    """Component-name pairs (as frozensets) the spec explicitly declares
    with {"type": "none"} — the repo's existing vocabulary for "these two
    are known to coexist without a fabrication joint" (CLAUDE.md: "type:
    'none' suppresses a joint"). e.g. planter.json declares
    {"a": "planting", "b": "urn", "type": "none"} because potting soil is
    *meant* to sit fully inside the hollow urn's coarse AABB envelope —
    that is nesting-by-design, not the ramming-through-solid-geometry
    defect this check exists to catch, so such declared pairs are exempt."""
    pairs = set()
    if not isinstance(spec, dict):
        return pairs
    for d in spec.get("connections") or []:
        if not isinstance(d, dict) or d.get("type") != "none":
            continue
        a, b = d.get("a"), d.get("b")
        if not isinstance(a, str) or not isinstance(b, str):
            continue
        if a == "ground" or b == "ground":
            continue
        pairs.add(frozenset((a.split("/")[0], b.split("/")[0])))
    return pairs


def check_embedded_parts(prims: List[Primitive], spec=None) -> List[dict]:
    """Flags two geometry signatures of "the AI just intersected the added
    part with the rest of the model" instead of seating it with a modest
    fabrication embed:

    * kind "embedded_part" — a component whose volume sits MOSTLY INSIDE
      other components' boxes (>= EMBED_OVERLAP_RATIO by volume). Catches
      a part buried whole inside a much bigger host.
    * kind "pierced_part" — a component that rams clean THROUGH a host,
      exiting both sides, even when the host is thin enough that the
      piercer's self-volume-inside-host fraction stays low (e.g. a long
      part skewered through a thin beam scores low on volume fraction but
      is exactly the same defect). See _prim_pierce_axes.

    Both share this dict shape: {severity: "warning", kind: str,
    message: str (contains both component names and, for pierced_part,
    the pierced axes), component: str (the offending/piercing component)}.

    Per-component AABBs come from non-cut, non-'hardware' prims via
    hardware._aabb, same filter as check_buildability/check_scale_sanity.
    AABBs are exact for boxes/spheres/tubes and oriented cylinders/cones,
    and tight-rotated-corner for other rotated constructed kinds (see
    hardware._aabb / _rotated_aabb) — a rotated prim contributes its WORLD
    AABB, which can slightly over-estimate true overlap for non-box shapes
    tilted off-axis. That is a pre-existing property of _aabb shared with
    the other connectivity checks, not something this check introduces.

    APPROXIMATION: for each primitive box belonging to component C, this
    takes the single LARGEST intersection volume against any one other
    component's primitive box (the "per-prim max-over-other-boxes"
    approximation the brief allows as an alternative to full box-union
    volume), then sums across C's prims and divides by the sum of C's own
    prim volumes. Taking a max (not a sum) over other boxes per prim
    guarantees no double-counting when multiple other components overlap
    the same region, so the resulting fraction can never exceed 1.0.
    BIAS: this can UNDERESTIMATE true overlap when a single primitive of C
    straddles two (or more) other components' boxes with no single other
    box covering the majority of it — e.g. a part that is ~50% inside a
    beam and ~50% inside an adjacent, non-overlapping post would score
    ~50%, not the true ~100% total-embedded fraction. This bias only
    under-flags that rarer straddling case; it never over-flags a
    legitimate seated joint (which has no single other box anywhere near
    covering it).

    EXEMPTION: a component pair the spec explicitly declares with
    {"type": "none"} (see _none_declared_pairs) is skipped entirely —
    that vocabulary already means "these two are known to coexist without
    a joint" (e.g. potting soil declared 'none' against its urn: it is
    *meant* to sit inside the container's coarse AABB envelope, which is
    nesting-by-design rather than the ramming-through-solid defect this
    check targets). Without this exemption a hollow container modeled as
    a single lathe/tube primitive looks, by AABB alone, indistinguishable
    from solid stock — hardware._aabb has no concept of "hollow".

    Never raises for a valid primitive list. Skips cut prims, the
    'hardware' component (same filter as the load-path/scale checks
    above), and specs with fewer than two components (nothing to embed
    into)."""
    by_comp = _boxes_by_component(prims)
    comps = sorted(by_comp)
    if len(comps) < 2:
        return []

    minmax_by_comp = {c: [_minmax(box) for box in boxes] for c, boxes in by_comp.items()}
    exempt_pairs = _none_declared_pairs(spec)

    findings: List[dict] = []
    for c in comps:
        c_boxes = minmax_by_comp[c]
        total_volume = sum(_box_volume(b) for b in c_boxes)
        if total_volume <= 0:
            continue

        overlap_volume = 0.0
        contributions: Dict[str, float] = {}
        for pb in c_boxes:
            best_vol = 0.0
            best_other: Optional[str] = None
            for o in comps:
                if o == c or frozenset((c, o)) in exempt_pairs:
                    continue
                for ob in minmax_by_comp[o]:
                    v = _intersection_volume(pb, ob)
                    if v > best_vol:
                        best_vol = v
                        best_other = o
            if best_other is not None:
                overlap_volume += best_vol
                contributions[best_other] = contributions.get(best_other, 0.0) + best_vol

        fraction = overlap_volume / total_volume
        if fraction >= EMBED_OVERLAP_RATIO:
            other = max(contributions, key=contributions.get)
            findings.append({
                "severity": "warning",
                "kind": "embedded_part",
                "message": (
                    f"Component '{c}' is {fraction * 100:.0f}% embedded inside "
                    f"component '{other}' (by AABB volume) — this looks like "
                    f"intersected/rammed-through geometry rather than a seated "
                    f"joint. Pull '{c}' back so it only overlaps '{other}' by a "
                    f"modest 10-20mm fabrication embed."
                ),
                "component": c,
            })

    # ---------------------------------------------------- pierced-through
    # A part that rams clean through a THIN host (poking out both sides)
    # can have a low self-volume-inside-host fraction — the embed check
    # above misses it entirely when the host is thin relative to the
    # piercing part's length. Checked per ordered (piercer, host) component
    # pair, at the primitive level (not merged per-component envelopes, so
    # one beam among several doesn't get its extent inflated by unrelated
    # siblings), restricted to box-kind prims on BOTH sides (see
    # _box_prims_by_component for why): the first primitive pair that
    # satisfies _prim_pierce_axes wins the pair.
    box_by_comp = _box_prims_by_component(prims)
    box_comps = set(box_by_comp)
    for c in comps:
        if c not in box_comps:
            continue
        c_boxes = [_minmax(b) for b in box_by_comp[c]]
        for o in comps:
            if o == c or o not in box_comps or frozenset((c, o)) in exempt_pairs:
                continue
            o_boxes = [_minmax(b) for b in box_by_comp[o]]
            axes_hit: List[str] = []
            for pb in c_boxes:
                for ob in o_boxes:
                    axes_hit = _prim_pierce_axes(pb, ob)
                    if axes_hit:
                        break
                if axes_hit:
                    break
            if axes_hit:
                axes_str = " and ".join(axes_hit)
                findings.append({
                    "severity": "warning",
                    "kind": "pierced_part",
                    "message": (
                        f"Component '{c}' pierces through component '{o}' — "
                        f"it exits '{o}''s envelope on both sides along the "
                        f"{axes_str} axis/axes, spanning nearly all of "
                        f"'{o}''s extent there. This looks like "
                        f"rammed-through/intersected geometry rather than a "
                        f"seated joint. Pull '{c}' back so it only overlaps "
                        f"'{o}' by a modest 10-20mm fabrication embed."
                    ),
                    "component": c,
                })

    return findings


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

    # ------------------------------------------------------- sliver members
    # Per-PRIMITIVE (not per-component envelope, which would blur a hairline
    # rod together with whatever else shares its component): a non-cut,
    # non-hardware prim whose own AABB is a hairline rod — long, with BOTH
    # cross-section dims tiny (a thin SHEET, e.g. a sign face, has one tiny
    # dim and one wide one, and is deliberately not caught by this).
    for p in prims:
        if p.cut or p.component == "hardware":
            continue
        length, width, thick = sorted(_envelope_dims(_aabb(p)), reverse=True)
        if (length >= SLIVER_MIN_SPAN
                and width < length / SLIVER_RATIO
                and thick < length / SLIVER_RATIO
                and width < SLIVER_ABS_CAP):
            findings.append(_scale_finding(
                "sliver_member",
                f"'{p.component}/{p.name}' spans {length:.2f} m but its "
                f"cross-section is only {width:.3f} x {thick:.3f} m — a "
                f"hairline member no real fabrication process keeps "
                f"standing. Freestanding post/pole bases run about "
                f"3-8 in (0.08-0.20 m); thicken '{p.component}/{p.name}' "
                f"or taper it from a real base section instead of a "
                f"constant hairline diameter.",
                component=p.component,
            ))

    return findings
