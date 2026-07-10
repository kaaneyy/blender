# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

AssetForge: AI parametric asset generator — prompt → parametric AssetSpec JSON → live Three.js preview → US-code-validated dimensions → headless Blender build → `.blend`/`.dae`/`.obj`/`.glb`/`.fbx` export. (The repo is named "blender" but is not the Blender source tree.)

## Commands

```bash
# Python tests (the only test suite — it guards BOTH language implementations)
pip install pytest jsonschema fastapi httpx          # one-time
python3 -m pytest                                    # from the repo root (pytest.ini sets testpaths)
python3 -m pytest blender/tests/test_audit.py        # one file
python3 -m pytest -k "gusset and flange"             # one test by keyword

# Frontend (Vite + React + react-three-fiber)
cd frontend && npm install
npm run dev                                          # http://localhost:5173
npm run build                                        # tsc --noEmit && vite build — this IS the TS type check; run it before committing TS changes

# Headless build without Blender (spec -> validated primitive JSON; good for eyeballing parity)
python3 blender/build_cli.py examples/street_light.json out/prims.json

# Full Blender export (format picked from the output extension)
blender -b -P blender/build_cli.py -- examples/street_light.json out/light.dae

# Backend AI API without a key (returns bundled examples)
LLM_PROVIDER=mock uvicorn backend.app.main:app --port 8000
```

## The one rule that dominates everything: the Python/TypeScript mirror

Every pure-geometry module exists twice and must be changed in **exact lockstep** — dimensional parity between the web preview and the Blender export is a hard requirement (preview and export must agree on every dimension *by construction*):

| Python (source of truth for tests) | TypeScript mirror |
| --- | --- |
| `blender/builders/base.py` | `frontend/src/builders/base.ts` |
| `blender/builders/connections.py` (joint emitters) | `frontend/src/builders/connections.ts` |
| `blender/builders/hardware.py` (joint orchestrator) | `frontend/src/builders/hardware.ts` |
| `blender/builders/edits.py` (move/rotate/scale overlay) | `frontend/src/builders/edits.ts` |
| `blender/builders/audit.py` (connection auditor) | `frontend/src/builders/audit.ts` |
| `blender/builders/generic.py` + `expr.py` + `shapes.py` | `frontend/src/builders/generic.ts` + `expr.ts` + `shapes.ts` |
| `blender/builders/street_light.py` | `frontend/src/builders/streetLight.ts` |

There is no TS test runner: correctness of both sides is asserted by pytest on the Python side plus `npm run build` for types. If you change constants, thresholds, formulas, or ordering on one side, port it verbatim to the other in the same commit. Python-only (no mirror, by design): `connectivity.py` (buildability findings), `schedule.py` (joint schedule/BOM), `ops.py` (bpy realization).

## Architecture

One shared **AssetSpec** JSON (`schemas/asset_spec.schema.json`, root `additionalProperties: false`) drives the UI controls, the Three.js preview, and the Blender build. Spec state in `frontend/src/App.tsx` is the single source of truth; primitives and code checks recompute synchronously in `useMemo` on every change — the preview never waits on the server.

**Geometry conventions:** meters, +Z up, grade at z=0, nothing below grade. `Primitive.rotation` is a **Blender XYZ Euler (extrinsic, R = Rz·Ry·Rx — X applied first about fixed axes)**. Three.js renders it with Euler order `'ZYX'` (see `blenderEuler()` in `AssetMesh.tsx`); the shared rotation matrix lives in `hardware.py::_euler_xyz_matrix` / `hardware.ts::eulerXyzMatrix`. Never hand a primitive rotation to Three with the default `'XYZ'` order — that convention mismatch once produced floating gussets and was a whole class of bugs.

**Build pipeline** (`compute_primitives`, mirrored in `base.py`/`base.ts` — the ordering is load-bearing):
1. Curated builder registered by `asset_type` (e.g. `street_light`), else the generic primitives-in-spec path where the AI writes primitives with dimensions as sandboxed arithmetic **expressions** over slider parameters (`expr.py`/`expr.ts` — LLM output can never execute code).
2. `apply_structure` — duplicate/delete edits, BEFORE hardware so copies get joints and deleted parts don't attract bolts.
3. `apply_transforms` — user offsets/rotations/scales, BEFORE hardware so joints are detected where parts actually are (moved parts take their bolts with them).
4. Connection hardware generation (toggle `connection_hardware`), then a second transform pass for the `hardware` group's own edits.

**Connection system** (the most intricate subsystem):
- The spec's `connections[]` declares fabrication intent per component pair (`anchor_base`, `band_clamp`, `slip_fit`, `through_bolt`, `carriage_bolt`, `flange_splice`, `weld`, `lag_screw`, `none`); geometric inference is the fallback. `type: "none"` suppresses a joint — curated builders that model their own connection (street light's base) declare it.
- `hardware.py` collects AABB contacts, **merges contact regions of the same pair+declaration into one joint per physical junction** (`_merge_pair_candidates`), infers types (pipe standing on a grade-level base ⇒ weld at the exit seam, never a bolt down its own axis), ranks (anchors, declared, inferred) and budgets to `MAX_JOINTS`, then dispatches to emitters in `connections.py`. Each joint stamps a meta record (type, members, fastener, torque, center/axis/overlap/face) on its first prim — consumed by `schedule.py`, the install guide, and the auditor.
- `audit.py`/`audit.ts` — the "Check connections" button: deterministic findings (floating parts, below grade, dead/gapped declarations, sliver joints, detached hardware, hardware buried in unrelated parts), each with data-op fixes (`nudge`/`declare`/`undeclare`) and before→after text. Fixes apply **only** via `apply_audit_fixes` after user confirmation (`CheckPanel.tsx`; hovering Apply previews the fixed spec in 3D). One positional fix per component per round.
- Bolt sizes snap to the catalog in `hardware.py::BOLT_CATALOG`, which a test keeps in sync with `standards/us_codes.json` `_connections.bolt_catalog`.

**Blender realization** (`base.build` + `ops.py`) is the only place `bpy` is imported. Finishing (bevels, shade-smooth, solidify) is deliberately Blender-only; the preview mirrors *shape*, not finish. Objects are named `AssetName/ComponentName/PartName` in per-component collections so DAE imports into SketchUp as editable nested components — never collapse that hierarchy (no boolean unions).

**Standards/validation:** `standards/us_codes.json` (dimensional limits with citations) + pure `validator.py`. `code_mode: "strict"` clamps out-of-code dimensions on export; `"advisory"` (the UI's unlock toggle) only warns.

**Backend AI** (`backend/app/`, thin Vercel wrapper in `api/`): `spec_ai.py` builds prompts embedding the JSON schema, the standards DB, two example specs, and rule blocks; output is schema-validated, code-clamped, and test-built. Failures are **classified** (`SpecGenerationError.kind`: truncated / not_json / schema / build / buildability / provider) with a targeted `hint`, and the pipeline re-prompts with the correction + full error history up to `MAX_ATTEMPTS` (3) model calls — final attempt lenient on buildability, transient provider errors (429/5xx/timeout) retried, config errors aborted. Provider adapters: deepseek (default) / openai / anthropic / mock.

**Examples** (`examples/*.json`) double as demo assets and AI few-shots (`FEW_SHOT_*` in `spec_ai.py`). A new example must pass schema validation, build via `compute_primitives`, and have no `check_buildability` errors — the README's "Add an example to the folder" section has the exact snippet. `street_light.json` is the startup asset (`App.tsx` imports it).

## Test conventions

Tests are behavior-based (names, counts, invariants like "profile_start.w > profile_end.w"), not golden dumps — keep them that way so parametric tweaks don't churn fixtures. Anything touching geometry needs a test in `blender/tests/`; joint behavior tests live in `test_connections_vocab.py`, `test_hardware_offsets.py`, `test_connection_redesign.py`, auditor tests in `test_audit.py`.
