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
import type { AssetSpec, Primitive } from "../types";
import { resolveMaterial } from "../builders";

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
  return useMemo(() => {
    const tex = new THREE.CanvasTexture(getNoiseImage());
    tex.wrapS = tex.wrapT = THREE.RepeatWrapping;
    tex.repeat.set(resolved.uvScale, resolved.uvScale);
    const color = new THREE.Color(resolved.color);
    const emissive =
      highlight === "none" ? color : new THREE.Color(highlight === "part" ? "#2f6fed" : "#1d4ed8");
    const emissiveIntensity =
      highlight === "none" ? resolved.emission : Math.max(highlight === "part" ? 0.55 : 0.25, resolved.emission);
    return new THREE.MeshStandardMaterial({
      color,
      metalness: resolved.metalness,
      roughness: resolved.roughness,
      map: tex,
      emissive,
      emissiveIntensity,
    });
  }, [resolved.color, resolved.metalness, resolved.roughness, resolved.uvScale, resolved.emission, highlight]);
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
}: {
  prim: Primitive;
  spec: AssetSpec;
  selected: Selection | null;
  onSelect: (sel: Selection) => void;
}) {
  const highlight: "none" | "part" | "group" =
    selected?.component !== prim.component
      ? "none"
      : selected.part === prim.name
        ? "part"
        : selected.part
          ? "none"
          : "group";
  const material = useSlotMaterial(spec, prim.materialSlot, highlight);

  let geometry: JSX.Element;
  let fix: [number, number, number] = [0, 0, 0];
  switch (prim.kind) {
    case "cylinder":
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
  }

  return (
    <group position={prim.location} rotation={prim.rotation}>
      <mesh
        rotation={fix}
        castShadow
        receiveShadow
        material={material}
        onClick={(e) => {
          e.stopPropagation();
          // first click selects the group; clicking inside the selected
          // group drills down to the individual part
          onSelect(
            selected?.component === prim.component
              ? { component: prim.component, part: prim.name }
              : { component: prim.component },
          );
        }}
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
}: {
  primitives: Primitive[];
  spec: AssetSpec;
  selected: Selection | null;
  onSelect: (sel: Selection) => void;
}) {
  // Rebuilds are a synchronous useMemo upstream; this component only maps
  // primitives to meshes, comfortably within the 16 ms budget (T4.3).
  const items = useMemo(
    () =>
      primitives.map((p) => (
        <PrimitiveMesh key={p.name} prim={p} spec={spec} selected={selected} onSelect={onSelect} />
      )),
    [primitives, spec, selected, onSelect],
  );
  return <>{items}</>;
}
