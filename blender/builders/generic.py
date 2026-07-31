"""Generic builder: realizes the spec's own `primitives` array.

This is the "generate anything" path — asset types without a curated builder
(bench, planter, sci-fi prop, ...) carry their parametric geometry inside the
spec, with dimensions written as expressions over parameter/toggle ids. The
same spec drives the Three.js preview through the mirrored TS implementation
(frontend/src/builders/generic.ts).
"""
from __future__ import annotations

import math
from typing import List

from .base import Primitive, spec_params, spec_toggles
from .expr import safe_eval
from .shapes import fillet_path


def expression_env(spec: dict) -> dict:
    """Names visible to primitive expressions: parameters (in meters) and
    toggles (0/1). Toggles first so a parameter with the same id wins."""
    env = {tid: 1.0 if v else 0.0 for tid, v in spec_toggles(spec).items()}
    env.update(spec_params(spec))
    return env


#: params whose value is a NAME, not a number — passed through untouched so
#: the expression evaluator never tries to resolve e.g. "square" as a variable
_ENUM_PARAMS = {"section"}


def _eval_params(raw_params: dict, env: dict) -> dict:
    """Evaluate primitive params: scalars, triples, lathe profiles, sweep
    paths, and loft cross-sections all accept expressions."""
    params = {}
    for key, value in raw_params.items():
        if key in _ENUM_PARAMS:
            params[key] = value  # a named choice, never arithmetic
        elif key == "profile":
            if isinstance(value, str):
                params[key] = value  # named profile, resolved at realization
            else:
                params[key] = tuple(
                    (safe_eval(r, env), safe_eval(z, env)) for r, z in value
                )
        elif key == "path":
            params[key] = tuple(
                tuple(safe_eval(v, env) for v in point) for point in value
            )
        elif key in ("profile_start", "profile_end"):
            params[key] = {
                "shape": value["shape"],
                "w": safe_eval(value["w"], env),
                "h": safe_eval(value["h"], env),
            }
        elif isinstance(value, (list, tuple)):
            params[key] = tuple(safe_eval(v, env) for v in value)
        else:
            params[key] = safe_eval(value, env)
    # A called-out bend is baked into the path HERE, once, so everything
    # downstream — the AABB, the preview, the Blender curve, the takeoff —
    # reads the same filleted polyline and can never disagree about it.
    bend = params.get("bend_radius")
    if params.get("path") and isinstance(bend, (int, float)) and bend > 0:
        params["path"] = tuple(fillet_path(params["path"], float(bend)))
    return params


def build_custom(spec: dict) -> List[Primitive]:
    env = expression_env(spec)
    prims: List[Primitive] = []
    used_names: set = set()

    for i, raw in enumerate(spec.get("primitives", [])):
        visible_if = raw.get("visible_if")
        if visible_if is not None and safe_eval(visible_if, env) == 0:
            continue

        params = _eval_params(raw.get("params", {}), env)
        base_name = raw.get("name") or f"part_{i + 1}"
        location = tuple(safe_eval(v, env) for v in raw.get("location", (0, 0, 0)))
        rotation = tuple(safe_eval(v, env) for v in raw.get("rotation", (0, 0, 0)))

        # B7: linear array — expand into evenly stepped copies
        array = raw.get("array")
        if array:
            # half-up: floor(x+0.5), keep identical to the mirror (JS
            # Math.round is not configurable, so it sets the convention —
            # plain round() here would banker's-round 2.5 down to 2).
            count = max(1, math.floor(safe_eval(array["count"], env) + 0.5))
            step = tuple(safe_eval(v, env) for v in array["step"])
            placements = [
                (
                    f"{base_name}_{n + 1}" if count > 1 else base_name,
                    (
                        location[0] + step[0] * n,
                        location[1] + step[1] * n,
                        location[2] + step[2] * n,
                    ),
                )
                for n in range(count)
            ]
        else:
            placements = [(base_name, location)]

        for name, loc in placements:
            while name in used_names:  # LLMs occasionally repeat names
                name += "_"
            used_names.add(name)
            prims.append(
                Primitive(
                    kind=raw["kind"],
                    name=name,
                    component=raw.get("component", "body"),
                    location=loc,
                    rotation=rotation,
                    material_slot=raw.get("material_slot", "default"),
                    cut=bool(raw.get("cut", False)),
                    params=dict(params),
                )
            )
    if not any(not p.cut for p in prims):
        raise ValueError("Custom spec produced no visible primitives")
    return prims
