"""Generic builder: realizes the spec's own `primitives` array.

This is the "generate anything" path — asset types without a curated builder
(bench, planter, sci-fi prop, ...) carry their parametric geometry inside the
spec, with dimensions written as expressions over parameter/toggle ids. The
same spec drives the Three.js preview through the mirrored TS implementation
(frontend/src/builders/generic.ts).
"""
from __future__ import annotations

from typing import List

from .base import Primitive, spec_params, spec_toggles
from .expr import safe_eval


def expression_env(spec: dict) -> dict:
    """Names visible to primitive expressions: parameters (in meters) and
    toggles (0/1). Toggles first so a parameter with the same id wins."""
    env = {tid: 1.0 if v else 0.0 for tid, v in spec_toggles(spec).items()}
    env.update(spec_params(spec))
    return env


def build_custom(spec: dict) -> List[Primitive]:
    env = expression_env(spec)
    prims: List[Primitive] = []
    used_names: set = set()

    for i, raw in enumerate(spec.get("primitives", [])):
        visible_if = raw.get("visible_if")
        if visible_if is not None and safe_eval(visible_if, env) == 0:
            continue

        params = {}
        for key, value in raw.get("params", {}).items():
            if isinstance(value, (list, tuple)):
                params[key] = tuple(safe_eval(v, env) for v in value)
            else:
                params[key] = safe_eval(value, env)

        name = raw.get("name") or f"part_{i + 1}"
        while name in used_names:  # LLMs occasionally repeat names
            name += "_"
        used_names.add(name)

        prims.append(
            Primitive(
                kind=raw["kind"],
                name=name,
                component=raw.get("component", "body"),
                location=tuple(safe_eval(v, env) for v in raw.get("location", (0, 0, 0))),
                rotation=tuple(safe_eval(v, env) for v in raw.get("rotation", (0, 0, 0))),
                material_slot=raw.get("material_slot", "default"),
                params=params,
            )
        )
    if not prims:
        raise ValueError("Custom spec produced no visible primitives")
    return prims
