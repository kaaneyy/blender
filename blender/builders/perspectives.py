"""Four professional-discipline evaluators for the Improve flow.

Python-only by design: this module produces server-side analysis text and
numbers, never preview geometry, so — like ``connectivity.py`` and
``schedule.py`` — it has NO TypeScript mirror and must never be added to
``blender/tests/test_mirror_parity.py``.

The app's Improve button looks at the current AssetSpec the way four
different professionals would:

* **architecture** — massing proportions, left/right symmetry, whether any
  horizontal surface lands in a human-scale ergonomic band;
* **mechanical engineering** — the joint/fastener health that
  :mod:`audit.py` already computes (collisions, slivers, detached hardware,
  dead/gapped declarations), plus a hardware bill-of-materials summary;
* **civil / structural engineering** — load path, grade, anchorage, and any
  US-code violation touching a structural dimension;
* **industrial design** — material-palette coherence (undeclared slots,
  missing presets, monochrome palettes, wild weathering spread).

Every evaluator reuses the SAME underlying machinery
(:func:`audit.audit_connections`, :func:`connectivity.check_buildability`,
:func:`schedule.joint_schedule`, :func:`standards.validator.validate_spec`)
rather than re-deriving geometry checks — this module only buckets and
frames those findings per discipline, plus a handful of new lightweight
heuristics (massing, symmetry, human-scale bands, material coherence) that
have no existing home.

Nothing here mutates the input spec; a copy is made before the hardware
toggle is forced on anywhere.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from standards.validator import validate_spec

from .audit import audit_connections
from .base import compute_primitives, resolve_material
from .connectivity import check_buildability
from .hardware import _aabb
from .schedule import joint_schedule

Box = Tuple[Tuple[float, float, float], Tuple[float, float, float]]

#: the four discipline evaluators, in the exact order the UI presents them
PERSPECTIVES: Tuple[dict, ...] = (
    {"id": "architecture", "label": "Architecture", "icon": "🏛️"},
    {"id": "mechanical", "label": "Mechanical engineering", "icon": "🔩"},
    {"id": "civil", "label": "Civil / structural engineering", "icon": "🏗️"},
    {"id": "design", "label": "Industrial design", "icon": "🎨"},
)

_PERSPECTIVE_BY_ID = {p["id"]: p for p in PERSPECTIVES}

# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

#: a component whose lowest point is within this of z=0 is grounded
#: (matches audit.py::GROUND_TOL / connectivity.py::GROUND_TOL)
GROUND_TOL = 0.005


def _finding(severity: str, kind: str, message: str) -> dict:
    return {"severity": severity, "kind": kind, "message": message, "source": "checks"}


def _toggle_on(spec: dict, toggle_id: str) -> bool:
    for t in spec.get("toggles") or []:
        if isinstance(t, dict) and t.get("id") == toggle_id:
            return bool(t.get("value"))
    return False


def _member_boxes(prims) -> Dict[str, List[Box]]:
    """component -> AABBs, excluding cut geometry and generated hardware —
    the "real" fabricated parts a discipline reviewer would look at."""
    out: Dict[str, List[Box]] = {}
    for p in prims:
        if p.cut or p.component == "hardware":
            continue
        out.setdefault(p.component, []).append(_aabb(p))
    return out


def _bounds(member_boxes: Dict[str, List[Box]]) -> Tuple[float, float, float, float, float, float]:
    """(min_x, max_x, min_y, max_y, min_z, max_z) of the combined AABB."""
    boxes = [b for lst in member_boxes.values() for b in lst]
    if not boxes:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    xs_lo = min(c[0] - h[0] for c, h in boxes)
    xs_hi = max(c[0] + h[0] for c, h in boxes)
    ys_lo = min(c[1] - h[1] for c, h in boxes)
    ys_hi = max(c[1] + h[1] for c, h in boxes)
    zs_lo = min(c[2] - h[2] for c, h in boxes)
    zs_hi = max(c[2] + h[2] for c, h in boxes)
    return xs_lo, xs_hi, ys_lo, ys_hi, zs_lo, zs_hi


def _round(v: float, ndigits: int = 4) -> float:
    return round(v, ndigits)


def _grounded_components(member_boxes: Dict[str, List[Box]]) -> set:
    return {
        c for c, boxes in member_boxes.items()
        if min(cen[2] - half[2] for cen, half in boxes) <= GROUND_TOL
    }


def _has_ground_declaration(spec: dict, component: str) -> bool:
    """True if the spec declares an anchor/ground connection touching this
    component (a connection to the literal 'ground' side, or an explicit
    anchor_base declaration naming it)."""
    for conn in spec.get("connections") or []:
        if not isinstance(conn, dict):
            continue
        a, b = conn.get("a"), conn.get("b")
        if not isinstance(a, str) or not isinstance(b, str):
            continue
        sides = {a.split("/")[0], b.split("/")[0]}
        if component not in sides:
            continue
        if "ground" in (a, b) or conn.get("type") == "anchor_base":
            return True
    return False


# ---------------------------------------------------------------------------
# civil / structural engineering
# ---------------------------------------------------------------------------

#: parameter ids whose name suggests a structural dimension (case-insensitive
#: substring match) — used to bucket US-code violations under civil
STRUCTURAL_PARAM_KEYWORDS = (
    "height", "depth", "diameter", "width", "length", "thickness",
    "span", "footing", "embed", "radius",
)


def _eval_civil(spec: dict) -> Tuple[dict, List[dict]]:
    prims = compute_primitives(spec)  # raises on a broken spec
    findings: List[dict] = []

    for f in check_buildability(prims, spec):
        if f.get("limit_type") == "floating":
            findings.append(_finding(f.get("severity", "error"), "floating",
                                      f.get("message", "")))

    audit = audit_connections(spec)
    for f in audit["findings"]:
        if f["kind"] == "below_grade":
            findings.append(_finding(f["severity"], f["kind"],
                                      f.get("detail") or f.get("title") or ""))

    member_boxes = _member_boxes(prims)
    grounded = _grounded_components(member_boxes)
    if _toggle_on(spec, "connection_hardware"):
        for c in sorted(grounded):
            if not _has_ground_declaration(spec, c):
                findings.append(_finding(
                    "info", "unanchored_grounded",
                    f"'{c}' sits at grade but no connection declares an "
                    f"anchor/ground joint for it — confirm it doesn't need "
                    f"foundation anchorage.",
                ))

    result = validate_spec(spec)
    for v in result.violations:
        d = v.to_dict()
        if any(k in d["parameter_id"].lower() for k in STRUCTURAL_PARAM_KEYWORDS):
            findings.append(_finding("warning", f"code_{d['limit_type']}", d["message"]))

    xs_lo, xs_hi, ys_lo, ys_hi, zs_lo, zs_hi = _bounds(member_boxes)
    metrics = {
        "height_m": _round(zs_hi),
        "footprint_m2": _round((xs_hi - xs_lo) * (ys_hi - ys_lo)),
        "grounded_components": len(grounded),
    }
    return metrics, findings


# ---------------------------------------------------------------------------
# mechanical engineering
# ---------------------------------------------------------------------------

#: audit_connections finding kinds that are joint/fastener-hardware concerns
MECHANICAL_AUDIT_KINDS = ("collision", "sliver", "detached", "dead_declaration",
                           "gap_declaration")


def _eval_mechanical(spec: dict) -> Tuple[dict, List[dict]]:
    compute_primitives(spec)  # raises on a broken spec
    findings: List[dict] = []

    audit = audit_connections(spec)
    for f in audit["findings"]:
        if f["kind"] in MECHANICAL_AUDIT_KINDS:
            findings.append(_finding(f["severity"], f["kind"],
                                      f.get("detail") or f.get("title") or ""))

    records = joint_schedule(spec)  # forces hardware on a deep copy internally
    fastener_count = sum(int(r.get("count") or 0) for r in records)
    types = {r.get("type") for r in records if r.get("type")}
    metrics = {
        "joint_count": len(records),
        "fastener_count": fastener_count,
        "connection_types": len(types),
    }
    return metrics, findings


# ---------------------------------------------------------------------------
# architecture
# ---------------------------------------------------------------------------

#: height : max-footprint-side ratio above which a massing reads as tower-like
MASSING_TOWER_RATIO = 8.0
#: ... and below which it reads as a slab
MASSING_SLAB_RATIO = 0.05
#: position/size tolerance (m) for matching a part to its YZ-plane mirror
SYMMETRY_TOL = 0.01
#: ergonomic bands (m elevation of a horizontal surface's top face)
SEAT_BAND = (0.35, 0.55)
COUNTER_BAND = (0.65, 1.10)


def _is_horizontal_surface(box: Box) -> bool:
    """A box reads as a "surface" (seat, shelf, counter) rather than a post
    or panel when it's thinner in Z than it is wide in X and Y."""
    _, h = box
    return h[2] < h[0] and h[2] < h[1] and h[0] > 0.02 and h[1] > 0.02


def _human_scale_hit(member_boxes: Dict[str, List[Box]], lo: float, hi: float) -> Optional[float]:
    for boxes in member_boxes.values():
        for box in boxes:
            if not _is_horizontal_surface(box):
                continue
            cen, half = box
            top = cen[2] + half[2]
            if lo <= top <= hi:
                return top
    return None


def _yz_unmatched(boxes: List[Box]) -> int:
    """Count of boxes with no mirror-image counterpart across the YZ plane
    (x -> -x), i.e. parts that break left/right symmetry. A box centered on
    the plane itself is trivially self-symmetric."""
    n = len(boxes)
    used = [False] * n
    unmatched = 0
    for i in range(n):
        if used[i]:
            continue
        ci, hi_ = boxes[i]
        if abs(ci[0]) < SYMMETRY_TOL:
            used[i] = True
            continue
        mirror_x = -ci[0]
        match = None
        for j in range(n):
            if used[j] or j == i:
                continue
            cj, hj = boxes[j]
            if (abs(cj[0] - mirror_x) < SYMMETRY_TOL
                    and abs(cj[1] - ci[1]) < SYMMETRY_TOL
                    and abs(cj[2] - ci[2]) < SYMMETRY_TOL
                    and abs(hj[0] - hi_[0]) < SYMMETRY_TOL
                    and abs(hj[1] - hi_[1]) < SYMMETRY_TOL
                    and abs(hj[2] - hi_[2]) < SYMMETRY_TOL):
                match = j
                break
        if match is not None:
            used[i] = used[match] = True
        else:
            used[i] = True
            unmatched += 1
    return unmatched


def _eval_architecture(spec: dict) -> Tuple[dict, List[dict]]:
    prims = compute_primitives(spec)  # raises on a broken spec
    findings: List[dict] = []

    member_boxes = _member_boxes(prims)
    xs_lo, xs_hi, ys_lo, ys_hi, zs_lo, zs_hi = _bounds(member_boxes)
    width = xs_hi - xs_lo
    depth = ys_hi - ys_lo
    height = zs_hi
    max_side = max(width, depth)

    if max_side > 1e-9:
        proportion = height / max_side
        if proportion > MASSING_TOWER_RATIO:
            findings.append(_finding(
                "warning", "tower_massing",
                f"Height:footprint ratio is {proportion:.1f} — tower-like "
                f"proportions.",
            ))
        elif proportion < MASSING_SLAB_RATIO:
            findings.append(_finding(
                "warning", "slab_massing",
                f"Height:footprint ratio is {proportion:.3f} — slab-like "
                f"proportions.",
            ))

    boxes = [b for lst in member_boxes.values() for b in lst]
    unmatched = _yz_unmatched(boxes)
    if unmatched > 0:
        findings.append(_finding(
            "info", "asymmetric",
            f"{unmatched} of {len(boxes)} parts have no mirror-image "
            f"counterpart across the centerline (YZ plane) — the asset "
            f"isn't left/right symmetric.",
        ))

    for band_name, (lo, hi) in (("seat (0.35-0.55m)", SEAT_BAND),
                                 ("table/counter (0.65-1.1m)", COUNTER_BAND)):
        top = _human_scale_hit(member_boxes, lo, hi)
        if top is not None:
            findings.append(_finding(
                "info", "human_scale",
                f"A horizontal surface at {top:.2f}m sits in the "
                f"{band_name} ergonomic band.",
            ))

    metrics = {
        "height_m": _round(height),
        "width_m": _round(width),
        "depth_m": _round(depth),
        "component_count": len(member_boxes),
    }
    return metrics, findings


# ---------------------------------------------------------------------------
# industrial design
# ---------------------------------------------------------------------------

#: material slots with a documented silent fallback (resolve_material /
#: material_preset_name) — never flagged as "missing from materials[]"
IMPLICIT_MATERIAL_SLOTS = ("hardware", "default")
#: weathering spread across declared slots above which the aging reads as
#: inconsistent rather than intentional patina
WEATHERING_SPREAD_THRESHOLD = 0.5


def _eval_design(spec: dict) -> Tuple[dict, List[dict]]:
    prims = compute_primitives(spec)  # raises on a broken spec
    findings: List[dict] = []

    materials = [m for m in (spec.get("materials") or []) if isinstance(m, dict)]
    declared_slots = {m.get("slot") for m in materials}

    used_slots = sorted({
        p.material_slot for p in prims
        if not p.cut and p.material_slot not in IMPLICIT_MATERIAL_SLOTS
    })
    for slot in used_slots:
        if slot not in declared_slots:
            findings.append(_finding(
                "warning", "missing_material",
                f"Material slot '{slot}' is used by geometry but isn't "
                f"declared in materials[] — it falls back to a default look.",
            ))

    for m in materials:
        if not m.get("preset"):
            findings.append(_finding(
                "info", "missing_preset",
                f"Material slot '{m.get('slot', '?')}' has no preset "
                f"assigned.",
            ))

    colors = []
    weatherings = []
    for m in materials:
        slot = m.get("slot")
        if not isinstance(slot, str):
            continue
        props = resolve_material(spec, slot)
        colors.append(props["base_color"])
        weatherings.append(props.get("weathering", 0.0))

    distinct_colors = len(set(colors))
    if len(materials) >= 2 and distinct_colors == 1:
        findings.append(_finding(
            "info", "monochrome_palette",
            "Every material slot resolves to the same color — a "
            "monochrome palette.",
        ))

    if weatherings:
        spread = max(weatherings) - min(weatherings)
        if spread > WEATHERING_SPREAD_THRESHOLD:
            findings.append(_finding(
                "info", "weathering_spread",
                f"Weathering varies by {spread:.2f} across slots — such a "
                f"wide spread usually reads as a mistake rather than "
                f"intentional patina.",
            ))

    preset_coverage = (
        sum(1 for m in materials if m.get("preset")) / len(materials)
        if materials else 1.0
    )
    metrics = {
        "slot_count": len(materials),
        "preset_coverage_fraction": _round(preset_coverage),
        "distinct_color_count": distinct_colors,
    }
    return metrics, findings


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

_EVALUATORS = {
    "architecture": _eval_architecture,
    "mechanical": _eval_mechanical,
    "civil": _eval_civil,
    "design": _eval_design,
}


def evaluate_perspective(pid: str, spec: dict) -> dict:
    """Run one discipline evaluator against ``spec``. Never mutates the
    input. Raises ValueError for an unknown perspective id; any other
    failure (most commonly a spec broken badly enough that
    ``compute_primitives`` raises) is caught and reported as a single
    ``build_failure`` finding with empty metrics instead of propagating."""
    meta = _PERSPECTIVE_BY_ID.get(pid)
    if meta is None:
        raise ValueError(f"Unknown perspective: {pid!r}")

    try:
        metrics, findings = _EVALUATORS[pid](spec)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any build
        # failure must degrade to a finding, never bubble out of this API
        metrics, findings = {}, [_finding("error", "build_failure", str(exc))]

    return {
        "id": meta["id"], "label": meta["label"], "icon": meta["icon"],
        "metrics": metrics, "findings": findings,
    }


def evaluate_all(spec: dict) -> List[dict]:
    """Run all four discipline evaluators, in PERSPECTIVES order."""
    return [evaluate_perspective(p["id"], spec) for p in PERSPECTIVES]
