/** Renders the shared primitive list as Three.js meshes. The asset is
 * authored Z-up (Blender convention); the parent group in Viewport rotates
 * the whole thing into Three's Y-up world.
 *
 * Materials come from resolveMaterial (preset + per-slot overrides). A
 * procedural grayscale noise texture provides visible surface detail whose
 * tiling follows the material's uv_scale slider; metalness/roughness/
 * emission map straight onto meshStandardMaterial. */
import { useMemo } from "react";
import * as THREE from "three";
import type { AssetSpec, LoftProfile, Primitive, Vec3 } from "../types";
import { aabb, resolveMaterial, weatheredShading } from "../builders";
import { resolveProfile, ringPoints } from "../shapes";

/** Loft: bridge two cross-section rings along local Z (mirror of
 * ops.realize_loft — shape parity, finishing stays Blender-side). */
function buildLoftGeometry(
  ps: LoftProfile,
  pe: LoftProfile,
  depth: number,
  n = 32,
): THREE.BufferGeometry {
  const r0 = ringPoints(ps.shape, ps.w, ps.h, n);
  const r1 = ringPoints(pe.shape, pe.w, pe.h, n);
  const positions: number[] = [];
  for (const [x, y] of r0) positions.push(x, y, -depth / 2);
  for (const [x, y] of r1) positions.push(x, y, depth / 2);
  positions.push(0, 0, -depth / 2); // bottom cap center (index 2n)
  positions.push(0, 0, depth / 2); // top cap center (index 2n+1)

  const indices: number[] = [];
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n;
    indices.push(i, j, n + j, i, n + j, n + i); // side quad
    indices.push(2 * n, j, i); // bottom cap fan (faces -Z)
    indices.push(2 * n + 1, n + i, n + j); // top cap fan (faces +Z)
  }
  const geom = new THREE.BufferGeometry();
  geom.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  geom.setIndex(indices);
  geom.computeVertexNormals();
  return geom;
}

/** Sweep: tapered tube along a path, previewed as lerped-radius segments
 * (dimension parity with the Blender curve sweep; smoothness differs). */
function SweepMesh({
  prim,
  material,
}: {
  prim: Primitive;
  material: THREE.MeshStandardMaterial;
}) {
  const segments = useMemo(() => {
    const path = prim.params.path!;
    const r0 = prim.params.radius!;
    const r1 = prim.params.radius_end ?? r0;
    const n = path.length - 1;
    return path.slice(0, -1).map((a, i) => {
      const b = path[i + 1];
      const dir = new THREE.Vector3(b[0] - a[0], b[1] - a[1], b[2] - a[2]);
      const len = dir.length() || 1e-6;
      const mid: Vec3 = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2];
      const q = new THREE.Quaternion().setFromUnitVectors(
        new THREE.Vector3(0, 1, 0),
        dir.divideScalar(len),
      );
      const ra = r0 + (r1 - r0) * (i / n);
      const rb = r0 + (r1 - r0) * ((i + 1) / n);
      return { mid, q, len: len * 1.04, ra, rb, key: i };
    });
  }, [prim]);

  return (
    <>
      {segments.map((s) => (
        <mesh key={s.key} position={s.mid} quaternion={s.q} material={material} castShadow receiveShadow>
          <cylinderGeometry args={[s.rb, s.ra, s.len, 20]} />
        </mesh>
      ))}
    </>
  );
}

/** Shared 256px near-white noise, generated once (deterministic seed). */
let noiseImage: HTMLCanvasElement | null = null;
function getNoiseImage(): HTMLCanvasElement {
  if (noiseImage) return noiseImage;
  const size = 256;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d")!;
  const img = ctx.createImageData(size, size);
  let s = 42; // mulberry32 — stable across sessions
  const rand = () => {
    s |= 0;
    s = (s + 0x6d2b79f5) | 0;
    let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  for (let i = 0; i < img.data.length; i += 4) {
    const v = 235 + Math.floor(rand() * 20); // subtle: 235-255
    img.data[i] = img.data[i + 1] = img.data[i + 2] = v;
    img.data[i + 3] = 255;
  }
  ctx.putImageData(img, 0, 0);
  noiseImage = canvas;
  return canvas;
}

/** Deterministic noise placement (offset + rotation) from the spec seed and
 * the material slot, so a new seed reshuffles the organic grime/grain pattern
 * and each slot varies a little. Preview-only surface finish — Blender does its
 * own texturing — so this needs no builder mirror. */
function noisePlacement(seed: number, slot: string): { ox: number; oy: number; rot: number } {
  let h = Math.imul((seed | 0) ^ 0x9e3779b9, 0x85ebca6b);
  for (let i = 0; i < slot.length; i++) h = Math.imul(h ^ slot.charCodeAt(i), 0x01000193);
  const next = () => {
    h = Math.imul(h ^ (h >>> 15), 1 | h);
    h = (h + Math.imul(h ^ (h >>> 7), 61 | h)) ^ h;
    return ((h ^ (h >>> 14)) >>> 0) / 4294967296;
  };
  return { ox: next(), oy: next(), rot: next() * Math.PI * 2 };
}

function useSlotMaterial(
  spec: AssetSpec,
  slot: string,
  highlight: "none" | "part" | "group",
  wireframe: boolean,
  lightsOn: boolean,
): THREE.MeshStandardMaterial {
  const resolved = resolveMaterial(spec, slot);
  const shade = weatheredShading(resolved); // D2: aged color/roughness/metalness
  const isEmitter = slot === "lens" || resolved.emission > 0;
  const seed = spec.seed ?? 0;
  return useMemo(() => {
    const tex = new THREE.CanvasTexture(getNoiseImage());
    tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
    // weathering tightens the grime pattern so dirt reads as finer speckle
    const tiles = resolved.uvScale * (1 + 1.5 * resolved.weathering);
    tex.repeat.set(tiles, tiles);
    // seed shuffles the grime/grain placement (and varies it per slot) so
    // "Randomize" produces organic variety without touching geometry
    const place = noisePlacement(seed, slot);
    tex.center.set(0.5, 0.5);
    tex.rotation = place.rot;
    tex.offset.set(place.ox, place.oy);
    const color = new THREE.Color(shade.color);
    // at night, emitter lenses glow noticeably (in their own color)
    const nightGlow = lightsOn && isEmitter ? Math.max(shade.emission, 2.5) : shade.emission;
    const emissive =
      highlight === "none" ? color : new THREE.Color(highlight === "part" ? "#2f6fed" : "#1d4ed8");
    const emissiveIntensity =
      highlight === "none" ? nightGlow : Math.max(highlight === "part" ? 0.55 : 0.25, nightGlow);
    return new THREE.MeshStandardMaterial({
      color,
      metalness: shade.metalness,
      roughness: shade.roughness,
      map: tex,
      emissive,
      emissiveIntensity,
      wireframe,
    });
  }, [shade.color, shade.metalness, shade.roughness, resolved.uvScale, resolved.weathering, shade.emission, highlight, wireframe, lightsOn, isEmitter, seed, slot]);
}

/** Rotates Three's Y-axis cylinders/cones onto the local Z axis so the
 * primitive params mean the same thing they do in Blender. */
const AXIS_FIX: [number, number, number] = [Math.PI / 2, 0, 0];

/** Primitive.rotation is a Blender-convention XYZ Euler (X applied first
 * about fixed axes). In Three.js that is Euler order 'ZYX' — using it here
 * keeps the preview identical to the Blender export for multi-axis rotated
 * parts (base gussets, radial set screws). */
function blenderEuler(rot: Vec3): THREE.Euler {
  return new THREE.Euler(rot[0], rot[1], rot[2], "ZYX");
}

export interface Selection {
  component: string;
  part?: string;
}

export function PrimitiveMesh({
  prim,
  spec,
  selected,
  onSelect,
  tourJoint,
  wireframe,
  lightsOn,
  explodeOffset,
  flash = false,
}: {
  prim: Primitive;
  spec: AssetSpec;
  selected: Selection | null;
  onSelect: (sel: Selection) => void;
  tourJoint: number | null;
  wireframe: boolean;
  lightsOn: boolean;
  explodeOffset: readonly [number, number, number];
  /** connection-check emphasis (hovered finding / fix preview changes) */
  flash?: boolean;
}) {
  const onTour =
    tourJoint !== null &&
    prim.component === "hardware" &&
    prim.name.startsWith(`joint${tourJoint}_`);
  const highlight: "none" | "part" | "group" = flash || onTour
    ? "part"
    : selected?.component !== prim.component
      ? "none"
      : selected.part === prim.name
        ? "part"
        : selected.part
          ? "none"
          : "group";
  const material = useSlotMaterial(spec, prim.materialSlot, highlight, wireframe, lightsOn);
  const pos: [number, number, number] = [
    prim.location[0] + explodeOffset[0],
    prim.location[1] + explodeOffset[1],
    prim.location[2] + explodeOffset[2],
  ];

  // custom geometry objects for the fabrication kinds
  const builtGeometry = useMemo(() => {
    if (prim.kind === "lathe") {
      const pts = resolveProfile(prim.params.profile!, prim.params.radius, prim.params.depth);
      return new THREE.LatheGeometry(
        pts.map(([r, z]) => new THREE.Vector2(Math.max(r, 1e-5), z)),
        32,
      );
    }
    if (prim.kind === "loft") {
      return buildLoftGeometry(
        prim.params.profile_start!,
        prim.params.profile_end!,
        prim.params.depth!,
      );
    }
    return null;
  }, [prim]);

  if (prim.cut) return null; // negative space: subtracted in Blender only

  const handleClick = (e: { stopPropagation(): void }) => {
    e.stopPropagation();
    // first click selects the group; clicking inside the selected group
    // drills down to the individual part
    onSelect(
      selected?.component === prim.component
        ? { component: prim.component, part: prim.name }
        : { component: prim.component },
    );
  };

  if (prim.kind === "sweep") {
    return (
      <group position={pos} rotation={blenderEuler(prim.rotation)} onClick={handleClick}>
        <SweepMesh prim={prim} material={material} />
      </group>
    );
  }

  let geometry: JSX.Element | null = null;
  let fix: [number, number, number] = [0, 0, 0];
  switch (prim.kind) {
    case "cylinder":
      geometry = (
        <cylinderGeometry args={[prim.params.radius!, prim.params.radius!, prim.params.depth!, 24]} />
      );
      fix = AXIS_FIX;
      break;
    // preview shows the outer shell; wall is a Blender solidify. section
    // "square" is hollow square stock (HSS) — radius is the half-width
    // across flats for both sections, so the outer shell is 2r across.
    case "tube":
      geometry =
        prim.params.section === "square" ? (
          <boxGeometry
            args={[prim.params.radius! * 2, prim.params.radius! * 2, prim.params.depth!]}
          />
        ) : (
          <cylinderGeometry
            args={[prim.params.radius!, prim.params.radius!, prim.params.depth!, 24]}
          />
        );
      // a box is already authored Z-up (like case "box"); only the round
      // section needs Three's Y-up cylinder rotated onto Z
      fix = prim.params.section === "square" ? [0, 0, 0] : AXIS_FIX;
      break;
    case "cone":
      geometry = (
        <cylinderGeometry
          args={[prim.params.radius_top!, prim.params.radius_bottom!, prim.params.depth!, 24]}
        />
      );
      fix = AXIS_FIX;
      break;
    case "box":
      geometry = <boxGeometry args={prim.params.size!} />;
      break;
    case "sphere":
      geometry = <sphereGeometry args={[prim.params.radius!, 24, 12]} />;
      break;
    case "lathe":
      fix = AXIS_FIX; // LatheGeometry revolves around Y; our axis is local Z
      break;
    case "loft":
      break; // built along local Z already
  }

  return (
    <group position={pos} rotation={blenderEuler(prim.rotation)}>
      <mesh
        rotation={fix}
        castShadow
        receiveShadow
        material={material}
        geometry={builtGeometry ?? undefined}
        onClick={handleClick}
      >
        {geometry}
      </mesh>
    </group>
  );
}

const NO_OFFSET: [number, number, number] = [0, 0, 0];

/** Exploded view: push each component away from the asset center along the
 * direction it already sits, so parts separate for inspection. Pure preview
 * transform — does not touch the spec or the Blender export. */
function explodeOffsets(primitives: Primitive[]): Record<string, [number, number, number]> {
  const byComp = new Map<string, { sum: [number, number, number]; n: number }>();
  const overall: [number, number, number] = [0, 0, 0];
  let count = 0;
  for (const p of primitives) {
    if (p.cut) continue;
    const c = aabb(p).center;
    const e = byComp.get(p.component) ?? { sum: [0, 0, 0], n: 0 };
    e.sum[0] += c[0]; e.sum[1] += c[1]; e.sum[2] += c[2]; e.n += 1;
    byComp.set(p.component, e);
    overall[0] += c[0]; overall[1] += c[1]; overall[2] += c[2]; count += 1;
  }
  if (!count) return {};
  const center: [number, number, number] = [overall[0] / count, overall[1] / count, overall[2] / count];
  const K = 0.8;
  const out: Record<string, [number, number, number]> = {};
  for (const [comp, e] of byComp) {
    const cc = [e.sum[0] / e.n, e.sum[1] / e.n, e.sum[2] / e.n];
    out[comp] = [(cc[0] - center[0]) * K, (cc[1] - center[1]) * K, (cc[2] - center[2]) * K];
  }
  return out;
}

/** True when the connection check wants this prim emphasized: its component
 * is flagged, its part path is flagged, or (for hardware) its joint is. */
function isFlashed(p: Primitive, flash?: Set<string>): boolean {
  if (!flash || flash.size === 0) return false;
  if (flash.has(p.component) || flash.has(`${p.component}/${p.name}`)) return true;
  if (p.component === "hardware") {
    const m = p.name.match(/^joint(\d+)_/);
    if (m && flash.has(`joint:${m[1]}`)) return true;
  }
  return false;
}

export default function AssetMesh({
  primitives,
  spec,
  selected,
  onSelect,
  tourJoint = null,
  wireframe = false,
  explode = false,
  lightsOn = false,
  editingKey = null,
  flash,
}: {
  primitives: Primitive[];
  spec: AssetSpec;
  selected: Selection | null;
  onSelect: (sel: Selection) => void;
  tourJoint?: number | null;
  wireframe?: boolean;
  explode?: boolean;
  lightsOn?: boolean;
  /** Edit target currently held by the transform gizmo — a component
   * ('pole') or a single part ('pole/shaft') — skipped here so the gizmo
   * can render and move it live without a double image. */
  editingKey?: string | null;
  /** Component names (or 'joint:N') the connection check highlights. */
  flash?: Set<string>;
}) {
  const offsets = useMemo(
    () => (explode ? explodeOffsets(primitives) : {}),
    [explode, primitives],
  );
  // Rebuilds are a synchronous useMemo upstream; this component only maps
  // primitives to meshes, comfortably within the 16 ms budget (T4.3).
  const items = useMemo(
    () =>
      primitives
        .filter(
          (p) => p.component !== editingKey && `${p.component}/${p.name}` !== editingKey,
        )
        .map((p) => (
          <PrimitiveMesh
            key={`${p.component}/${p.name}`}
            prim={p}
            spec={spec}
            selected={selected}
            onSelect={onSelect}
            tourJoint={tourJoint}
            wireframe={wireframe}
            lightsOn={lightsOn}
            explodeOffset={offsets[p.component] ?? NO_OFFSET}
            flash={isFlashed(p, flash)}
          />
        )),
    [primitives, spec, selected, onSelect, tourJoint, wireframe, lightsOn, offsets, editingKey, flash],
  );
  return <>{items}</>;
}
