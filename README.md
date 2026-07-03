# AssetForge

AI parametric asset generator: prompt → parametric spec → live 3D preview →
US-code-validated dimensions → headless Blender build → `.dae`/`.obj`/`.glb`/`.fbx`
export for SketchUp and animation pipelines.

![AssetForge live preview](docs/preview.png)

The full roadmap lives in [`docs/BUILD_PLAN.md`](docs/BUILD_PLAN.md). Shipped so far:

- **Milestone 1 — headless proof**: shared schemas, US-code standards DB +
  validator, builder registry, `street_light` builder, CLI harness.
- **Milestone 2 — live preview**: the web app above. Drag sliders, flip
  toggles, swap materials; dimensions outside US code turn red with the code
  citation and a "snap to code" button. A 6 ft human silhouette and live
  dimension callouts keep the scale honest.

**Not built yet** (coming milestones): the AI prompt box (type "a 30 ft art-deco
street light" → spec) and the final SketchUp `.dae` download from the browser.

---

## How to run it — pick one option

### Option A — Vercel, nothing to install (recommended)

Vercel is a free hosting service that builds and publishes this app for you.
You only use your web browser; there is nothing to download or install, and
the free "Hobby" plan is enough.

1. Open **[vercel.com/signup](https://vercel.com/signup)** and choose
   **Continue with GitHub**. Sign in with the same GitHub account that owns
   this repository and authorize Vercel when asked.
2. On your Vercel dashboard, click **Add New… → Project**.
3. You'll see a list of your GitHub repositories. Find **blender** (this
   repository) and click **Import**.
   - If the repository isn't listed, click **Adjust GitHub App Permissions**
     (or "Install"), grant Vercel access to the repository, and it will appear.
4. On the configuration screen, **change nothing** — this repository contains
   a `vercel.json` file that tells Vercel exactly how to build the app.
5. Click **Deploy** and wait about a minute.
6. Click the big preview image / **Visit** button. That URL is your app — it
   works on any device, and you can share it.

From now on, **every time new code is pushed to this repository, Vercel
rebuilds and updates your URL automatically.** You never have to repeat these
steps.

**What works on Vercel:** the entire live preview — sliders, toggles,
materials, code checking, spec download.
**What can't run on Vercel:** the final Blender export to `.dae`/`.glb`
(Vercel has no Blender) and, later, the AI endpoints. Those will run on a
small server (e.g. Render/Railway/Fly.io) when milestones 3–4 land —
step-by-step instructions will be added here at that point.

### Option B — Run on your own computer (one install)

1. Install **Node.js**: go to [nodejs.org](https://nodejs.org), click the
   green **LTS** button, run the downloaded installer, keep every default.
2. Get this code onto your computer: on the GitHub page of this repository,
   click the green **Code** button → **Download ZIP**, then unzip it
   somewhere easy to find (e.g. your Desktop).
3. Open a terminal:
   - **Windows:** press the Windows key, type `cmd`, press Enter.
   - **Mac:** press ⌘-space, type `Terminal`, press Enter.
4. Type these three lines, pressing Enter after each (adjust the first path
   to wherever you unzipped):

   ```
   cd Desktop/blender-main/frontend
   npm install
   npm run dev
   ```

5. Open **http://localhost:5173** in your browser. Leave the terminal window
   open while you use the app; press `Ctrl+C` in it to stop.

### Option C — Full stack (for developers)

```bash
# Python tests: validator + builders, no Blender required
pip install pytest && pytest

# Validate a spec and dump its primitive list (no Blender required)
python3 blender/build_cli.py examples/street_light.json out/primitives.json

# Full headless build + export (requires Blender)
blender -b -P blender/build_cli.py -- examples/street_light.json out/light.dae   # SketchUp
blender -b -P blender/build_cli.py -- examples/street_light.json out/light.glb   # web/animation

# API + workers
docker compose up --build backend
curl -X POST localhost:8000/validate-spec -H 'content-type: application/json' \
     -d @examples/street_light.json
```

---

## Repository layout

| Path | Purpose |
| --- | --- |
| `schemas/` | Shared JSON Schemas (`AssetSpec`, `ExportRequest`) that drive UI, preview, and Blender alike |
| `standards/` | `us_codes.json` (per-asset dimensional limits with citations) + pure `validator.py` |
| `blender/` | Builder registry (`builders/base.py`), asset builders, `build_cli.py` harness, worker Dockerfile |
| `backend/` | FastAPI service (health, standards, spec validation; LLM + export endpoints land in later milestones) |
| `frontend/` | Vite + React + react-three-fiber live preview (deployed by Vercel) |
| `examples/` | Ready-to-build example specs |

## Architecture rule

One shared JSON **AssetSpec** drives everything — the Three.js preview, the
Blender builder, and the UI controls are all generated from it. Builders are
split into a *pure primitive layer* (`compute_primitives(spec)`, plain
cylinders/cones/boxes in meters, no `bpy`, unit-testable anywhere) and a thin
Blender *realization layer* (`builders/base.py:build`). The frontend mirrors
the primitive layer 1:1 (`frontend/src/builders/`), so preview and final
export agree on every dimension by construction. The standards DB
(`standards/us_codes.json`) is likewise imported by both the Python validator
and the browser — one source of truth, never copied.

Out-of-code dimensions are clamped automatically (`code_mode: "strict"`) or
reported as warnings only (`"advisory"`, for props that don't need
compliance), always with the code citation attached.

## Why Blender as the engine (SketchUp as the destination)

- The SketchUp C SDK (writes `.skp`) is Windows/macOS only — no Linux headless generation.
- No SketchUp browser runtime exists for live preview; SketchUp Free web has no API.
- Blender headless is Linux-native, free, fully scriptable, and exports `.dae`,
  which SketchUp imports losslessly for this asset class — including the
  `AssetName/ComponentName` node hierarchy, so poles/arms/heads arrive as
  separately editable components.
