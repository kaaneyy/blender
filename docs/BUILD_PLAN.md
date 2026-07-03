# AssetForge — AI Parametric Asset Generator (Blender backend, SketchUp-compatible export)

GOAL: User prompts an asset (lamp, street light, bench, bollard, sign, prop). LLM converts prompt → parametric spec. Browser shows live 3D preview with toggles/sliders. US-code standards DB clamps dimensions. Final render built headless in Blender, exported as .dae/.obj/.glb for SketchUp/animation pipelines.

STACK: React + react-three-fiber (preview) · Node/Express or FastAPI (API) · Blender headless + bpy (final geometry) · Claude/OpenAI/DeepSeek (prompt→spec) · Postgres or SQLite (projects) · S3/local disk (exports).

ARCHITECTURE RULE: One shared JSON "AssetSpec" schema drives everything — the Three.js preview, the Blender builder, and the UI controls are all generated from it. Never hand-build UI per asset.

> Progress key: `[x]` shipped · `[~]` partially shipped · `[ ]` not started.

---

## PHASE 0 — Foundations

- [x] T0.1 Init monorepo: `/frontend` (Vite + React + TS), `/backend` (FastAPI), `/blender` (python scripts), `/standards` (JSON code DB), `/schemas` (shared JSON Schema).
- [x] T0.2 Define `AssetSpec` JSON Schema → `schemas/asset_spec.schema.json`:
      ```json
      {
        "asset_type": "street_light",
        "name": "string",
        "units": "imperial|metric",
        "parameters": [
          { "id": "pole_height", "label": "Pole Height", "type": "slider",
            "min": 20, "max": 40, "step": 0.5, "value": 30, "unit": "ft",
            "code_ref": "AASHTO-RDG-lighting" }
        ],
        "toggles": [
          { "id": "double_arm", "label": "Double Arm", "value": false },
          { "id": "banner_bracket", "label": "Banner Bracket", "value": false }
        ],
        "materials": [{ "slot": "pole", "preset": "galvanized_steel" }],
        "components": ["pole", "arm", "luminaire", "base_plate"],
        "seed": 42
      }
      ```
- [x] T0.3 Define `ExportRequest` schema: spec + format (`dae|obj|glb|fbx`) + LOD level + poly budget → `schemas/export_request.schema.json`.
- [x] T0.4 Docker compose: backend, Blender worker (image with blender installed), Postgres, Redis (job queue).

## PHASE 1 — Standards Database (do BEFORE any LLM work)

- [x] T1.1 Create `/standards/us_codes.json` — per asset_type, hard min/max/default dims with source citation. Seed entries:
      - street_light: pole height 20–40 ft (typ. 25–35), arm length 4–15 ft, luminaire mount ≥ 8 ft over walkway (source: AASHTO Roadway Lighting Design Guide, local DOT typicals)
      - pedestrian_lamp: 10–15 ft
      - traffic_sign: bottom of sign ≥ 7 ft above sidewalk (MUTCD 2A.18)
      - bollard: 30–36 in height, 4–8 in dia; ADA detectable
      - bench: seat 17–19 in high (ADA-friendly), depth 20–24 in
      - handrail: 34–38 in (IBC 1014.2)
      - door prop: 80 in clear height min (ADA/IBC)
      - fire_hydrant: outlet 18 in min above grade
- [x] T1.2 Write `validator.py`: given AssetSpec → clamp values to code ranges, return list of violations + auto-corrections. Pure function, unit tested. → `standards/validator.py` + `standards/tests/test_validator.py`
- [x] T1.3 Add `code_mode` flag to spec: `strict` (clamp) | `advisory` (warn only, for movie/animation props that don't need code compliance).

## PHASE 2 — LLM Layer (prompt → AssetSpec)

- [x] T2.1 Backend endpoint `POST /generate-spec` → calls LLM with system prompt: "Return ONLY valid AssetSpec JSON matching this schema. Choose parameter ranges from the provided standards table. No prose." → `backend/app/spec_ai.py`
- [x] T2.2 Inject the relevant slice of `us_codes.json` into the LLM context so ranges come from the DB, not model memory. *(Whole DB is injected — it's small.)*
- [x] T2.3 Post-process: parse JSON → run `validator.py` → clamp → return spec + violations to client. *(Plus a test-build of the geometry so unbuildable specs are rejected server-side.)*
- [x] T2.4 Provider abstraction: `llm.py` with adapters for Anthropic / OpenAI / DeepSeek behind one interface (env var selects provider). *(Plus a keyless `mock` provider for tests/demo.)*
- [x] T2.5 Endpoint `POST /refine-spec`: current spec + user chat message ("make it art-deco, add a second arm") → LLM returns modified spec. Always re-validate.
- [x] T2.6 Retry logic: if JSON parse fails, re-prompt once with the parse error appended.
- [x] T2.7 *(beyond plan)* "Generate anything": specs may carry their own `primitives` array with dimensions as sandboxed arithmetic expressions over parameter/toggle ids (`blender/builders/{expr,generic}.py`, mirrored in `frontend/src/{expr,builders/generic}.ts`) — AI-generated assets stay fully slider-parametric without a curated builder.

## PHASE 3 — Procedural Geometry (the core)

Strategy: implement each asset_type ONCE as a Python builder module used by BOTH a lightweight JS mirror (preview) and Blender (final). Keep builders parametric-primitive based (cylinders, boxes, lathes, arrays) so JS and bpy stay in sync.

- [x] T3.1 `/blender/builders/base.py`: builder registry, `build(spec) -> bpy objects`, component naming convention `AssetName/ComponentName` (SketchUp imports DAE nodes as nested components — naming matters). *Implemented with a pure `compute_primitives` layer (no bpy, unit-testable, mirrors to JS) + bpy realization layer.*
- [~] T3.2 Implement builders (each ~1 file, each maps params/toggles → geometry):
      - [x] `street_light.py` (pole taper, arm curve, luminaire head, base plate + anchor bolts toggle)
      - [ ] `pedestrian_lamp.py` (post lantern styles: acorn, teardrop, modern)
      - [ ] `bench.py`, `bollard.py`, `traffic_sign.py`, `trash_bin.py`, `planter.py`, `hydrant.py`
- [~] T3.3 Material system: PBR presets (galvanized steel, powder-coat black, cast iron, concrete, brushed aluminum) assigned per component slot; bake to simple diffuse for DAE (SketchUp ignores full PBR). *Presets + per-slot overrides (color, metalness/reflection, roughness, uv_scale, emission) shipped as spec fields, UI sliders, and prompt-settable properties; applied to Principled BSDF in Blender and mirrored in the preview. Image-texture baking pending.*
- [ ] T3.4 LOD generator: decimate modifier at 100% / 50% / 20% poly budget, user-selectable.
- [x] T3.5 CLI harness: `blender -b -P build_cli.py -- spec.json out.glb` for testing without the web app. *Also runs Blender-free (`python3 blender/build_cli.py spec.json out.json` validates + dumps primitives) and exports native `.blend` files.*

## PHASE 3.5 — Editor & tooling extensions (added beyond the original plan)

- [x] E1 Click-to-select in the viewport: first click selects the component group, second the individual part; selected geometry glows blue.
- [x] E2 Focused selection panel: shows only the clicked part/group's settings — part count, measured W/D/H, per-axis position nudges (persisted as `spec.offsets`, honored by the Blender export), directly editable dimensions for custom assets.
- [x] E3 Connection-hardware pass: `connection_hardware` spec toggle materializes bolt/nut assemblies (6-segment hex cylinders) at every inter-component joint, in preview and exports alike (`blender/builders/hardware.py` + TS mirror).
- [x] E4 AI installation guide: `POST /install-guide` → Markdown instructions grounded in the spec's components, dimensions, and code citations; modal view + .md download.
- [x] E5 AI standards refresh: `POST /update-standards` → structurally validated proposal for `us_codes.json`; commits to GitHub via the contents API when `GITHUB_TOKEN` is set, else offered as a download.
- [x] E6 Dark mode: CSS-variable theming + viewport palette swap, persisted per user.

## PHASE 4 — Live Preview (frontend)

- [x] T4.1 React app shell: prompt box + chat refine panel (left), 3D viewport (center), auto-generated controls panel (right). *Prompt box present but disabled until Phase 2 ships.*
- [x] T4.2 Controls panel is 100% schema-driven: sliders from `parameters[]`, switches from `toggles[]`, dropdowns from `materials[]`. Zero per-asset UI code. → `frontend/src/components/ControlsPanel.tsx`
- [x] T4.3 Three.js preview builders mirroring Phase 3 (`/frontend/src/builders/*.ts`) — approximate is fine; parity on dimensions is mandatory. Rebuild mesh on any control change (<16ms target; debounce heavy assets). *`streetLight.ts` is a 1:1 port of the Python primitive layer; rebuild is a synchronous useMemo.*
- [x] T4.4 Viewport features: orbit/pan/zoom, ground grid in ft/m, 6ft human silhouette for scale, dimension annotations (height/width callouts), imperial↔metric toggle.
- [x] T4.5 Code-violation UI: slider handle turns red + tooltip with code citation when outside range; "snap to code" button. *Client-side check reads the same `standards/us_codes.json` the Python validator uses.*
- [x] T4.6 Fast path: preview never waits on the server. Server round-trips only for LLM calls and final export. *The preview app is fully client-side (deployable to static hosting/Vercel).*

## PHASE 5 — Final Render & Export

- [ ] T5.1 `POST /export`: enqueue job (Redis) → Blender worker builds from spec → exports requested format(s).
- [~] T5.2 Export formats: **.dae (Collada — SketchUp native import, DEFAULT)**, .obj+.mtl, .glb, .fbx (for Blender/animation users). Bundle as .zip with a README noting import steps + license. *All four formats wired in `build_cli.py`; zip bundling pending.*
- [~] T5.3 Component hierarchy preserved in DAE so SketchUp imports grouped, named components (pole/arm/head editable separately). *Collection-per-component naming implemented in `builders/base.py`; needs verification against a real SketchUp import.*
- [ ] T5.4 Thumbnail render: Cycles/EEVEE 512px turntable PNG per export, shown in library.
- [ ] T5.5 (Optional, later) .skp worker: Windows VM + SketchUp C SDK converting .dae → .skp for one-click native files.
- [ ] T5.6 Job status endpoint + progress toast in UI; download link on completion.

## PHASE 6 — Quality-of-life features

- [ ] T6.1 Asset library: save/load specs per user, fork existing assets, tag & search.
- [ ] T6.2 Preset gallery: 20+ curated starting specs per category (user clicks instead of prompting).
- [ ] T6.3 Variation generator: "give me 4 variants" → LLM perturbs spec within code limits, grid preview, pick one.
- [ ] T6.4 Batch export: select N assets → one zip.
- [ ] T6.5 Share link: read-only spec URL others can fork.
- [ ] T6.6 Randomize-seed button for organic details (weathering, wood grain UV offsets).
- [ ] T6.7 Undo/redo stack on spec state (immutable spec snapshots).
- [ ] T6.8 Prompt history + "explain my asset" (LLM summarizes dims + code refs, exportable spec sheet PDF).
- [ ] T6.9 Scene context toggle: drop asset onto sample sidewalk/street scene to judge scale.

## PHASE 7 — Hardening

- [~] T7.1 Unit tests: validator clamping, every builder at min/mid/max params (no NaN verts, watertight where expected). *Validator + street_light covered; grows with each new builder.*
- [ ] T7.2 Golden-file tests: spec → export → assert bounding box matches spec dims within 1%.
- [ ] T7.3 Rate limiting + LLM cost caps per user.
- [x] T7.4 Sanitize LLM output strictly against JSON Schema (reject unknown fields), plus a sandboxed expression evaluator (arithmetic only) so LLM specs can never execute code.
- [ ] T7.5 Load test Blender worker; scale via queue concurrency.

---

## BUILD ORDER (dependency-sorted, ship a demo at each ✂)
1. **T0.\* → T1.\* → T3.1 + one builder (street_light) → T3.5 CLI  ✂ *(headless proof)* ← SHIPPED**
2. **T4.1–T4.4 with street_light JS mirror ✂ *(live preview, no AI yet)* ← SHIPPED (incl. T4.5/T4.6; deploys to Vercel via root `vercel.json`)**
3. **T2.* ✂ *(prompt → spec → preview loop complete)* ← SHIPPED (DeepSeek default; backend also runs as a Vercel Python function via `api/index.py`)**
4. T5.1–T5.3 ✂ *(end-to-end: prompt → tweak → download .dae → import to SketchUp)*
5. Remaining builders → Phase 6 → Phase 7.

## WHY NOT SKETCHUP-NATIVE (record for the agent)
- SketchUp C SDK (writes .skp) is Windows/macOS only — no Linux headless generation.
- No SketchUp browser runtime for live preview; SketchUp Free web has no API.
- Blender headless is Linux-native, free, fully scriptable, and exports .dae which SketchUp imports losslessly for this asset class. SketchUp remains the destination, not the engine.
