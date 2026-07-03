"""Builder registry and Blender realization layer (T3.1).

Every asset builder is split in two:

1.  A **pure primitive layer** — ``compute_primitives(spec) -> list[Primitive]``
    registered here with :func:`register`. It maps parameters/toggles to plain
    parametric primitives (cylinders, cones, boxes) in **meters**, with no bpy
    dependency, so it is unit-testable anywhere and easy to mirror 1:1 in the
    Three.js preview builders (Phase 4 parity requirement).

2.  The **realization layer** — :func:`build` — which turns those primitives
    into bpy objects. Naming convention (matters for SketchUp): every object
    is named ``AssetName/ComponentName/PartName`` and grouped into one
    collection per component, so the DAE export imports into SketchUp as
    nested, individually editable components.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Tuple

from standards.validator import convert

Vec3 = Tuple[float, float, float]

#: kind -> required keys in Primitive.params
PRIMITIVE_KINDS = {
    "cylinder": ("radius", "depth"),
    "cone": ("radius_bottom", "radius_top", "depth"),
    "box": ("size",),
    "sphere": ("radius",),
}

#: PBR presets assigned per component slot (T3.3). Baked to simple diffuse
#: for DAE later — SketchUp ignores full PBR, so base_color is what survives.
MATERIAL_PRESETS = {
    "galvanized_steel": {"base_color": (0.55, 0.57, 0.58, 1.0), "metallic": 1.0, "roughness": 0.45},
    "powder_coat_black": {"base_color": (0.02, 0.02, 0.02, 1.0), "metallic": 0.2, "roughness": 0.50},
    "powder_coat_green": {"base_color": (0.03, 0.12, 0.06, 1.0), "metallic": 0.2, "roughness": 0.50},
    "cast_iron": {"base_color": (0.08, 0.08, 0.09, 1.0), "metallic": 0.9, "roughness": 0.75},
    "concrete": {"base_color": (0.55, 0.53, 0.50, 1.0), "metallic": 0.0, "roughness": 0.90},
    "brushed_aluminum": {"base_color": (0.72, 0.73, 0.75, 1.0), "metallic": 1.0, "roughness": 0.35},
    "wood_slat": {"base_color": (0.38, 0.23, 0.10, 1.0), "metallic": 0.0, "roughness": 0.65},
    "lamp_lens": {"base_color": (0.92, 0.88, 0.72, 1.0), "metallic": 0.0, "roughness": 0.15},
}


@dataclass(frozen=True)
class Primitive:
    """One parametric primitive, dimensioned in meters, +Z up, asset at origin."""

    kind: str
    name: str
    component: str
    location: Vec3 = (0.0, 0.0, 0.0)
    rotation: Vec3 = (0.0, 0.0, 0.0)  # Euler XYZ, radians
    material_slot: str = "default"
    params: dict = field(default_factory=dict)

    def __post_init__(self):
        required = PRIMITIVE_KINDS.get(self.kind)
        if required is None:
            raise ValueError(f"Unknown primitive kind {self.kind!r}")
        missing = [k for k in required if k not in self.params]
        if missing:
            raise ValueError(f"{self.kind} {self.name!r} missing params: {missing}")


BuilderFn = Callable[[dict], List[Primitive]]
BUILDERS: Dict[str, BuilderFn] = {}


def register(asset_type: str) -> Callable[[BuilderFn], BuilderFn]:
    """Decorator: register ``compute_primitives`` for an asset_type."""

    def deco(fn: BuilderFn) -> BuilderFn:
        BUILDERS[asset_type] = fn
        return fn

    return deco


def spec_params(spec: dict) -> Dict[str, float]:
    """Numeric parameter values keyed by id, converted to meters."""
    default_unit = "ft" if spec.get("units", "imperial") == "imperial" else "m"
    out: Dict[str, float] = {}
    for p in spec.get("parameters", []):
        if isinstance(p.get("value"), (int, float)):
            out[p["id"]] = convert(p["value"], p.get("unit", default_unit), "m")
    return out


def spec_toggles(spec: dict) -> Dict[str, bool]:
    return {t["id"]: bool(t["value"]) for t in spec.get("toggles", [])}


def spec_selects(spec: dict) -> Dict[str, str]:
    """String-valued (type=select) parameters keyed by id."""
    return {
        p["id"]: p["value"]
        for p in spec.get("parameters", [])
        if isinstance(p.get("value"), str)
    }


def compute_primitives(spec: dict) -> List[Primitive]:
    """Dispatch to the registered builder for spec['asset_type']."""
    asset_type = spec.get("asset_type", "")
    try:
        builder = BUILDERS[asset_type]
    except KeyError:
        known = ", ".join(sorted(BUILDERS)) or "<none registered>"
        raise ValueError(f"No builder for asset_type {asset_type!r}; known: {known}")
    return builder(spec)


def mirror_x(primitives: List[Primitive], suffix: str = "_mirrored") -> List[Primitive]:
    """Mirror primitives across the YZ plane (used for e.g. double arms)."""
    out = []
    for p in primitives:
        x, y, z = p.location
        rx, ry, rz = p.rotation
        out.append(
            Primitive(
                kind=p.kind,
                name=p.name + suffix,
                component=p.component,
                location=(-x, y, z),
                rotation=(rx, -ry, rz if rz == 0.0 else math.pi - rz),
                material_slot=p.material_slot,
                params=dict(p.params),
            )
        )
    return out


# --------------------------------------------------------------------------
# Blender realization layer — everything below requires bpy.
# --------------------------------------------------------------------------

def _material_map(spec: dict) -> Dict[str, str]:
    return {m["slot"]: m["preset"] for m in spec.get("materials", [])}


def _get_or_create_material(preset_name: str):
    import bpy

    preset = MATERIAL_PRESETS.get(preset_name, MATERIAL_PRESETS["galvanized_steel"])
    mat = bpy.data.materials.get(f"AF_{preset_name}")
    if mat is not None:
        return mat
    mat = bpy.data.materials.new(f"AF_{preset_name}")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = preset["base_color"]
        bsdf.inputs["Metallic"].default_value = preset["metallic"]
        bsdf.inputs["Roughness"].default_value = preset["roughness"]
    # diffuse fallback that survives DAE/OBJ export (SketchUp path, T3.3)
    mat.diffuse_color = preset["base_color"]
    return mat


def clear_scene() -> None:
    """Remove every object/collection from the current scene (headless reset)."""
    import bpy

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)


def _realize(prim: Primitive):
    import bpy

    if prim.kind == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(
            radius=prim.params["radius"], depth=prim.params["depth"],
            location=prim.location, rotation=prim.rotation, vertices=24,
        )
    elif prim.kind == "cone":
        bpy.ops.mesh.primitive_cone_add(
            radius1=prim.params["radius_bottom"], radius2=prim.params["radius_top"],
            depth=prim.params["depth"],
            location=prim.location, rotation=prim.rotation, vertices=24,
        )
    elif prim.kind == "box":
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=prim.location, rotation=prim.rotation)
        obj = bpy.context.active_object
        obj.scale = prim.params["size"]
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    elif prim.kind == "sphere":
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=prim.params["radius"], location=prim.location,
            rotation=prim.rotation, segments=24, ring_count=12,
        )
    else:  # pragma: no cover - Primitive.__post_init__ already guards this
        raise ValueError(f"Unknown primitive kind {prim.kind!r}")
    return bpy.context.active_object


def build(spec: dict):
    """Realize a spec into bpy objects; returns the root collection.

    Assumes the spec has already been validated/clamped (build_cli and the
    export worker both run the validator first).
    """
    import bpy

    primitives = compute_primitives(spec)
    asset_name = spec.get("name") or spec["asset_type"]
    materials = _material_map(spec)

    root = bpy.data.collections.new(asset_name)
    bpy.context.scene.collection.children.link(root)

    component_colls: Dict[str, object] = {}
    for prim in primitives:
        coll = component_colls.get(prim.component)
        if coll is None:
            coll = bpy.data.collections.new(f"{asset_name}/{prim.component}")
            root.children.link(coll)
            component_colls[prim.component] = coll

        obj = _realize(prim)
        obj.name = f"{asset_name}/{prim.component}/{prim.name}"
        for existing in list(obj.users_collection):
            existing.objects.unlink(obj)
        coll.objects.link(obj)

        preset = materials.get(prim.material_slot)
        if preset is None and prim.material_slot == "lens":
            preset = "lamp_lens"
        obj.data.materials.append(_get_or_create_material(preset or "galvanized_steel"))

    return root
