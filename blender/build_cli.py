"""Headless build harness (T3.5) — test the pipeline without the web app.

Inside Blender (full build + export; format chosen by output extension):

    blender -b -P blender/build_cli.py -- examples/street_light.json out/light.glb
    blender -b -P blender/build_cli.py -- examples/street_light.json out/light.dae

Without Blender (validate the spec and dump the primitive list as JSON —
useful for CI and for eyeballing preview parity):

    python3 blender/build_cli.py examples/street_light.json out/primitives.json

Flags:
    --no-validate    skip the standards validator (debugging only)
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from standards.validator import validate_spec  # noqa: E402

EXPORTERS = {".glb", ".gltf", ".dae", ".obj", ".fbx"}


def _cli_args() -> list[str]:
    """Args after '--' when run inside Blender, plain argv otherwise."""
    argv = sys.argv
    if "--" in argv:
        return argv[argv.index("--") + 1:]
    return argv[1:]


def _export(filepath: Path) -> None:
    import bpy

    suffix = filepath.suffix.lower()
    filepath.parent.mkdir(parents=True, exist_ok=True)
    if suffix in (".glb", ".gltf"):
        bpy.ops.export_scene.gltf(
            filepath=str(filepath),
            export_format="GLB" if suffix == ".glb" else "GLTF_SEPARATE",
        )
    elif suffix == ".dae":
        # SketchUp-native path (T5.2/T5.3): collections become nested DAE
        # nodes, which SketchUp imports as named, editable components.
        bpy.ops.wm.collada_export(filepath=str(filepath), use_object_instantiation=True)
    elif suffix == ".obj":
        try:
            bpy.ops.wm.obj_export(filepath=str(filepath))  # Blender >= 3.2
        except AttributeError:
            bpy.ops.export_scene.obj(filepath=str(filepath))
    elif suffix == ".fbx":
        bpy.ops.export_scene.fbx(filepath=str(filepath))
    else:
        raise SystemExit(f"Unsupported export format: {suffix}")


def main() -> None:
    args = _cli_args()
    flags = {a for a in args if a.startswith("--")}
    positional = [a for a in args if not a.startswith("--")]
    if len(positional) != 2:
        raise SystemExit(__doc__)

    spec_path, out_path = Path(positional[0]), Path(positional[1])
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    if "--no-validate" not in flags:
        result = validate_spec(spec)
        if not result.checked:
            print(f"[validator] no standards entry for {spec.get('asset_type')!r}; skipped")
        for v in result.violations:
            action = f"clamped to {v.corrected_value} {v.unit}" if v.corrected_value is not None else "advisory"
            print(f"[validator] {v.message} -> {action}")
        spec = result.spec

    try:
        import bpy  # noqa: F401
        have_bpy = True
    except ImportError:
        have_bpy = False

    if not have_bpy:
        if out_path.suffix.lower() in EXPORTERS:
            raise SystemExit(
                f"Exporting {out_path.suffix} requires Blender:\n"
                f"  blender -b -P blender/build_cli.py -- {spec_path} {out_path}"
            )
        from blender.builders import base  # noqa: F401  (registers builders)
        import blender.builders  # noqa: F401
        prims = base.compute_primitives(spec)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps([dataclasses.asdict(p) for p in prims], indent=2),
            encoding="utf-8",
        )
        print(f"[build_cli] wrote {len(prims)} primitives -> {out_path}")
        return

    from blender.builders import base
    import blender.builders  # noqa: F401  (registers builders)

    base.clear_scene()
    root = base.build(spec)
    print(f"[build_cli] built collection {root.name!r} "
          f"({sum(len(c.objects) for c in root.children_recursive)} objects)")
    _export(out_path)
    print(f"[build_cli] exported -> {out_path}")


if __name__ == "__main__":
    main()
