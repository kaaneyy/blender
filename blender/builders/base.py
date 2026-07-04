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
    # Part B fabrication vocabulary:
    "lathe": ("profile",),          # revolve profile around Z (globes, finials)
    "sweep": ("path", "radius"),    # round tube along a 3D path, optional taper
    "loft": ("profile_start", "profile_end", "depth"),  # section-to-section taper
    "tube": ("radius", "wall", "depth"),  # hollow cylinder
}

#: A3 quality tiers -> radial segment counts for round geometry.
QUALITY_SEGMENTS = {"draft": 24, "preview": 32, "final": 64}

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
    #: negative space — boolean-subtracted from its component, never rendered
    cut: bool = False
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
    """Numeric parameter values keyed by id. Values with a length unit are
    converted to meters; unit-less parameters (angles in degrees, counts,
    ratios) pass through unchanged so expressions can use them directly
    (e.g. rotation "panel_tilt * 0.01745")."""
    out: Dict[str, float] = {}
    for p in spec.get("parameters", []):
        if isinstance(p.get("value"), (int, float)):
            unit = p.get("unit")
            out[p["id"]] = convert(p["value"], unit, "m") if unit else float(p["value"])
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


def apply_offsets(prims: List[Primitive], offsets: dict) -> List[Primitive]:
    """Apply user position nudges: 'Component' and 'Component/Part' keys
    stack, values are (dx, dy, dz) in meters."""
    out = []
    for p in prims:
        dc = offsets.get(p.component, (0.0, 0.0, 0.0))
        dp = offsets.get(f"{p.component}/{p.name}", (0.0, 0.0, 0.0))
        if dc == (0.0, 0.0, 0.0) and dp == (0.0, 0.0, 0.0):
            out.append(p)
            continue
        x, y, z = p.location
        out.append(
            Primitive(
                kind=p.kind, name=p.name, component=p.component,
                location=(x + dc[0] + dp[0], y + dc[1] + dp[1], z + dc[2] + dp[2]),
                rotation=p.rotation, material_slot=p.material_slot,
                params=dict(p.params),
            )
        )
    return out


def compute_primitives(spec: dict) -> List[Primitive]:
    """Dispatch to the registered builder for spec['asset_type'], falling
    back to the generic primitives-in-the-spec builder (the LLM's
    'generate anything' path) when no curated builder exists. Then apply the
    cross-cutting passes: connection hardware and user position offsets."""
    asset_type = spec.get("asset_type", "")
    builder = BUILDERS.get(asset_type)
    if builder is not None:
        prims = builder(spec)
    elif spec.get("primitives"):
        from .generic import build_custom

        prims = build_custom(spec)
    else:
        known = ", ".join(sorted(BUILDERS)) or "<none registered>"
        raise ValueError(
            f"No builder for asset_type {asset_type!r} and the spec has no "
            f"'primitives' array; curated builders: {known}"
        )

    if spec_toggles(spec).get("connection_hardware"):
        from .hardware import compute_hardware

        prims = prims + compute_hardware(prims, spec)

    # SketchUp-style edit overlay: structural (duplicate/delete) then the
    # move/rotate/scale transform, baked so the export matches the viewport.
    from .edits import apply_structure, apply_transforms

    prims = apply_structure(prims, spec)
    prims = apply_transforms(prims, spec)
    return prims


def mirror_x(primitives: List[Primitive], suffix: str = "_mirrored") -> List[Primitive]:
    """Mirror primitives across the YZ plane (used for e.g. double arms)."""
    out = []
    for p in primitives:
        x, y, z = p.location
        rx, ry, rz = p.rotation
        params = dict(p.params)
        if "path" in params:  # sweep paths carry their own coordinates
            params["path"] = tuple((-px, py, pz) for px, py, pz in params["path"])
        out.append(
            Primitive(
                kind=p.kind,
                name=p.name + suffix,
                component=p.component,
                location=(-x, y, z),
                rotation=(rx, -ry, rz if rz == 0.0 else math.pi - rz),
                material_slot=p.material_slot,
                cut=p.cut,
                params=params,
            )
        )
    return out


def hex_to_rgba(color: str) -> tuple:
    c = color.lstrip("#")
    return tuple(int(c[i : i + 2], 16) / 255 for i in (0, 2, 4)) + (1.0,)


#: Grime color weathering lerps toward (dark warm grey).
GRIME_COLOR = (0.16, 0.14, 0.12)

#: Finish presets nudge roughness toward a fabrication surface (D/E5).
FINISH_ROUGHNESS = {"cast": 0.72, "machined": 0.35, "sheet": 0.28, "rough": 0.85}


def material_preset_name(spec: dict, slot: str) -> str:
    """Resolved preset name for a slot, applying the same fallbacks
    resolve_material uses (lens→lamp_lens, hardware→brushed_aluminum, else
    galvanized_steel). Shared by the hardware material-appropriateness check."""
    entry = next((m for m in spec.get("materials", []) if m.get("slot") == slot), None)
    fallbacks = {"lens": "lamp_lens", "hardware": "brushed_aluminum"}
    return (entry or {}).get("preset") or fallbacks.get(slot, "galvanized_steel")


def resolve_material(spec: dict, slot: str) -> dict:
    """Preset values merged with the spec's per-slot overrides (color,
    metalness, roughness, uv_scale, emission, weathering, finish). Pure — the
    same logic is mirrored in the frontend so preview and export shade alike.
    The returned values are the AUTHORED appearance; call weathered() for the
    aged shading actually sent to the BSDF/preview material."""
    entry = next((m for m in spec.get("materials", []) if m.get("slot") == slot), None)
    preset_name = material_preset_name(spec, slot)
    preset = MATERIAL_PRESETS.get(preset_name, MATERIAL_PRESETS["galvanized_steel"])
    props = {
        "base_color": preset["base_color"],
        "metallic": preset["metallic"],
        "roughness": preset["roughness"],
        "uv_scale": 1.0,
        "emission": 0.0,
        "weathering": 0.0,
    }
    if entry:
        if entry.get("color"):
            props["base_color"] = hex_to_rgba(entry["color"])
        # a named finish sets the roughness baseline before any explicit override
        if entry.get("finish") in FINISH_ROUGHNESS:
            props["roughness"] = FINISH_ROUGHNESS[entry["finish"]]
        for spec_key, prop_key in (
            ("metalness", "metallic"),
            ("roughness", "roughness"),
            ("uv_scale", "uv_scale"),
            ("emission", "emission"),
            ("weathering", "weathering"),
        ):
            if isinstance(entry.get(spec_key), (int, float)):
                props[prop_key] = float(entry[spec_key])
    return props


def weathered(props: dict) -> dict:
    """Apply the weathering aging model to authored props → shading values.
    w=0 is a no-op; higher w lerps color toward grime, raises roughness, and
    dulls metals. Identical formula in the frontend (D1/D2)."""
    w = max(0.0, min(1.0, props.get("weathering", 0.0)))
    if w <= 0.0:
        return {
            "base_color": props["base_color"],
            "metallic": props["metallic"],
            "roughness": props["roughness"],
            "emission": props["emission"],
        }
    base = props["base_color"]
    mix = 0.5 * w
    color = tuple(base[i] * (1.0 - mix) + GRIME_COLOR[i] * mix for i in range(3)) + (1.0,)
    return {
        "base_color": color,
        "metallic": props["metallic"] * (1.0 - 0.4 * w),
        "roughness": min(1.0, props["roughness"] + 0.45 * w),
        "emission": props["emission"],
    }


# --------------------------------------------------------------------------
# Blender realization layer — everything below requires bpy.
# --------------------------------------------------------------------------

def _get_or_create_material(name: str, props: dict):
    import bpy

    mat = bpy.data.materials.get(name)
    if mat is not None:
        return mat
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    shade = weathered(props)  # D2: aged base color / roughness / metalness
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = shade["base_color"]
        bsdf.inputs["Metallic"].default_value = shade["metallic"]
        bsdf.inputs["Roughness"].default_value = shade["roughness"]
        if shade["emission"] > 0:
            try:  # Blender 4.x names
                bsdf.inputs["Emission Color"].default_value = shade["base_color"]
                bsdf.inputs["Emission Strength"].default_value = shade["emission"]
            except KeyError:  # Blender 3.x fallback
                bsdf.inputs["Emission"].default_value = shade["base_color"]
    # diffuse fallback that survives DAE/OBJ export (SketchUp path, T3.3)
    mat.diffuse_color = shade["base_color"]
    # consumed by the texture-mapping pass when image textures land (T3.3)
    mat["af_uv_scale"] = props["uv_scale"]
    mat["af_weathering"] = props.get("weathering", 0.0)
    return mat


def clear_scene() -> None:
    """Remove every object/collection from the current scene (headless reset)."""
    import bpy

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for coll in list(bpy.data.collections):
        bpy.data.collections.remove(coll)


def _realize(prim: Primitive, quality: str = "final"):
    import bpy

    from . import ops

    default_segments = QUALITY_SEGMENTS.get(quality, QUALITY_SEGMENTS["final"])
    segments = int(prim.params.get("segments", default_segments))

    if prim.kind == "cylinder":
        bpy.ops.mesh.primitive_cylinder_add(
            radius=prim.params["radius"], depth=prim.params["depth"],
            location=prim.location, rotation=prim.rotation,
            vertices=segments,
        )
    elif prim.kind == "cone":
        bpy.ops.mesh.primitive_cone_add(
            radius1=prim.params["radius_bottom"], radius2=prim.params["radius_top"],
            depth=prim.params["depth"],
            location=prim.location, rotation=prim.rotation,
            vertices=segments,
        )
    elif prim.kind == "box":
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=prim.location, rotation=prim.rotation)
        obj = bpy.context.active_object
        obj.scale = prim.params["size"]
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    elif prim.kind == "sphere":
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=prim.params["radius"], location=prim.location,
            rotation=prim.rotation,
            segments=max(segments // 2, 16), ring_count=max(segments // 4, 8),
        )
    elif prim.kind == "lathe":
        return ops.realize_lathe(prim, segments)
    elif prim.kind == "sweep":
        return ops.realize_sweep(prim, segments)
    elif prim.kind == "loft":
        return ops.realize_loft(prim, segments)
    elif prim.kind == "tube":
        return ops.realize_tube(prim, segments)
    else:  # pragma: no cover - Primitive.__post_init__ already guards this
        raise ValueError(f"Unknown primitive kind {prim.kind!r}")
    return bpy.context.active_object


def build(spec: dict, quality: str = "final", apply_modifiers: bool = True):
    """Realize a spec into bpy objects; returns the root collection.

    quality: draft|preview|final segment tiers (A3). apply_modifiers bakes
    the finishing pass into the meshes (A6) — pass False for .blend output
    so the file stays non-destructively editable.

    Assumes the spec has already been validated/clamped (build_cli and the
    export worker both run the validator first).
    """
    import bpy

    from . import ops

    primitives = compute_primitives(spec)
    asset_name = spec.get("name") or spec["asset_type"]

    root = bpy.data.collections.new(asset_name)
    bpy.context.scene.collection.children.link(root)

    component_colls: Dict[str, object] = {}
    solids: Dict[str, list] = {}
    cutters: Dict[str, list] = {}
    realized: List[tuple] = []

    for prim in primitives:
        coll = component_colls.get(prim.component)
        if coll is None:
            coll = bpy.data.collections.new(f"{asset_name}/{prim.component}")
            root.children.link(coll)
            component_colls[prim.component] = coll

        obj = _realize(prim, quality)
        obj.name = f"{asset_name}/{prim.component}/{prim.name}"
        for existing in list(obj.users_collection):
            existing.objects.unlink(obj)
        coll.objects.link(obj)

        if prim.cut:
            cutters.setdefault(prim.component, []).append(obj)
            continue

        solids.setdefault(prim.component, []).append(obj)
        realized.append((obj, prim))

        props = resolve_material(spec, prim.material_slot)
        obj.data.materials.append(
            _get_or_create_material(f"AF_{asset_name}_{prim.material_slot}", props)
        )

    # B3: boolean-difference negative space (bolt holes, slots, cutouts)
    for component, cut_objs in cutters.items():
        ops.apply_cuts(solids.get(component, []), cut_objs)

    # Part A: universal finishing pass
    for obj, prim in realized:
        ops.finish(obj, prim, quality)

    if apply_modifiers:
        ops.apply_all_modifiers([obj for obj, _ in realized])

    return root
