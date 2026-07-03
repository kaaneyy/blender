# AssetForge

AI parametric asset generator: prompt → parametric spec → live 3D preview →
US-code-validated dimensions → headless Blender build → `.dae`/`.obj`/`.glb`/`.fbx`
export for SketchUp and animation pipelines.

The full roadmap lives in [`docs/BUILD_PLAN.md`](docs/BUILD_PLAN.md). This repo
currently ships **milestone 1 — the headless proof**: shared schemas, the
US-code standards DB + validator, the builder registry, the first asset builder
(`street_light`), and a CLI harness that works with or without Blender.

## Layout

| Path | Purpose |
| --- | --- |
| `schemas/` | Shared JSON Schemas (`AssetSpec`, `ExportRequest`) that drive UI, preview, and Blender alike |
| `standards/` | `us_codes.json` (per-asset dimensional limits with citations) + pure `validator.py` |
| `blender/` | Builder registry (`builders/base.py`), asset builders, `build_cli.py` harness, worker Dockerfile |
| `backend/` | FastAPI service (health, standards, spec validation; LLM + export endpoints land in later milestones) |
| `frontend/` | Vite + React + react-three-fiber app shell (preview lands in milestone 2) |
| `examples/` | Ready-to-build example specs |

## Architecture rule

One shared JSON **AssetSpec** drives everything — the Three.js preview, the
Blender builder, and the UI controls are all generated from it. Builders are
split into a *pure primitive layer* (`compute_primitives(spec)`, plain
cylinders/cones/boxes in meters, no `bpy`, unit-testable anywhere) and a thin
Blender *realization layer* (`builders/base.py:build`). The primitive layer is
what the Three.js preview mirrors 1:1 for dimensional parity.

## Quickstart

```bash
# 1. Run the test suite (validator + street_light builder; no Blender needed)
pip install pytest && pytest

# 2. Validate a spec and dump its primitive list (no Blender needed)
python3 blender/build_cli.py examples/street_light.json out/primitives.json

# 3. Full headless build + export (requires Blender)
blender -b -P blender/build_cli.py -- examples/street_light.json out/light.dae   # SketchUp
blender -b -P blender/build_cli.py -- examples/street_light.json out/light.glb   # web/animation

# 4. API + workers via Docker
docker compose up --build backend
curl -X POST localhost:8000/validate-spec -H 'content-type: application/json' \
     -d @examples/street_light.json
```

Out-of-code dimensions are clamped automatically (`code_mode: "strict"`) or
reported as warnings only (`"advisory"`, for props that don't need compliance),
always with the code citation attached.

## Why Blender as the engine (SketchUp as the destination)

- The SketchUp C SDK (writes `.skp`) is Windows/macOS only — no Linux headless generation.
- No SketchUp browser runtime exists for live preview; SketchUp Free web has no API.
- Blender headless is Linux-native, free, fully scriptable, and exports `.dae`,
  which SketchUp imports losslessly for this asset class — including the
  `AssetName/ComponentName` node hierarchy, so poles/arms/heads arrive as
  separately editable components.
