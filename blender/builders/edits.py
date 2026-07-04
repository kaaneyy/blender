"""SketchUp-style direct-manipulation overlay (move / rotate / stretch /
delete / duplicate), baked into the primitive list so the Blender export
matches what the viewport shows. Mirror of frontend/src/builders/edits.ts —
keep in lockstep.

Two passes:
    apply_structure  — duplicate component groups, then drop deleted keys.
    apply_transforms — per-component rotate/scale about the group's center,
                       then position offsets (component + part).

Rotation follows Three.js' Euler-XYZ convention (the order the preview meshes
render with) so preview and export agree; the matrix below is Three's
``makeRotationFromEuler`` for order 'XYZ', and the recovery is its
``setFromRotationMatrix``.
"""
from __future__ import annotations

import copy
import math
from dataclasses import replace
from typing import Dict, List, Sequence, Tuple

from .base import Primitive
from .hardware import _aabb

Vec3 = Tuple[float, float, float]
_ZERO: Vec3 = (0.0, 0.0, 0.0)
_ONE: Vec3 = (1.0, 1.0, 1.0)


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


# ── rotation math (Three.js Euler-XYZ parity) ─────────────────────────────

def _euler_xyz_matrix(rot: Sequence[float]) -> List[List[float]]:
    x, y, z = rot
    c1, s1 = math.cos(x), math.sin(x)
    c2, s2 = math.cos(y), math.sin(y)
    c3, s3 = math.cos(z), math.sin(z)
    return [
        [c2 * c3, -c2 * s3, s2],
        [c1 * s3 + c3 * s1 * s2, c1 * c3 - s1 * s2 * s3, -c2 * s1],
        [s1 * s3 - c1 * c3 * s2, c3 * s1 + c1 * s2 * s3, c1 * c2],
    ]


def _mat_vec(m: List[List[float]], v: Sequence[float]) -> Vec3:
    return (
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    )


def _mat_mul(a: List[List[float]], b: List[List[float]]) -> List[List[float]]:
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]


def _euler_from_matrix(m: List[List[float]]) -> Vec3:
    m02 = max(-1.0, min(1.0, m[0][2]))
    y = math.asin(m02)
    if abs(m02) < 0.9999999:
        x = math.atan2(-m[1][2], m[2][2])
        z = math.atan2(-m[0][1], m[0][0])
    else:
        x = math.atan2(m[2][1], m[1][1])
        z = 0.0
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

def _scale_params(p: Primitive, s: Vec3) -> dict:
    params = dict(p.params)
    rxy = (s[0] + s[1]) / 2

    def scl(key: str, f: float) -> None:
        v = params.get(key)
        if isinstance(v, (int, float)):
            params[key] = v * f

    if "size" in params:
        sx, sy, sz = params["size"]
        params["size"] = (sx * s[0], sy * s[1], sz * s[2])
    for key in ("radius", "radius_bottom", "radius_top", "radius_end", "wall"):
        scl(key, rxy)
    scl("depth", s[2])
    if isinstance(params.get("path"), list):
        params["path"] = [(x * s[0], y * s[1], z * s[2]) for x, y, z in params["path"]]
    for key in ("profile_start", "profile_end"):
        prof = params.get(key)
        if isinstance(prof, dict):
            params[key] = {**prof, "w": prof["w"] * s[0], "h": prof["h"] * s[1]}
    prof = params.get("profile")
    if isinstance(prof, list):
        params["profile"] = [(r * rxy, z * s[2]) for r, z in prof]
    return params


def apply_transforms(prims: List[Primitive], spec: dict) -> List[Primitive]:
    edits = spec.get("edits") or {}
    rotations: Dict[str, Vec3] = {k: tuple(v) for k, v in (edits.get("rotations") or {}).items()}
    scales: Dict[str, Vec3] = {k: tuple(v) for k, v in (edits.get("scales") or {}).items()}
    offsets: Dict[str, Vec3] = {k: tuple(v) for k, v in (spec.get("offsets") or {}).items()}

    if not rotations and not scales and not offsets:
        return prims

    pivots: Dict[str, Vec3] = {}
    if rotations or scales:
        by_comp: Dict[str, List[Primitive]] = {}
        for p in prims:
            by_comp.setdefault(p.component, []).append(p)
        for comp, arr in by_comp.items():
            if comp in rotations or comp in scales:
                pivots[comp] = component_pivot(arr)

    out: List[Primitive] = []
    for p in prims:
        rot = rotations.get(p.component)
        scl = scales.get(p.component)
        oc = offsets.get(p.component, _ZERO)
        op = offsets.get(f"{p.component}/{p.name}", _ZERO)
        if rot is None and scl is None and oc == _ZERO and op == _ZERO:
            out.append(p)
            continue

        c = pivots.get(p.component, _ZERO)
        s = scl if scl is not None else _ONE
        rel = (
            (p.location[0] - c[0]) * s[0],
            (p.location[1] - c[1]) * s[1],
            (p.location[2] - c[2]) * s[2],
        )
        rotation = p.rotation
        if rot is not None:
            r_mat = _euler_xyz_matrix(rot)
            rel = _mat_vec(r_mat, rel)
            composed = _mat_mul(r_mat, _euler_xyz_matrix(p.rotation))
            rotation = _euler_from_matrix(composed)
        location = (
            c[0] + rel[0] + oc[0] + op[0],
            c[1] + rel[1] + oc[1] + op[1],
            c[2] + rel[2] + oc[2] + op[2],
        )
        params = _scale_params(p, s) if scl is not None else dict(p.params)
        out.append(replace(p, location=location, rotation=rotation, params=params))
    return out
