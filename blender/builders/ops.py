"""Blender-side geometry operations and the universal finishing pass.

Everything here requires bpy and is only imported from base.build(). The
pure-primitive layer stays bpy-free; the Three.js preview mirrors SHAPE only
(finishing — bevels, smoothing, solidify — is Blender-only by design).

Part A (finishing):
    finish(obj, prim, quality)  — shade-smooth-by-angle 30°, size-scaled
    bevel (segments 2, clamped, hardened normals), solidify for `shell`
    parts, weld for constructed meshes.

Part B (fabrication vocabulary):
    realize_lathe / realize_sweep / realize_loft / realize_tube, plus
    apply_cuts() for boolean-difference negative space.
"""
from __future__ import annotations

import math

from .shapes import profile_bounds, resolve_profile, ring_points


def _link_new_object(name: str, mesh, location, rotation):
    import bpy

    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    obj.rotation_euler = rotation
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    return obj


# ---------------------------------------------------------------------------
# Part B realizers
# ---------------------------------------------------------------------------

def realize_lathe(prim, segments: int):
    """B1: revolve a 2D (r, z) profile around local Z. z=0 sits at the
    primitive's location and the profile rises to its full depth."""
    import bmesh
    import bpy

    pts = resolve_profile(
        prim.params["profile"],
        radius=prim.params.get("radius"),
        depth=prim.params.get("depth"),
    )
    bm = bmesh.new()
    verts = [bm.verts.new((max(r, 1e-5), 0.0, z)) for r, z in pts]
    edges = [bm.edges.new((verts[i], verts[i + 1])) for i in range(len(verts) - 1)]
    bmesh.ops.spin(
        bm,
        geom=verts + edges,
        cent=(0.0, 0.0, 0.0),
        axis=(0.0, 0.0, 1.0),
        angle=2.0 * math.pi,
        steps=segments,
        use_merge=True,
    )
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)  # A5
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    mesh = bpy.data.meshes.new("lathe")
    bm.to_mesh(mesh)
    bm.free()
    return _link_new_object("lathe", mesh, prim.location, prim.rotation)


def realize_sweep(prim, segments: int):
    """B2: a round tube following a smooth 3D path with linear taper —
    a real tapered mast arm instead of overlapping cylinder segments."""
    import bpy

    path = prim.params["path"]
    radius = prim.params["radius"]
    radius_end = prim.params.get("radius_end", radius)

    curve = bpy.data.curves.new("sweep", type="CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = radius
    curve.bevel_resolution = max(2, segments // 8)
    curve.use_fill_caps = True
    curve.resolution_u = 12

    spline = curve.splines.new("BEZIER")
    spline.bezier_points.add(len(path) - 1)
    n = len(path)
    for i, (x, y, z) in enumerate(path):
        bp = spline.bezier_points[i]
        bp.co = (x, y, z)
        bp.handle_left_type = bp.handle_right_type = "AUTO"
        t = i / (n - 1) if n > 1 else 0.0
        # per-point radius factor drives the taper
        bp.radius = 1.0 + (radius_end / radius - 1.0) * t if radius else 1.0

    obj = _link_new_object("sweep", curve, prim.location, prim.rotation)
    bpy.ops.object.convert(target="MESH")
    return bpy.context.view_layer.objects.active


def realize_loft(prim, segments: int):
    """B4: bridge two cross-sections (rect/ellipse) along local Z —
    cobra-head housings, flared transitions, tapered caps."""
    import bmesh
    import bpy

    ps = prim.params["profile_start"]
    pe = prim.params["profile_end"]
    depth = prim.params["depth"]
    n = max(16, segments // 2)
    ring0 = ring_points(ps["shape"], ps["w"], ps["h"], n)
    ring1 = ring_points(pe["shape"], pe["w"], pe["h"], n)

    bm = bmesh.new()
    v0 = [bm.verts.new((x, y, -depth / 2.0)) for x, y in ring0]
    v1 = [bm.verts.new((x, y, depth / 2.0)) for x, y in ring1]
    for i in range(n):
        j = (i + 1) % n
        bm.faces.new((v0[i], v0[j], v1[j], v1[i]))
    bm.faces.new(tuple(reversed(v0)))  # caps
    bm.faces.new(tuple(v1))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    mesh = bpy.data.meshes.new("loft")
    bm.to_mesh(mesh)
    bm.free()
    return _link_new_object("loft", mesh, prim.location, prim.rotation)


def realize_tube(prim, segments: int):
    """B5: hollow tube — poles, bollards, and arms are never solid.

    ``section="square"`` builds hollow square stock (HSS) instead of pipe:
    bike racks, sign posts and railings are commonly square tube. ``radius``
    is the half-width across flats for both sections, so the wall solidify
    below is identical either way."""
    import bpy

    if prim.params.get("section") == "square":
        r = prim.params["radius"]
        bpy.ops.mesh.primitive_cube_add(
            size=2.0, location=prim.location, rotation=prim.rotation,
        )
        obj = bpy.context.active_object
        obj.scale = (r, r, prim.params["depth"] / 2.0)
        # bake the scale in before solidify, or the wall comes out uneven
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    else:
        bpy.ops.mesh.primitive_cylinder_add(
            radius=prim.params["radius"],
            depth=prim.params["depth"],
            location=prim.location,
            rotation=prim.rotation,
            vertices=segments,
        )
        obj = bpy.context.active_object
    sol = obj.modifiers.new("AF_Tube", "SOLIDIFY")
    sol.thickness = prim.params["wall"]
    sol.offset = -1.0
    sol.use_even_offset = True
    return obj


# ---------------------------------------------------------------------------
# B3: boolean-difference negative space
# ---------------------------------------------------------------------------

def apply_cuts(component_objects: list, cutters: list) -> None:
    """Subtract every cutter from every solid object in its component, then
    delete the cutter. Union is deliberately NOT offered: it would collapse
    the named part hierarchy that SketchUp/selection editing rely on."""
    import bpy

    for cutter in cutters:
        for obj in component_objects:
            mod = obj.modifiers.new("AF_Cut", "BOOLEAN")
            mod.operation = "DIFFERENCE"
            mod.object = cutter
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.modifier_apply(modifier=mod.name)
            weld = obj.modifiers.new("AF_CutWeld", "WELD")  # A5
            weld.merge_threshold = 1e-4
            bpy.ops.object.modifier_apply(modifier=weld.name)
        bpy.data.objects.remove(cutter, do_unlink=True)


# ---------------------------------------------------------------------------
# Part A: universal finishing pass
# ---------------------------------------------------------------------------

#: kinds that get welded after construction (bmesh/curve-built meshes)
_CONSTRUCTED = {"lathe", "sweep", "loft"}


def _min_feature(prim) -> float:
    """Smallest characteristic dimension, used to scale the bevel width."""
    p = prim.params
    if prim.kind == "box":
        return min(p["size"])
    if prim.kind in ("cylinder", "tube"):
        return min(p["radius"] * 2.0, p["depth"])
    if prim.kind == "cone":
        return min(max(p["radius_bottom"], p["radius_top"]) * 2.0, p["depth"])
    if prim.kind == "sphere":
        return p["radius"] * 2.0
    if prim.kind == "lathe":
        pts = resolve_profile(p["profile"], radius=p.get("radius"), depth=p.get("depth"))
        max_r, z0, z1 = profile_bounds(pts)
        return min(max_r * 2.0, z1 - z0)
    if prim.kind == "sweep":
        return p["radius"] * 2.0
    if prim.kind == "loft":
        ps, pe = p["profile_start"], p["profile_end"]
        return min(ps["w"], ps["h"], pe["w"], pe["h"], p["depth"])
    return 0.05


def finish(obj, prim, quality: str) -> None:
    """A1 smooth + A2 bevel + A4 solidify(shell) + A5 weld, per object."""
    import bpy

    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)

    # A1 — smooth shading with a 30° angle split: tubes read smooth, box
    # edges stay crisp. API differs across Blender versions.
    bpy.ops.object.shade_smooth()
    try:
        bpy.ops.object.shade_auto_smooth(angle=math.radians(30))  # Blender 4.1+
    except AttributeError:
        try:  # Blender <= 4.0
            obj.data.use_auto_smooth = True
            obj.data.auto_smooth_angle = math.radians(30)
        except AttributeError:
            pass

    # A5 — fuse coincident verts on constructed meshes before beveling
    if prim.kind in _CONSTRUCTED:
        weld = obj.modifiers.new("AF_Weld", "WELD")
        weld.merge_threshold = 1e-4

    # A4 — real wall thickness for sheet/cast parts
    shell = prim.params.get("shell")
    if isinstance(shell, (int, float)) and shell > 0 and prim.kind != "tube":
        sol = obj.modifiers.new("AF_Shell", "SOLIDIFY")
        sol.thickness = float(shell)
        sol.offset = -1.0
        sol.use_even_offset = True

    # A2 — the machined/cast edge: size-scaled bevel, clamped, hardened
    width = max(min(0.004, 0.15 * _min_feature(prim)), 0.0004)
    bev = obj.modifiers.new("AF_Bevel", "BEVEL")
    bev.width = width
    bev.segments = 2
    bev.limit_method = "ANGLE"
    bev.angle_limit = math.radians(30)
    bev.use_clamp_overlap = True
    try:
        bev.harden_normals = True
    except AttributeError:
        pass


def apply_all_modifiers(objects: list) -> None:
    """A6: bake modifiers into the meshes (DAE/OBJ/GLB/FBX exports). Skipped
    for .blend output so the file stays non-destructively editable."""
    import bpy

    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    if objects:
        bpy.context.view_layer.objects.active = objects[0]
        bpy.ops.object.convert(target="MESH")
