"""SketchUp-style direct-manipulation overlay (move / rotate / stretch /
delete / duplicate), baked into the primitive list so the Blender export
matches what the viewport shows. Mirror of frontend/src/builders/edits.ts —
keep in lockstep.

Two passes:
    apply_structure  — duplicate component groups, then drop deleted keys.
    apply_transforms — rotate/scale about the target's center, then position
                       offsets. Every overlay is keyed by a whole component
                       ('pole') or a single part ('pole/shaft'); a part edit
                       turns/stretches that part about its OWN center and
                       composes with any group edit (part first, then group,
                       then re-grounding, then offsets). A scale target whose
                       bottom sat at grade (z=0) before the scale is shifted
                       back to grade afterward (GROUND_EPS) — offsets can
                       still deliberately lift or sink it from there.

Rotation follows the Blender Euler-XYZ convention (R = Rz·Ry·Rx, X applied
first about fixed axes) — the one the Blender realization layer uses for
``rotation_euler`` and the preview renders with (Three.js Euler order 'ZYX'),
so preview and export agree. The shared matrix lives in hardware.py; the
recovery below is Three's ``setFromRotationMatrix`` for order 'ZYX'.
"""
from __future__ import annotations

import copy
import math
from dataclasses import replace
from typing import Dict, List, Optional, Sequence, Tuple

from .base import Primitive
from .hardware import _aabb, _euler_xyz_matrix  # noqa: F401  (re-exported for tests)

Vec3 = Tuple[float, float, float]
_ZERO: Vec3 = (0.0, 0.0, 0.0)
_ONE: Vec3 = (1.0, 1.0, 1.0)

#: a target whose lowest point sits within this of z=0 before a scale edit is
#: "grounded" and gets its bottom restored to grade after the scale — matches
#: audit.py's GROUND_TOL (its below-grade/grounded tolerance) so the two
#: subsystems agree on what "at grade" means.
GROUND_EPS = 0.005


def component_pivot(prims: Sequence[Primitive]) -> Vec3:
    """Center of the combined AABB of a component's non-cut parts."""
    lo = [math.inf, math.inf, math.inf]
    hi = [-math.inf, -math.inf, -math.inf]
    n = 0
    for p in prims:
        if p.cut:
            continue
        center, half = _aabb(p)
        for k in range(3):
            lo[k] = min(lo[k], center[k] - half[k])
            hi[k] = max(hi[k], center[k] + half[k])
        n += 1
    if not n:
        return (0.0, 0.0, 0.0)
    return ((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2)


def _group_z_min(members: Sequence[Primitive]) -> Optional[float]:
    """Lowest world-z point among a group's non-cut members (``None`` if it
    has none), via the shared AABB helper — used to detect and restore
    grounding across a scale edit."""
    lo: Optional[float] = None
    for p in members:
        if p.cut:
            continue
        center, half = _aabb(p)
        z = center[2] - half[2]
        lo = z if lo is None else min(lo, z)
    return lo


# ── rotation math (Blender Euler-XYZ parity; matrix lives in hardware.py) ──

def _mat_vec(m: List[List[float]], v: Sequence[float]) -> Vec3:
    return (
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    )


def _mat_mul(a: List[List[float]], b: List[List[float]]) -> List[List[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _euler_from_matrix(m: List[List[float]]) -> Vec3:
    """Recover Blender-XYZ Euler angles from R = Rz·Ry·Rx (Three.js
    ``setFromRotationMatrix`` for order 'ZYX')."""
    m20 = max(-1.0, min(1.0, m[2][0]))
    y = math.asin(-m20)
    if abs(m20) < 0.9999999:
        x = math.atan2(m[2][1], m[2][2])
        z = math.atan2(m[1][0], m[0][0])
    else:
        x = 0.0
        z = math.atan2(-m[0][1], m[1][1])
    return (x, y, z)


# ── structural pass (duplicate / delete) ──────────────────────────────────

def _clone_component(prims: Sequence[Primitive], source: str, name: str) -> List[Primitive]:
    return [
        replace(p, component=name, params=copy.deepcopy(p.params))
        for p in prims
        if p.component == source
    ]


def apply_structure(prims: List[Primitive], spec: dict) -> List[Primitive]:
    edits = spec.get("edits") or {}
    out = list(prims)

    for dup in edits.get("duplicates") or []:
        out = out + _clone_component(out, dup["source"], dup["name"])

    hidden = set(edits.get("hidden") or [])
    if hidden:
        out = [
            p for p in out
            if p.component not in hidden and f"{p.component}/{p.name}" not in hidden
        ]
    return out


# ── transform pass (move / rotate / scale) ────────────────────────────────

def _scale_params(source: dict, f: Vec3) -> dict:
    """Scale a primitive's LOCAL dimensions by per-local-axis factors ``f``
    (see ``_scale_factors`` — for identity rotation ``f`` is exactly the
    caller's world-axis scale ``s``, so unrotated behavior is unchanged)."""
    params = dict(source)
    rxy = (f[0] + f[1]) / 2

    def scl(key: str, factor: float) -> None:
        v = params.get(key)
        if isinstance(v, (int, float)):
            params[key] = v * factor

    if "size" in params:
        sx, sy, sz = params["size"]
        params["size"] = (sx * f[0], sy * f[1], sz * f[2])
    for key in ("radius", "radius_bottom", "radius_top", "radius_end", "wall"):
        scl(key, rxy)
    scl("depth", f[2])
    if isinstance(params.get("path"), list):
        params["path"] = [(x * f[0], y * f[1], z * f[2]) for x, y, z in params["path"]]
    for key in ("profile_start", "profile_end"):
        prof = params.get(key)
        if isinstance(prof, dict):
            params[key] = {**prof, "w": prof["w"] * f[0], "h": prof["h"] * f[1]}
    prof = params.get("profile")
    if isinstance(prof, list):
        params["profile"] = [(r * rxy, z * f[2]) for r, z in prof]
    return params


def _scale_factors(s: Vec3, rotation: Vec3) -> Vec3:
    """Per-LOCAL-axis stretch equivalent to a world-axis scale ``s`` on a
    primitive currently rotated by ``rotation`` (Blender XYZ Euler, entering
    the stage before the stage's own rotation is composed). For R = the
    Blender-parity rotation matrix and S = diag(s), the local basis vector
    e_i lands at S·R·e_i in world space, so its length is the equivalent
    local stretch: f_i = ||S·R·e_i|| = sqrt(sum_j (s_j * R[j][i])^2).
    Exact (f == s) for identity rotation. For rotations that aren't
    axis-aligned this is the best diagonal approximation — a true
    non-uniform scale of a rotated solid is a shear, which primitives
    (box/cylinder/etc, defined by size/radius/depth) can't represent."""
    m = _euler_xyz_matrix(rotation)
    return tuple(
        math.sqrt(sum((s[j] * m[j][i]) ** 2 for j in range(3)))
        for i in range(3)
    )


def _apply_stage(location: Vec3, rotation: Vec3, params: dict, pivot: Vec3,
                 rot, scl) -> Tuple[Vec3, Vec3, dict]:
    """One rotate/scale stage about a pivot (used for the part-level edit,
    then again for the component-level edit)."""
    s = scl if scl is not None else _ONE
    rel = (
        (location[0] - pivot[0]) * s[0],
        (location[1] - pivot[1]) * s[1],
        (location[2] - pivot[2]) * s[2],
    )
    rotation_in = rotation
    if rot is not None:
        r_mat = _euler_xyz_matrix(rot)
        rel = _mat_vec(r_mat, rel)
        composed = _mat_mul(r_mat, _euler_xyz_matrix(rotation))
        rotation = _euler_from_matrix(composed)
    if scl is not None:
        f = _scale_factors(s, rotation_in)
        params = _scale_params(params, f)
    return (
        (pivot[0] + rel[0], pivot[1] + rel[1], pivot[2] + rel[2]),
        rotation,
        params,
    )


def apply_transforms(prims: List[Primitive], spec: dict) -> List[Primitive]:
    edits = spec.get("edits") or {}
    rotations: Dict[str, Vec3] = {k: tuple(v) for k, v in (edits.get("rotations") or {}).items()}
    scales: Dict[str, Vec3] = {k: tuple(v) for k, v in (edits.get("scales") or {}).items()}
    offsets: Dict[str, Vec3] = {k: tuple(v) for k, v in (spec.get("offsets") or {}).items()}

    if not rotations and not scales and not offsets:
        return prims

    # pivots per edit key, from the pre-transform prims: a component key
    # turns about the group's center, a 'component/part' key about that
    # part's own center. Scale keys also record whether the group was
    # sitting at grade before the scale, so it can be re-grounded after.
    pivots: Dict[str, Vec3] = {}
    grounded: Dict[str, bool] = {}
    for key in set(rotations) | set(scales):
        if "/" in key:
            match = [p for p in prims if f"{p.component}/{p.name}" == key]
        else:
            match = [p for p in prims if p.component == key]
        if match:
            pivots[key] = component_pivot(match)
        if key in scales:
            z_min = _group_z_min(match)
            grounded[key] = z_min is not None and abs(z_min) <= GROUND_EPS

    # stage pass: rotate/scale about each key's pivot (part stage, then
    # component stage). Offsets are applied afterward, once re-grounding
    # (below) has had a chance to restore any scaled group to grade.
    out: List[Primitive] = []
    for p in prims:
        part_key = f"{p.component}/{p.name}"
        rot_p, scl_p = rotations.get(part_key), scales.get(part_key)
        rot_c, scl_c = rotations.get(p.component), scales.get(p.component)
        if rot_p is None and scl_p is None and rot_c is None and scl_c is None:
            out.append(p)
            continue

        location, rotation, params = p.location, p.rotation, dict(p.params)
        if rot_p is not None or scl_p is not None:
            location, rotation, params = _apply_stage(
                location, rotation, params, pivots.get(part_key, _ZERO),
                rot_p, scl_p)
        if rot_c is not None or scl_c is not None:
            location, rotation, params = _apply_stage(
                location, rotation, params, pivots.get(p.component, _ZERO),
                rot_c, scl_c)
        out.append(replace(p, location=location, rotation=rotation, params=params))

    # re-ground: a scaled group whose bottom sat at grade before the scale
    # gets shifted back to z-min == 0 afterward — part-level keys first
    # (matching the part-then-component stage order), so nested edits
    # compose. z-min is recomputed per key against the current `out`.
    part_keys = [k for k in scales if "/" in k]
    comp_keys = [k for k in scales if "/" not in k]
    for key in part_keys + comp_keys:
        if not grounded.get(key):
            continue
        if "/" in key:
            idxs = [i for i, p in enumerate(out) if f"{p.component}/{p.name}" == key]
        else:
            idxs = [i for i, p in enumerate(out) if p.component == key]
        if not idxs:
            continue
        z_min = _group_z_min([out[i] for i in idxs])
        if z_min is None:
            continue
        shift = -z_min
        if shift:
            for i in idxs:
                loc = out[i].location
                out[i] = replace(out[i], location=(loc[0], loc[1], loc[2] + shift))

    if not offsets:
        return out

    result: List[Primitive] = []
    for p in out:
        oc = offsets.get(p.component, _ZERO)
        op = offsets.get(f"{p.component}/{p.name}", _ZERO)
        if oc == _ZERO and op == _ZERO:
            result.append(p)
            continue
        loc = p.location
        result.append(replace(p, location=(
            loc[0] + oc[0] + op[0],
            loc[1] + oc[1] + op[1],
            loc[2] + oc[2] + op[2],
        )))
    return result
