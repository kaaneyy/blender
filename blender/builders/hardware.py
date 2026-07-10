"""Connection-hardware orchestrator v4: declared intent + engineered joints,
one joint per physical junction.

For every place two *different* components genuinely intersect, this emits
hardware the way a fabricator would detail it. The spec's optional
``connections`` array declares joint intent (welded / slip-fit / carriage
bolts / flange splice / band clamp / through-bolt / anchor base / none) and
is honored at matching contacts; geometric inference remains the fallback:

* **Through-bolt assemblies** — shaft spans the actual joint (overlap depth
  plus up to 25 mm of embedment into each member), hex head + flat washer on
  one face, flat washer + hex nut on the opposite face; diameter scales with
  the joint face; wide joints get 2-/4-bolt patterns with edge distances.
* **Split band clamps** — horizontal round member meeting a vertical pole:
  a two-piece saddle band with ear tabs, bolted through the ears.
* **Slip fitters** — round-over-round vertical fits (post-top luminaires):
  collar + 3 radial set screws instead of a nonsense vertical through-bolt.
* **Carriage bolts** — wood decking on metal frames (vertical axis): dome
  head proud of the timber, washer + hex nut on the steel side.
* **Anchor bases** — vertical structural members landing at grade with no
  modeled base get the full ground package (plate + anchor circle + grout +
  gussets) from :mod:`connections`.
* Declared-only: **weld** fillet rings (and no bolts), **flange splices**,
  **lag screws**.

Joints are collected first, then contact regions belonging to the same
component pair and declaration are MERGED when their boxes touch — a pole
meeting its base plate's grout pad, flange, and gussets is one junction to
a fabricator, not six bolted joints (see :func:`_merge_pair_candidates`).
A standing pipe on a modeled base plate at grade infers a WELD at the seam
where it exits the plate, never a bolt down its own axis. Survivors are
ordered deterministically (anchor bases, declared joints, inferred joints;
position-stable within each rank) so ids survive slider nudges and the
``MAX_JOINTS`` budget keeps structural joints. Rotated primitives use exact
rotated-corner bounding boxes (oriented axis for cylinders/cones), so bolts
only appear where parts really touch; joints with a face too thin to drill
(<10 mm) are skipped. The orchestrator runs on TRANSFORMED geometry (see
base.compute_primitives), so joints land where the user actually placed the
parts and moved components take their hardware with them.

Mirrored 1:1 in frontend/src/builders/hardware.ts. Enabled by the spec
toggle ``connection_hardware``.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

from .base import Primitive
from .connections import (
    carriage_bolt_assembly,
    flange_splice,
    ground_connection,
    lag_screw_assembly,
    slip_fitter,
    split_band_clamp,
    through_bolt_assembly,
    weld_fillet,
)
from .shapes import profile_bounds, resolve_profile

#: kinds treated as round members for band-clamp/slip-fit detection
ROUND_KINDS = ("cylinder", "cone", "sweep", "tube")

MAX_JOINTS = 24
EMBED = 0.025      # max bolt embedment into each member beyond the joint, m
MIN_FACE = 0.010   # skip joints whose bolt face is thinner than this, m
GRID = 0.06        # joint dedupe grid, m

#: connection types a spec's ``connections`` array may declare
CONNECTION_TYPES = (
    "anchor_base", "through_bolt", "flange_splice", "band_clamp",
    "slip_fit", "weld", "carriage_bolt", "lag_screw", "none",
)


def _euler_xyz_matrix(rot: Sequence[float]) -> List[List[float]]:
    """Blender-parity Euler XYZ rotation matrix (shared with edits.py):
    R = Rz·Ry·Rx, X applied first about fixed axes — exactly how the Blender
    realization layer interprets ``obj.rotation_euler`` and how the preview
    renders (Three.js Euler order 'ZYX'). One convention everywhere is what
    keeps multi-axis-rotated parts (base gussets, set screws) in the same
    place in the preview, the AABB math, and the export."""
    x, y, z = rot
    c1, s1 = math.cos(x), math.sin(x)
    c2, s2 = math.cos(y), math.sin(y)
    c3, s3 = math.cos(z), math.sin(z)
    return [
        [c3 * c2, c3 * s2 * s1 - s3 * c1, c3 * s2 * c1 + s3 * s1],
        [s3 * c2, s3 * s2 * s1 + c3 * c1, s3 * s2 * c1 - c3 * s1],
        [-s2, c2 * s1, c2 * c1],
    ]


def _cylinder_axis(rotation: Sequence[float]) -> Tuple[float, float, float]:
    """Unit axis of a cylinder/cone after a Blender XYZ Euler rotation."""
    rx, ry, rz = rotation
    x, y, z = 0.0, -math.sin(rx), math.cos(rx)
    x, z = x * math.cos(ry) + z * math.sin(ry), -x * math.sin(ry) + z * math.cos(ry)
    x, y = x * math.cos(rz) - y * math.sin(rz), x * math.sin(rz) + y * math.cos(rz)
    return (x, y, z)


def _rotated_aabb(loc: Sequence[float], center_off: Sequence[float],
                  h: Sequence[float], rotation: Sequence[float]):
    """World AABB of a rotated local box: rotate its 8 corners and take the
    extremes. Tight (unlike the old max-extent cube), still axis-aligned."""
    m = _euler_xyz_matrix(rotation)
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    for sx in (-1.0, 1.0):
        for sy in (-1.0, 1.0):
            for sz in (-1.0, 1.0):
                v = (center_off[0] + sx * h[0], center_off[1] + sy * h[1],
                     center_off[2] + sz * h[2])
                for k in range(3):
                    w = m[k][0] * v[0] + m[k][1] * v[1] + m[k][2] * v[2]
                    lo[k] = min(lo[k], w)
                    hi[k] = max(hi[k], w)
    center = tuple(loc[k] + (lo[k] + hi[k]) / 2 for k in range(3))
    half = tuple((hi[k] - lo[k]) / 2 for k in range(3))
    return (center, half)


def _aabb(p: Primitive) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
    """(center, half_extents) of the primitive's world AABB. Exact for
    boxes/spheres/tubes and arbitrarily rotated cylinders/cones
    (oriented-cylinder AABB); path/profile-based for sweeps and lathes;
    rotated constructed kinds use tight rotated-corner boxes."""
    loc = p.location
    rotated = any(abs(a) > 1e-6 for a in p.rotation)

    if p.kind == "box":
        sx, sy, sz = p.params["size"]
        h = (sx / 2, sy / 2, sz / 2)
        if rotated:
            return _rotated_aabb(loc, (0.0, 0.0, 0.0), h, p.rotation)
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
        off = (0.0, 0.0, (z0 + z1) / 2)
        if rotated:
            return _rotated_aabb(loc, off, h, p.rotation)
        return ((loc[0], loc[1], loc[2] + off[2]), h)
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
        off = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2,
               (max(zs) + min(zs)) / 2)
        if rotated:
            return _rotated_aabb(loc, off, h, p.rotation)
        return ((loc[0] + off[0], loc[1] + off[1], loc[2] + off[2]), h)
    if p.kind == "loft":
        ps, pe = p.params["profile_start"], p.params["profile_end"]
        h = (
            max(ps["w"], pe["w"]) / 2,
            max(ps["h"], pe["h"]) / 2,
            p.params["depth"] / 2,
        )
        if rotated:
            return _rotated_aabb(loc, (0.0, 0.0, 0.0), h, p.rotation)
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


def _round_radius(prim: Primitive) -> float:
    """Nominal radius of a round-kind member."""
    return prim.params.get("radius") or max(
        prim.params.get("radius_bottom", 0.0), prim.params.get("radius_top", 0.0)
    )


def _is_upright_round(p: Primitive) -> bool:
    return p.kind in ("cylinder", "cone", "tube") and all(
        abs(a) < 1e-3 for a in p.rotation
    )


def _is_pipe_like(p: Primitive) -> bool:
    """An upright round member meaningfully taller than wide — a pole/post,
    not a flange disc or a grout pad."""
    return _is_upright_round(p) and p.params["depth"] >= 2.0 * _round_radius(p)


def _is_vertical_structural(p: Primitive) -> bool:
    """A member that carries load down to grade: an upright round, or an
    unrotated box clearly taller than it is wide."""
    if _is_upright_round(p):
        return True
    if p.kind == "box" and all(abs(a) < 1e-3 for a in p.rotation):
        sx, sy, sz = p.params["size"]
        return sz >= 2.0 * max(sx, sy)
    return False


#: C6: fastener sizing scales with the connection's tributary load tier.
LOAD_FACTOR = {"light": 0.75, "standard": 1.0, "heavy": 1.35}

#: Metric fastener catalog: name -> shaft radius (m). Computed sizes snap to
#: the nearest entry so exports cite real hardware. The human-facing table
#: (grades, torque, clearance holes) lives in standards/us_codes.json under
#: `_connections.bolt_catalog`; a test asserts the two stay in sync.
BOLT_CATALOG = (
    ("M6", 0.003), ("M8", 0.004), ("M10", 0.005), ("M12", 0.006),
    ("M16", 0.008), ("M20", 0.010), ("M24", 0.012),
)

#: rough densities (t/m³) for the moment proxy, keyed by material family
DENSITY = {"metal": 7.9, "concrete": 2.4, "wood": 0.6, "other": 1.0}


def _snap_bolt(r: float) -> Tuple[str, float]:
    """Nearest catalog fastener for a computed shaft radius."""
    return min(BOLT_CATALOG, key=lambda entry: abs(entry[1] - r))


def _volume(p: Primitive) -> float:
    _, h = _aabb(p)
    return 8.0 * h[0] * h[1] * h[2]


def _density(slot: str, spec) -> float:
    preset = _preset_name(slot, spec)
    if preset is None or preset in METAL_PRESETS:
        return DENSITY["metal"]
    if preset == "concrete":
        return DENSITY["concrete"]
    if preset in WOOD_PRESETS:
        return DENSITY["wood"]
    return DENSITY["other"]


def _moment(pa: Primitive, pb: Primitive, center: Sequence[float], spec) -> float:
    """Tributary-moment proxy for the joint: each member's bounding mass
    times its horizontal lever arm about the joint; the larger governs. A
    cantilevered arm outranks an equal-volume compact mass (heuristic
    fabrication convention, not FEA)."""
    best = 0.0
    for p in (pa, pb):
        c, _ = _aabb(p)
        lever = max(0.05, math.hypot(c[0] - center[0], c[1] - center[1]))
        best = max(best, _volume(p) * _density(p.material_slot, spec) * lever)
    return best


def _load_class_from_moment(moment: float) -> str:
    if moment > 0.12:
        return "heavy"
    if moment < 0.004:
        return "light"
    return "standard"


def _bolt_pattern(d1: float, d2: float, head_r: float,
                  round_face: bool = False,
                  count: Optional[int] = None) -> List[Tuple[float, float]]:
    """Bolt offsets on the joint face: 1 center bolt for small faces, a
    2-bolt row along a long face, a 4-bolt pattern for plate-like faces —
    all with real edge distances (>= 1.5d from the overlap edge). On a round
    face the offsets shrink so corner bolts stay inside the circle. An
    explicit ``count`` overrides the pattern with an evenly spaced row along
    the longer face axis."""
    scale = 0.7 if round_face else 1.0
    if count:
        if count == 1:
            return [(0.0, 0.0)]
        along_1 = d1 >= d2
        span = 0.6 * (d1 if along_1 else d2) * scale
        out = []
        for i in range(count):
            o = -span / 2 + span * i / (count - 1)
            out.append((o, 0.0) if along_1 else (0.0, o))
        return out
    edge = 1.5 * head_r
    big1 = d1 >= 0.22 and d1 / 2 - 0.3 * d1 >= edge
    big2 = d2 >= 0.22 and d2 / 2 - 0.3 * d2 >= edge
    o1, o2 = 0.3 * d1 * scale, 0.3 * d2 * scale
    if big1 and big2:
        return [(-o1, -o2), (o1, -o2), (-o1, o2), (o1, o2)]
    if big1:
        return [(-o1, 0.0), (o1, 0.0)]
    if big2:
        return [(0.0, -o2), (0.0, o2)]
    return [(0.0, 0.0)]


#: preset names treated as structural metal for fastener appropriateness.
METAL_PRESETS = {
    "galvanized_steel", "cast_iron", "brushed_aluminum",
    "powder_coat_black", "powder_coat_green",
}

#: preset names treated as timber (carriage-bolt territory)
WOOD_PRESETS = {"wood_slat"}


def _preset_name(slot: str, spec) -> Optional[str]:
    if spec is None:
        return None
    from .base import material_preset_name

    return material_preset_name(spec, slot)


def _is_soft(slot: str, spec) -> bool:
    """True when a member's material is non-metal (wood/concrete/lens). With
    no spec (direct test calls) everything is treated as metal, preserving
    the pre-material-awareness behavior."""
    preset = _preset_name(slot, spec)
    return preset is not None and preset not in METAL_PRESETS


def _is_wood(slot: str, spec) -> bool:
    preset = _preset_name(slot, spec)
    return preset is not None and preset in WOOD_PRESETS


# ---------------------------------------------------------------------------
# Declared connections (the spec's `connections` array)
# ---------------------------------------------------------------------------

def _spec_connections(spec) -> List[dict]:
    if not isinstance(spec, dict):
        return []
    out = []
    for c in spec.get("connections") or []:
        if (
            isinstance(c, dict)
            and isinstance(c.get("a"), str)
            and isinstance(c.get("b"), str)
            and c.get("type") in CONNECTION_TYPES
        ):
            out.append(c)
    return out


def _side_matches(ref: str, p: Primitive) -> bool:
    return ref == p.component or ref == f"{p.component}/{p.name}"


def _find_declaration(decls: List[dict], pa: Primitive, pb: Primitive) -> Optional[dict]:
    """First declaration whose {a, b} matches this prim pair (unordered;
    component or component/part paths)."""
    for d in decls:
        a, b = d["a"], d["b"]
        if b == "ground":
            continue  # ground declarations are handled by the anchor pass
        if (_side_matches(a, pa) and _side_matches(b, pb)) or (
            _side_matches(a, pb) and _side_matches(b, pa)
        ):
            return d
    return None


def _ground_declaration(decls: List[dict], p: Primitive) -> Optional[dict]:
    for d in decls:
        if d["b"] == "ground" and _side_matches(d["a"], p):
            return d
        if d["a"] == "ground" and _side_matches(d["b"], p):
            return d
    return None


def _round_about_axis(p: Primitive, axis: int) -> bool:
    """True when the member is round and its cylinder axis is the bolt axis —
    its joint face is a circle, so bolt offsets must stay inside it."""
    if p.kind not in ("cylinder", "cone", "tube"):
        return False
    u = _cylinder_axis(p.rotation)
    return abs(u[axis]) > 0.9


def _member_direction(p: Primitive) -> Tuple[float, float, float]:
    """Unit direction a member runs along: the cylinder axis for round kinds,
    the path chord for sweeps, +Z otherwise."""
    if p.kind == "sweep":
        path = p.params.get("path") or []
        if len(path) >= 2:
            dx = path[-1][0] - path[0][0]
            dy = path[-1][1] - path[0][1]
            dz = path[-1][2] - path[0][2]
            n = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
            return (dx / n, dy / n, dz / n)
    if p.kind in ("cylinder", "cone", "tube"):
        return _cylinder_axis(p.rotation)
    return (0.0, 0.0, 1.0)


def _is_telescoping_fit(pa: Primitive, pb: Primitive, center) -> bool:
    """Post-top slip fit: two coaxial upright pipe-like members (each much
    longer than wide) telescoping vertically with meaningfully different
    radii — a luminaire fitter over a pole tenon, not a pole on a flange
    disc (a disc's depth fails the pipe-like test)."""
    if not (_is_upright_round(pa) and _is_upright_round(pb)):
        return False
    for p in (pa, pb):
        if p.params["depth"] < 2.0 * _round_radius(p):
            return False  # disc/flange, not a pipe
    ra, rb = _radius_at_z(pa, center[2]), _radius_at_z(pb, center[2])
    big, small = max(ra, rb), min(ra, rb)
    if small <= 0 or (big - small) / big < 0.15:
        return False
    dx = pa.location[0] - pb.location[0]
    dy = pa.location[1] - pb.location[1]
    return math.hypot(dx, dy) <= 0.3 * big  # coaxial


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

#: contact regions closer than this (m) belong to the same physical junction
MERGE_TOL = 0.005


def _contact_gap(a: dict, b: dict) -> float:
    """Euclidean gap between two candidates' contact boxes (0 = touching)."""
    d2 = 0.0
    for k in range(3):
        g = max(a["lo"][k] - b["hi"][k], b["lo"][k] - a["hi"][k], 0.0)
        if g > 0:
            d2 += g * g
    return math.sqrt(d2)


def _better_candidate(c1: dict, c2: dict) -> bool:
    """Which contact represents a merged junction: larger joint face, then
    deeper overlap, then the smaller (stable) grid key."""
    a1, a2 = c1["d1"] * c1["d2"], c2["d1"] * c2["d2"]
    if a1 != a2:
        return a1 > a2
    o1 = c1["hi"][c1["axis"]] - c1["lo"][c1["axis"]]
    o2 = c2["hi"][c2["axis"]] - c2["lo"][c2["axis"]]
    if o1 != o2:
        return o1 > o2
    return c1["key"] < c2["key"]


def _merge_pair_candidates(cands: List[dict]) -> List[dict]:
    """One physical junction -> one joint. A pole meeting its base plate
    touches the grout pad, the flange, AND every gusset — six AABB contacts
    that are ONE junction to a fabricator (the old code bolted each of them,
    which is where horizontal bolts through poles came from). Candidates for
    the same component pair and same declaration whose contact boxes touch
    collapse into the best-faced one; genuinely separate contact regions
    (three bench slats along a rail) keep their own joints."""
    groups: Dict[tuple, List[dict]] = {}
    for c in cands:
        key = (tuple(sorted((c["pa"].component, c["pb"].component))), c["decl_idx"])
        groups.setdefault(key, []).append(c)
    out: List[dict] = []
    for arr in groups.values():
        remaining = list(range(len(arr)))
        while remaining:
            blob = [remaining.pop(0)]
            grew = True
            while grew:
                grew = False
                for i in list(remaining):
                    if any(_contact_gap(arr[i], arr[j]) <= MERGE_TOL for j in blob):
                        remaining.remove(i)
                        blob.append(i)
                        grew = True
            best = blob[0]
            for i in blob[1:]:
                if _better_candidate(arr[i], arr[best]):
                    best = i
            winner = arr[best]
            # where the junction's full-face contact tops out — a pipe welded
            # into a stack of base discs carries its bead at the seam where
            # it exits the TOPMOST disc, not the first one it touches (small
            # side contacts like gusset slivers don't count)
            face = winner["d1"] * winner["d2"]
            winner["junction_top"] = max(
                arr[i]["hi"][2] for i in blob
                if arr[i]["d1"] * arr[i]["d2"] >= 0.8 * face
            )
            out.append(winner)
    return out


def compute_hardware(prims: List[Primitive], spec=None) -> List[Primitive]:
    """Emit visible connection hardware. Candidates are collected first
    (inter-component contacts + anchor bases), ordered deterministically
    (anchor bases, then declared joints, then inferred; position-stable), and
    dispatched to the emitter matching the declared or inferred connection
    type. Joints between two non-metal members with no declaration get no
    bolts — real furniture uses concealed joinery."""
    decls = _spec_connections(spec)
    boxes = [
        (p, *_aabb(p))
        for p in prims
        if p.component != "hardware" and not p.cut
    ]

    pair_cands: List[dict] = []
    seen: set = set()

    # -------------------------------------------------- pair contacts
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

            decl = _find_declaration(decls, pa, pb)
            if decl is not None and decl["type"] == "none":
                continue  # explicitly no visible hardware
            if decl is None and _is_soft(pa.material_slot, spec) and _is_soft(pb.material_slot, spec):
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

            # structural (high-moment) inferred joints outrank light ones so
            # the MAX_JOINTS budget never drops a mast arm for a trim strip
            moment = _moment(pa, pb, center, spec)
            rank = 1 if decl else (2 if moment >= 0.01 else 3)
            pair_cands.append({
                "kind": "pair", "rank": rank, "key": key, "moment": moment,
                "pa": pa, "pb": pb, "ca": ca, "ha": ha, "cb": cb, "hb": hb,
                "lo": lo, "hi": hi, "center": center,
                "axis": axis, "perp": perp, "d1": d1, "d2": d2, "decl": decl,
                "decl_idx": decls.index(decl) if decl else -1,
            })

    # one joint per physical junction (see _merge_pair_candidates)
    candidates: List[dict] = _merge_pair_candidates(pair_cands)

    # -------------------------------------------------- anchor bases
    anchor_seen: set = set()
    for p, c, h in boxes:
        if not _is_vertical_structural(p):
            continue
        if c[2] - h[2] > 0.01:  # bottom must land at grade
            continue
        gdecl = _ground_declaration(decls, p)
        if gdecl is not None and gdecl["type"] == "none":
            continue
        forced = gdecl is not None and gdecl["type"] == "anchor_base"
        if not forced:
            if _is_soft(p.material_slot, spec):
                continue  # timber posts don't get anchor flanges uninvited
            if _volume(p) < 0.01:
                continue  # light member — leveling feet territory, not anchors
        # a modeled base (any other component at this member's foot) wins
        foot_r = max(h[0], h[1])
        has_base = any(
            q.component != p.component
            and qc[2] + qh[2] <= 0.15
            and abs(qc[0] - c[0]) < foot_r + qh[0]
            and abs(qc[1] - c[1]) < foot_r + qh[1]
            for q, qc, qh in boxes
        )
        if has_base:
            continue
        key = (p.component, round(c[0] / 0.1), round(c[1] / 0.1))
        if key in anchor_seen:
            continue
        anchor_seen.add(key)
        if p.kind == "box":
            member_r = max(p.params["size"][0], p.params["size"][1]) / 2
            shape = "square"
        else:
            member_r = _radius_at_z(p, 0.0)
            shape = "round"
        load = gdecl.get("load") if gdecl else None
        candidates.append({
            "kind": "anchor", "rank": 0,
            "key": (round(c[0] / GRID), round(c[1] / GRID), 0),
            "center_xy": (c[0], c[1]), "member_r": member_r, "shape": shape,
            "slot": p.material_slot, "component": p.component,
            "load": load or ("heavy" if _volume(p) > 0.15 else "standard"),
        })

    # -------------------------------------------------- deterministic order
    candidates.sort(key=lambda cand: (cand["rank"], cand["key"]))

    out: List[Primitive] = []
    joint = 0
    for cand in candidates:
        if joint >= MAX_JOINTS:
            break
        emitted = _dispatch(joint + 1, cand, spec)
        if emitted:
            joint += 1
            out.extend(emitted)
    return out


#: heuristic anchor-rod torque per tier (see us_codes.json _connections)
ANCHOR_TORQUE = {"light": 100, "standard": 220, "heavy": 400}


def _with_joint_meta(emitted: List[Primitive], record: dict) -> List[Primitive]:
    """Attach the joint record to the first emitted prim so the schedule can
    enumerate exactly what was generated."""
    from dataclasses import replace

    if not emitted:
        return emitted
    return [replace(emitted[0], meta={"joint": record})] + emitted[1:]


def _catalog_row(name: str) -> dict:
    return {"grade": "8.8 / A325", "code_ref": "AISC J3 / RCSC Table 8.1",
            "torque_nm": {"M6": 10, "M8": 25, "M10": 50, "M12": 85,
                          "M16": 210, "M20": 425, "M24": 730}[name]}


def _dispatch(joint: int, cand: dict, spec) -> List[Primitive]:
    """Emit one joint's hardware from a candidate record, snapping fastener
    sizes to the catalog and stamping a joint record (type, members,
    fastener, count, torque) onto the first prim for the joint schedule."""
    if cand["kind"] == "anchor":
        n_bolts = {"light": 4, "standard": 4, "heavy": 6}[cand["load"]]
        dia_mm = {"light": 16, "standard": 22, "heavy": 28}[cand["load"]]
        return _with_joint_meta(
            ground_connection(
                cand["member_r"], mount="flange", load_class=cand["load"],
                component="hardware", slot=cand["slot"],
                center=cand["center_xy"], shape=cand["shape"],
                name_prefix=f"joint{joint}_",
            ),
            {"id": joint, "type": "anchor_base",
             "a": cand["component"], "b": "ground",
             "fastener": f"{dia_mm}mm anchor bolt", "count": n_bolts,
             "grade": "F1554 Gr.55", "torque_nm": ANCHOR_TORQUE[cand["load"]],
             "code_ref": "AASHTO LTS-6 / ACI 318-19 Ch.17",
             "center": (cand["center_xy"][0], cand["center_xy"][1], 0.0)},
        )

    pa, pb = cand["pa"], cand["pb"]
    lo, hi, center = cand["lo"], cand["hi"], cand["center"]
    axis, perp, d1, d2 = cand["axis"], cand["perp"], cand["d1"], cand["d2"]
    decl = cand["decl"]
    ca, ha, cb, hb = cand["ca"], cand["ha"], cand["cb"], cand["hb"]

    upright = pa if _is_upright_round(pa) else pb if _is_upright_round(pb) else None
    other = pb if upright is pa else pa

    # ------------------------------------------------ pick the joint type
    pipe = pa if _is_pipe_like(pa) else pb if _is_pipe_like(pb) else None
    ctype = decl["type"] if decl else None
    if ctype is None:
        base_c, base_h = (cb, hb) if pipe is pa else (ca, ha)
        if _is_telescoping_fit(pa, pb, center):
            ctype = "slip_fit"
        elif (
            axis != 2
            and upright is not None
            and not _is_upright_round(other)
            and other.kind in ROUND_KINDS
        ):
            ctype = "band_clamp"
        elif axis == 2 and pipe is not None and base_c[2] + base_h[2] <= 0.15:
            # a standing pipe on a modeled base plate at grade is shop-welded
            # into it — never bolted down its own axis
            ctype = "weld"
        elif axis == 2 and (_is_wood(pa.material_slot, spec) != _is_wood(pb.material_slot, spec)):
            ctype = "carriage_bolt"
        else:
            ctype = "through_bolt"

    # declared types that need geometry they don't have fall back to bolts
    if ctype == "band_clamp" and upright is None:
        ctype = "through_bolt"
    if ctype == "slip_fit" and not (
        pa.kind in ("cylinder", "cone", "tube") or pb.kind in ("cylinder", "cone", "tube")
    ):
        ctype = "through_bolt"
    if ctype == "anchor_base":  # pair-declared anchor_base has no grade side
        ctype = "through_bolt"

    def record(fastener: str, count: int, grade: str = "", torque=None,
               code_ref: str = "") -> dict:
        # geometric context (center, bolt axis, overlap depth, face size)
        # rides along so the connection auditor can verify the joint without
        # re-deriving contact detection
        return {"id": joint, "type": ctype, "a": pa.component, "b": pb.component,
                "fastener": fastener, "count": count, "grade": grade,
                "torque_nm": torque, "code_ref": code_ref,
                "center": tuple(center), "axis": axis,
                "overlap": hi[axis] - lo[axis], "face": (d1, d2)}

    # ------------------------------------------------ emit
    if ctype == "weld":
        round_m = upright or (pa if pa.kind in ROUND_KINDS else pb if pb.kind in ROUND_KINDS else None)
        if round_m is None:
            return []  # shop weld with no round member: nothing visible
        # a vertical member welded into a lower part carries the bead at the
        # seam where it exits that part, not at the overlap's midpoint
        weld_z = center[2]
        if axis == 2 and round_m is not None:
            seam = cand.get("junction_top", hi[2])
            rc, rh = (ca, ha) if round_m is pa else (cb, hb)
            if rc[2] + rh[2] > seam + 0.01:
                weld_z = seam
        r = (
            _radius_at_z(round_m, weld_z)
            if round_m.kind in ("cylinder", "cone", "tube") and _is_upright_round(round_m)
            else _round_radius(round_m)
        )
        return _with_joint_meta(
            [weld_fillet(
                r, max(0.008, r * 0.2), weld_z, "hardware", "hardware",
                name=f"joint{joint}_weld",
                center=(round_m.location[0], round_m.location[1]),
            )],
            record("fillet weld", 1, grade="E70XX", code_ref="AWS D1.1 (heuristic)"),
        )

    if ctype == "band_clamp":
        arm_r = _round_radius(other) if other.kind in ROUND_KINDS else min(d1, d2) / 2
        # ear bolts run along the ARM's horizontal direction (ears sit on the
        # pole's flanks, clear of the arm), not the overlap box's thin axis
        direction = _member_direction(other)
        axis_h = 0 if abs(direction[0]) >= abs(direction[1]) else 1
        ear_name, _ = _snap_bolt(min(max(0.4 * arm_r, 0.004), 0.008))
        row = _catalog_row(ear_name)
        return _with_joint_meta(
            split_band_clamp(
                joint, _radius_at_z(upright, center[2]),
                (upright.location[0], upright.location[1]), center[2],
                axis_h, arm_r,
            ),
            record(f"{ear_name} ear bolt (split band clamp)", 2,
                   grade=row["grade"], torque=row["torque_nm"],
                   code_ref=row["code_ref"]),
        )

    if ctype == "slip_fit":
        round_prims = [p for p in (pa, pb) if p.kind in ("cylinder", "cone", "tube")]
        outer = max(round_prims, key=lambda p: _round_radius(p))
        outer_r = (
            _radius_at_z(outer, center[2]) if _is_upright_round(outer)
            else _round_radius(outer)
        )
        return _with_joint_meta(
            slip_fitter(joint, outer_r, (outer.location[0], outer.location[1]),
                        center[2]),
            record("M8 set screw (slip fitter)", 3, grade="45H",
                   torque=15, code_ref="pole-fitter convention (heuristic)"),
        )

    if ctype == "flange_splice":
        member_r = min(d1, d2) / 2
        n_bolts = (decl or {}).get("count") or 6
        splice_name, _ = _snap_bolt(min(max(0.35 * member_r, 0.005), 0.012))
        row = _catalog_row(splice_name)
        return _with_joint_meta(
            flange_splice(joint, center, axis, member_r, n_bolts=n_bolts),
            record(f"{splice_name} flange bolt", n_bolts, grade=row["grade"],
                   torque=row["torque_nm"], code_ref=row["code_ref"]),
        )

    # bolted family: through / carriage / lag — snapped to the catalog
    load = (decl or {}).get("load") or _load_class_from_moment(cand.get("moment", 0.0))
    factor = LOAD_FACTOR.get(load, 1.0)
    bolt_name, shaft_r = _snap_bolt(
        min(max(0.22 * min(d1, d2) * factor, 0.004), 0.012)
    )
    above = max(ca[axis] + ha[axis], cb[axis] + hb[axis]) - hi[axis]
    below = lo[axis] - min(ca[axis] - ha[axis], cb[axis] - hb[axis])
    span_hi = hi[axis] + min(above, EMBED)
    span_lo = lo[axis] - min(below, EMBED)
    round_face = _round_about_axis(pa, axis) or _round_about_axis(pb, axis)
    pattern = _bolt_pattern(d1, d2, 1.8 * shaft_r, round_face,
                            (decl or {}).get("count"))

    out: List[Primitive] = []
    for idx, (o1, o2) in enumerate(pattern, start=1):
        c = list(center)
        c[perp[0]] += o1
        c[perp[1]] += o2
        if ctype == "carriage_bolt":
            # dome goes on the timber face
            wood = pa if _is_wood(pa.material_slot, spec) else pb
            dome_at_hi = True if spec is None else wood.location[axis] >= center[axis]
            out.extend(carriage_bolt_assembly(joint, idx, c, axis, shaft_r,
                                              span_lo, span_hi, dome_at_hi))
        elif ctype == "lag_screw":
            out.extend(lag_screw_assembly(joint, idx, c, axis, shaft_r,
                                          span_lo, span_hi))
        else:
            out.extend(through_bolt_assembly(joint, idx, c, axis, shaft_r,
                                             span_lo, span_hi))
    row = _catalog_row(bolt_name)
    label = {"carriage_bolt": "carriage bolt", "lag_screw": "lag screw"}.get(
        ctype, "through bolt")
    return _with_joint_meta(
        out,
        record(f"{bolt_name} {label}", len(pattern), grade=row["grade"],
               torque=row["torque_nm"], code_ref=row["code_ref"]),
    )
