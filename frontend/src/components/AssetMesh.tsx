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
import { resolveMaterial, weatheredShading } from "../builders";
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

function useSlotMaterial(
  spec: AssetSpec,
  slot: string,
  highlight: "none" | "part" | "group",
): THREE.MeshStandardMaterial {
  const resolved = resolveMaterial(spec, slot);
  const shade = weatheredShading(resolved); // D2: aged color/roughness/metalness
  return useMemo(() => {
    const tex = new THREE.CanvasTexture(getNoiseImage());
    tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
    // weathering tightens the grime pattern so dirt reads as finer speckle
    const tiles = resolved.uvScale * (1 + 1.5 * resolved.weathering);
    tex.repeat.set(tiles, tiles);
    const color = new THREE.Color(shade.color);
    const emissive =
      highlight === "none" ? color : new THREE.Color(highlight === "part" ? "#2f6fed" : "#1d4ed8");
    const emissiveIntensity =
      highlight === "none" ? shade.emission : Math.max(highlight === "part" ? 0.55 : 0.25, shade.emission);
    return new THREE.MeshStandardMaterial({
      color,
      metalness: shade.metalness,
      roughness: shade.roughness,
      map: tex,
      emissive,
      emissiveIntensity,
    });
  }, [shade.color, shade.metalness, shade.roughness, resolved.uvScale, resolved.weathering, shade.emission, highlight]);
}

/** Rotates Three's Y-axis cylinders/cones onto the local Z axis so the
 * primitive params mean the same thing they do in Blender. */
const AXIS_FIX: [number, number, number] = [Math.PI / 2, 0, 0];

export interface Selection {
  component: string;
  part?: string;
}

function PrimitiveMesh({
  prim,
  spec,
  selected,
  onSelect,
  tourJoint,
}: {
  prim: Primitive;
  spec: AssetSpec;
  selected: Selection | null;
  onSelect: (sel: Selection) => void;
  tourJoint: number | null;
}) {
  const onTour =
    tourJoint !== null &&
    prim.component === "hardware" &&
    prim.name.startsWith(`joint${tourJoint}_`);
  const highlight: "none" | "part" | "group" = onTour
    ? "part"
    : selected?.component !== prim.component
      ? "none"
      : selected.part === prim.name
        ? "part"
        : selected.part
          ? "none"
          : "group";
  const material = useSlotMaterial(spec, prim.materialSlot, highlight);

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
      <group position={prim.location} rotation={prim.rotation} onClick={handleClick}>
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
    case "tube": // preview shows the outer shell; wall is a Blender solidify
      geometry = (
        <cylinderGeometry args={[prim.params.radius!, prim.params.radius!, prim.params.depth!, 24]} />
      );
      fix = AXIS_FIX;
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
    <group position={prim.location} rotation={prim.rotation}>
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

export default function AssetMesh({
  primitives,
  spec,
  selected,
  onSelect,
  tourJoint = null,
}: {
  primitives: Primitive[];
  spec: AssetSpec;
  selected: Selection | null;
  onSelect: (sel: Selection) => void;
  tourJoint?: number | null;
}) {
  // Rebuilds are a synchronous useMemo upstream; this component only maps
  // primitives to meshes, comfortably within the 16 ms budget (T4.3).
  const items = useMemo(
    () =>
      primitives.map((p) => (
        <PrimitiveMesh
          key={p.name}
          prim={p}
          spec={spec}
          selected={selected}
          onSelect={onSelect}
          tourJoint={tourJoint}
        />
      )),
    [primitives, spec, selected, onSelect, tourJoint],
  );
  return <>{items}</>;
}
