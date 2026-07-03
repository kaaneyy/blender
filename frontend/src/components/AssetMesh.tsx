/** Renders the shared primitive list as Three.js meshes. The asset is
 * authored Z-up (Blender convention); the parent group in Viewport rotates
 * the whole thing into Three's Y-up world. */
import { useMemo } from "react";
import type { AssetSpec, Primitive } from "../types";
import { MATERIAL_PRESETS } from "../builders";

function materialFor(spec: AssetSpec, slot: string) {
  const bySlot: Record<string, string> = {};
  for (const m of spec.materials ?? []) bySlot[m.slot] = m.preset;
  const preset = bySlot[slot] ?? (slot === "lens" ? "lamp_lens" : "galvanized_steel");
  return MATERIAL_PRESETS[preset] ?? MATERIAL_PRESETS.galvanized_steel;
}

/** Rotates Three's Y-axis cylinders/cones onto the local Z axis so the
 * primitive params mean the same thing they do in Blender. */
const AXIS_FIX: [number, number, number] = [Math.PI / 2, 0, 0];

function PrimitiveMesh({ prim, spec }: { prim: Primitive; spec: AssetSpec }) {
  const mat = materialFor(spec, prim.materialSlot);
  const material = (
    <meshStandardMaterial color={mat.color} metalness={mat.metalness} roughness={mat.roughness} />
  );

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
      <mesh rotation={fix} castShadow receiveShadow>
        {geometry}
        {material}
      </mesh>
    </group>
  );
}

export default function AssetMesh({
  primitives,
  spec,
}: {
  primitives: Primitive[];
  spec: AssetSpec;
}) {
  // Rebuilds are a synchronous useMemo upstream; this component only maps
  // primitives to meshes, comfortably within the 16 ms budget (T4.3).
  const items = useMemo(
    () => primitives.map((p) => <PrimitiveMesh key={p.name} prim={p} spec={spec} />),
    [primitives, spec],
  );
  return <>{items}</>;
}
