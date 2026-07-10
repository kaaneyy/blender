"""Deterministic connection auditor — the machine behind the app's
"Check connections" button.

Recomputes the asset with connection hardware forced on, then walks every
joint and component the way a fabricator checks a shop drawing:

* **floating** — a component with no load path to the ground;
* **below_grade** — geometry dipping below z=0;
* **dead_declaration** — a declared connection referencing parts that don't
  exist in the geometry (and aren't just toggled off);
* **gap_declaration** — a declared connection whose parts don't actually
  touch, so no hardware can be placed;
* **sliver** — a bolted joint whose members merely graze (overlap thinner
  than a washer), leaving the bolts nothing real to clamp;
* **detached** — generated hardware that sticks out into thin air instead
  of attaching to its members (each prim must touch a member or chain to
  one through its own assembly);
* **collision** — a joint's hardware buried inside an unrelated third
  component.

Every finding carries a machine-applicable fix — a small list of data ops
on the spec (declare/undeclare a connection, nudge a component's offset) —
plus human-readable before/after lines for the UI's hover preview. Nothing
is applied here: :func:`apply_audit_fixes` runs only after the user
confirms.

Mirrored 1:1 in frontend/src/builders/audit.ts — keep in exact lockstep.
"""
from __future__ import annotations

import copy
import math
import re
from typing import Dict, List, Optional, Sequence, Tuple

from .base import Primitive, compute_primitives
from .hardware import _aabb

Box = Tuple[Tuple[float, float, float], Tuple[float, float, float]]

#: parts closer than this (m) count as touching (matches connectivity.py)
CONTACT_TOL = 0.0005
#: a component whose lowest point is within this of z=0 is grounded
GROUND_TOL = 0.005
#: fixes make parts interpenetrate by this much (mid of the 10-20mm rule)
EMBED_FIX = 0.014
#: bolted joints thinner than this clamp on a knife edge
SLIVER = 0.006
#: hardware attachment tolerance (m)
ATTACH_TOL = 0.002
#: the ground, as a box whose top face is grade
GROUND_BOX: Box = ((0.0, 0.0, -0.5), (1e9, 1e9, 0.5 + CONTACT_TOL))

BOLTED_TYPES = ("through_bolt", "carriage_bolt", "lag_screw")


def _mm(v: float) -> int:
    return int(round(v * 1000))


def _finding(fid: str, severity: str, kind: str, title: str, detail: str,
             component: Optional[str] = None, joint: Optional[int] = None,
             fix: Optional[dict] = None) -> dict:
    return {"id": fid, "severity": severity, "kind": kind, "title": title,
            "detail": detail, "component": component, "joint": joint,
            "fix": fix}


def _fix(summary: str, before: str, after: str, ops: List[dict]) -> dict:
    return {"summary": summary, "before": before, "after": after, "ops": ops}


def _gap(a: Box, b: Box) -> float:
    (ca, ha), (cb, hb) = a, b
    d2 = 0.0
    for k in range(3):
        g = abs(ca[k] - cb[k]) - (ha[k] + hb[k])
        if g > 0:
            d2 += g * g
    return math.sqrt(d2)


def _intersects(a: Box, b: Box, tol: float = 0.0) -> bool:
    (ca, ha), (cb, hb) = a, b
    return all(abs(ca[k] - cb[k]) <= ha[k] + hb[k] + tol for k in range(3))


def _overlap_volume(a: Box, b: Box) -> float:
    (ca, ha), (cb, hb) = a, b
    v = 1.0
    for k in range(3):
        o = min(ca[k] + ha[k], cb[k] + hb[k]) - max(ca[k] - ha[k], cb[k] - hb[k])
        if o <= 0:
            return 0.0
        v *= o
    return v


def _volume(box: Box) -> float:
    return 8.0 * box[1][0] * box[1][1] * box[1][2]


def _component_gap(boxes_a: List[Box], boxes_b: List[Box]) -> float:
    return min(_gap(a, b) for a in boxes_a for b in boxes_b)


def _closing_delta(boxes_move: List[Box], boxes_target: List[Box]) -> Tuple[float, float, float]:
    """Offset that moves `boxes_move` toward `boxes_target` so the closest
    box pair interpenetrates EMBED_FIX on every currently-separating axis."""
    best = None
    best_d = math.inf
    for a in boxes_move:
        for b in boxes_target:
            d = _gap(a, b)
            if d < best_d:
                best_d = d
                best = (a, b)
    (ca, ha), (cb, hb) = best
    delta = [0.0, 0.0, 0.0]
    for k in range(3):
        g = abs(ca[k] - cb[k]) - (ha[k] + hb[k])
        if g > -EMBED_FIX:
            sign = 1.0 if cb[k] >= ca[k] else -1.0
            delta[k] = sign * (g + EMBED_FIX)
    return tuple(delta)


def _fmt_delta(delta: Sequence[float]) -> str:
    axes = "xyz"
    parts = [f"{axes[k]} {'+' if delta[k] >= 0 else '−'}{_mm(abs(delta[k]))}mm"
             for k in range(3) if abs(delta[k]) > 1e-9]
    return ", ".join(parts) if parts else "no move"


def _with_hardware(spec: dict) -> dict:
    s = copy.deepcopy(spec)
    toggles = s.setdefault("toggles", [])
    for t in toggles:
        if t.get("id") == "connection_hardware":
            t["value"] = True
            break
    else:
        toggles.append({"id": "connection_hardware",
                        "label": "Connection Hardware", "value": True})
    return s


def _declared_pair(spec: dict, a: str, b: str) -> Optional[dict]:
    for c in spec.get("connections") or []:
        if not isinstance(c, dict):
            continue
        if {c.get("a"), c.get("b")} == {a, b}:
            return c
    return None


def audit_connections(spec: dict) -> dict:
    """Run every check and return {'findings', 'joints', 'components'} —
    findings ordered errors-first, position-stable within severity."""
    findings: List[dict] = []
    try:
        prims = compute_primitives(_with_hardware(spec))
    except Exception as e:  # pragma: no cover - defensive
        return {"findings": [_finding(
            "build:error", "error", "build",
            "The asset does not build",
            f"Geometry computation failed: {e}",
        )], "joints": 0, "components": 0}

    members = [p for p in prims if p.component != "hardware" and not p.cut]
    hardware = [p for p in prims if p.component == "hardware"]

    member_boxes: Dict[str, List[Box]] = {}
    for p in members:
        member_boxes.setdefault(p.component, []).append(_aabb(p))
    comps = sorted(member_boxes)

    # hardware grouped by joint, with each joint's record
    joint_prims: Dict[int, List[Tuple[Primitive, Box]]] = {}
    joint_records: Dict[int, dict] = {}
    for p in hardware:
        m = re.match(r"^joint(\d+)_", p.name)
        if not m:
            continue
        jid = int(m.group(1))
        joint_prims.setdefault(jid, []).append((p, _aabb(p)))
        if p.meta and isinstance(p.meta.get("joint"), dict):
            joint_records[jid] = p.meta["joint"]

    # one positional fix per component per audit round: stacking two nudges
    # computed against the same starting position would overshoot. Later
    # findings keep reporting but defer their fix to the next re-check.
    nudged: set = set()

    # ---------------------------------------------------------- load paths
    touching: Dict[str, set] = {c: set() for c in comps}
    for i in range(len(comps)):
        for j in range(i + 1, len(comps)):
            if _component_gap(member_boxes[comps[i]], member_boxes[comps[j]]) <= CONTACT_TOL:
                touching[comps[i]].add(comps[j])
                touching[comps[j]].add(comps[i])
    grounded = {
        c for c in comps
        if min(b[0][2] - b[1][2] for b in member_boxes[c]) <= GROUND_TOL
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
        supported = [s for s in comps if s in reached]
        if supported:
            nearest = min(supported,
                          key=lambda s: _component_gap(member_boxes[c], member_boxes[s]))
            gap_mm = _mm(_component_gap(member_boxes[c], member_boxes[nearest]))
            delta = _closing_delta(member_boxes[c], member_boxes[nearest])
            nudged.add(c)
            findings.append(_finding(
                f"floating:{c}", "error", "floating",
                f"'{c}' floats in mid-air",
                f"It has no load path to the ground — its nearest support "
                f"'{nearest}' is {gap_mm}mm away. Nothing holds it up, so it "
                f"can't be built.",
                component=c,
                fix=_fix(
                    f"Move '{c}' onto '{nearest}' so they overlap {_mm(EMBED_FIX)}mm",
                    f"'{c}' hangs {gap_mm}mm from '{nearest}'",
                    f"'{c}' seats into '{nearest}' ({_fmt_delta(delta)})",
                    [{"op": "nudge", "key": c, "delta": list(delta)}],
                ),
            ))
        else:
            findings.append(_finding(
                f"floating:{c}", "error", "floating",
                f"'{c}' floats in mid-air",
                "No component reaches the ground at all — nothing can carry "
                "load to grade.",
                component=c,
            ))

    # ---------------------------------------------------------- below grade
    for c in comps:
        low = min(b[0][2] - b[1][2] for b in member_boxes[c])
        if low < -GROUND_TOL:
            fix = None
            if c not in nudged:
                nudged.add(c)
                fix = _fix(
                    f"Raise '{c}' {_mm(-low)}mm so it sits on grade",
                    f"lowest point {_mm(-low)}mm below grade",
                    "lowest point at grade (z=0)",
                    [{"op": "nudge", "key": c, "delta": [0.0, 0.0, -low]}],
                )
            findings.append(_finding(
                f"below_grade:{c}", "warning", "below_grade",
                f"'{c}' digs {_mm(-low)}mm below grade",
                "Geometry below z=0 would be underground on site — it can't "
                "be fabricated as shown.",
                component=c, fix=fix,
            ))

    # ---------------------------------------------------------- declarations
    declared_components = set(spec.get("components") or [])
    for rp in spec.get("primitives") or []:
        if isinstance(rp, dict) and rp.get("component"):
            declared_components.add(rp["component"])

    def side_boxes(ref: str) -> List[Box]:
        out = []
        for p in members:
            if ref == p.component or ref == f"{p.component}/{p.name}":
                out.append(_aabb(p))
        return out

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
            missing = a if not boxes_a else b
            if missing.split("/")[0] in declared_components:
                continue  # component exists but is toggled off — dormant
            findings.append(_finding(
                f"dead_declaration:{a}~{b}", "warning", "dead_declaration",
                f"Declared joint {a} ↔ {b} references a missing part",
                f"'{missing}' doesn't exist in the geometry, so this "
                f"declaration does nothing.",
                fix=_fix(
                    "Remove the dead declaration",
                    f"connections: {a} ↔ {b} ({d.get('type')})",
                    "declaration removed",
                    [{"op": "undeclare", "a": a, "b": b}],
                ),
            ))
            continue
        gap = _component_gap(boxes_a, boxes_b)
        if gap > CONTACT_TOL:
            mover = a.split("/")[0]
            target_boxes = boxes_b
            # move the smaller side; hardware goes where they overlap
            vol_a = sum(_volume(x) for x in boxes_a)
            vol_b = sum(_volume(x) for x in boxes_b)
            if vol_b < vol_a:
                mover, target_boxes = b.split("/")[0], boxes_a
            mover_boxes = member_boxes.get(mover) or (boxes_a if mover == a.split("/")[0] else boxes_b)
            fix = None
            if mover not in nudged:
                nudged.add(mover)
                delta = _closing_delta(mover_boxes, target_boxes)
                fix = _fix(
                    f"Move '{mover}' to close the {_mm(gap)}mm gap",
                    f"parts {_mm(gap)}mm apart, no contact",
                    f"parts overlap {_mm(EMBED_FIX)}mm ({_fmt_delta(delta)})",
                    [{"op": "nudge", "key": mover, "delta": list(delta)}],
                )
            findings.append(_finding(
                f"gap_declaration:{a}~{b}", "error", "gap_declaration",
                f"Declared joint {a} ↔ {b} has an air gap",
                f"The parts are {_mm(gap)}mm apart — a "
                f"{d.get('type', 'joint')} can't fasten parts that don't "
                f"touch.",
                component=mover, fix=fix,
            ))

    # ---------------------------------------------------------- joint checks
    sliver_seen: set = set()
    for jid in sorted(joint_prims):
        rec = joint_records.get(jid)
        prims_j = joint_prims[jid]
        a = rec.get("a") if rec else None
        b = rec.get("b") if rec else None

        # -- detached hardware: every prim must chain to a member
        anchor_boxes: List[Box] = []
        for side in (a, b):
            if side == "ground":
                anchor_boxes.append(GROUND_BOX)
            elif side and side in member_boxes:
                anchor_boxes.extend(member_boxes[side])
        if anchor_boxes:
            attached = [False] * len(prims_j)
            for i, (_, box) in enumerate(prims_j):
                if any(_intersects(box, mb, ATTACH_TOL) for mb in anchor_boxes):
                    attached[i] = True
            changed = True
            while changed:
                changed = False
                for i, (_, box) in enumerate(prims_j):
                    if attached[i]:
                        continue
                    if any(attached[k] and _intersects(box, prims_j[k][1], ATTACH_TOL)
                           for k in range(len(prims_j))):
                        attached[i] = changed = True
            loose = [p.name for (p, _), ok in zip(prims_j, attached) if not ok]
            if loose:
                pair = f"{a} ↔ {b}" if a and b else f"joint {jid}"
                decl = _declared_pair(spec, a or "", b or "")
                ops = [{"op": "declare", "a": a, "b": b, "type": "none"}] if a and b else []
                findings.append(_finding(
                    f"detached:{jid}", "error", "detached",
                    f"Joint {jid} hardware hangs in mid-air",
                    f"{len(loose)} of its {len(prims_j)} parts (e.g. "
                    f"'{loose[0]}') don't touch either member at {pair} — "
                    f"fasteners must bear on the parts they join.",
                    joint=jid, component=a,
                    fix=_fix(
                        f"Remove the unbuildable hardware at {pair}",
                        f"{len(prims_j)} hardware parts, {len(loose)} floating",
                        f"joint {pair} declared 'none' — no hardware generated",
                        ops,
                    ) if ops and not (decl and decl.get("type") not in (None, "none")) else None,
                ))

        # -- sliver joints: bolted members that merely graze
        if rec and rec.get("type") in BOLTED_TYPES and a and b and b != "ground":
            overlap = rec.get("overlap")
            pair_key = tuple(sorted((a, b)))
            if overlap is not None and overlap < SLIVER and pair_key not in sliver_seen:
                sliver_seen.add(pair_key)
                axis = rec.get("axis", 2)
                boxes_a = member_boxes.get(a, [])
                boxes_b = member_boxes.get(b, [])
                if boxes_a and boxes_b:
                    vol_a = sum(_volume(x) for x in boxes_a)
                    vol_b = sum(_volume(x) for x in boxes_b)
                    mover, target = (a, b) if vol_a <= vol_b else (b, a)
                    fix = None
                    if mover not in nudged:
                        nudged.add(mover)
                        mv_boxes, tg_boxes = member_boxes[mover], member_boxes[target]
                        c_m = sum(x[0][axis] for x in mv_boxes) / len(mv_boxes)
                        c_t = sum(x[0][axis] for x in tg_boxes) / len(tg_boxes)
                        sign = 1.0 if c_t >= c_m else -1.0
                        delta = [0.0, 0.0, 0.0]
                        delta[axis] = sign * (EMBED_FIX - overlap)
                        fix = _fix(
                            f"Seat '{mover}' {_mm(EMBED_FIX - overlap)}mm deeper into '{target}'",
                            f"members overlap {_mm(overlap)}mm",
                            f"members overlap {_mm(EMBED_FIX)}mm ({_fmt_delta(delta)})",
                            [{"op": "nudge", "key": mover, "delta": delta}],
                        )
                    findings.append(_finding(
                        f"sliver:{pair_key[0]}~{pair_key[1]}", "warning", "sliver",
                        f"{a} ↔ {b} barely touch ({_mm(overlap)}mm)",
                        f"The {rec.get('type', 'bolted').replace('_', ' ')}s "
                        f"at joint {jid} clamp on a {_mm(overlap)}mm sliver — "
                        f"thinner than a washer. Real joints overlap 10-20mm.",
                        joint=jid, component=mover, fix=fix,
                    ))

        # -- collisions with unrelated components
        for other in comps:
            if other in (a, b):
                continue
            hit = None
            for p, box in prims_j:
                for mb in member_boxes[other]:
                    ov = _overlap_volume(box, mb)
                    if ov > 0.3 * _volume(box) and _volume(box) > 0:
                        hit = p.name
                        break
                if hit:
                    break
            if hit:
                decl = _declared_pair(spec, a or "", b or "")
                inferred = decl is None and a and b
                findings.append(_finding(
                    f"collision:{jid}:{other}", "warning", "collision",
                    f"Joint {jid} hardware buried inside '{other}'",
                    f"'{hit}' passes through '{other}', which isn't part of "
                    f"this joint — a wrench could never reach it.",
                    joint=jid, component=other,
                    fix=_fix(
                        f"Remove the colliding hardware at {a} ↔ {b}",
                        f"hardware intersects '{other}'",
                        f"joint {a} ↔ {b} declared 'none' — no hardware generated",
                        [{"op": "declare", "a": a, "b": b, "type": "none"}],
                    ) if inferred else None,
                ))

    findings.sort(key=lambda f: (0 if f["severity"] == "error" else 1))
    return {"findings": findings, "joints": len(joint_prims),
            "components": len(comps)}


def apply_audit_fixes(spec: dict, findings: List[dict]) -> dict:
    """Pure: return a new spec with the given findings' fix ops applied.
    Findings without a fix are ignored. Called only after user confirmation."""
    out = copy.deepcopy(spec)
    for f in findings:
        fix = f.get("fix")
        if not fix:
            continue
        for op in fix.get("ops") or []:
            if op["op"] == "nudge":
                offsets = out.setdefault("offsets", {})
                cur = list(offsets.get(op["key"]) or [0.0, 0.0, 0.0])
                offsets[op["key"]] = [cur[k] + op["delta"][k] for k in range(3)]
            elif op["op"] == "declare":
                conns = [c for c in out.get("connections") or []
                         if not (isinstance(c, dict)
                                 and {c.get("a"), c.get("b")} == {op["a"], op["b"]})]
                conns.append({"a": op["a"], "b": op["b"], "type": op["type"]})
                out["connections"] = conns
            elif op["op"] == "undeclare":
                out["connections"] = [
                    c for c in out.get("connections") or []
                    if not (isinstance(c, dict)
                            and {c.get("a"), c.get("b")} == {op["a"], op["b"]})
                ]
    return out
