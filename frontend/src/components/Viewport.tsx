/** 3D viewport (T4.3/T4.4): orbit/pan/zoom, ground grid, 6 ft human
 * silhouette for scale, and dimension annotations that follow the spec. */
import { useEffect } from "react";
import { Canvas, useThree } from "@react-three/fiber";
import { Grid, Html, Line, OrbitControls } from "@react-three/drei";
import * as THREE from "three";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import type { AssetSpec, Primitive, UnitSystem } from "../types";
import { specParams } from "../builders";
import { formatLength } from "../units";
import AssetMesh, { type Selection } from "./AssetMesh";

const HUMAN_HEIGHT = 1.8288; // 6 ft

const THEME_COLORS = {
  light: { bg: "#eef1f5", cell: "#c3cad4", section: "#8d99a8", silhouette: "#3f4a5a" },
  dark: { bg: "#15181d", cell: "#2b323c", section: "#48525f", silhouette: "#8b98ab" },
};

/** Generated-in-memory studio environment (no network fetch) so metallic
 * materials have something real to reflect. */
function StudioEnvironment() {
  const gl = useThree((s) => s.gl);
  const scene = useThree((s) => s.scene);
  useEffect(() => {
    const pmrem = new THREE.PMREMGenerator(gl);
    const rt = pmrem.fromScene(new RoomEnvironment(), 0.04);
    scene.environment = rt.texture;
    return () => {
      scene.environment = null;
      rt.dispose();
      pmrem.dispose();
    };
  }, [gl, scene]);
  return null;
}

function HumanSilhouette({ x, color }: { x: number; color: string }) {
  return (
    <group position={[x, 0, 0]}>
      <mesh position={[0, 0.765, 0]} castShadow>
        <capsuleGeometry args={[0.2, 1.13, 6, 16]} />
        <meshStandardMaterial color={color} roughness={0.9} />
      </mesh>
      <mesh position={[0, 1.68, 0]} castShadow>
        <sphereGeometry args={[0.12, 16, 12]} />
        <meshStandardMaterial color={color} roughness={0.9} />
      </mesh>
      <Html position={[0, HUMAN_HEIGHT + 0.25, 0]} center>
        <div className="dim-label dim-label--muted">6 ft</div>
      </Html>
    </group>
  );
}

function VerticalDim({
  x,
  height,
  label,
}: {
  x: number;
  height: number;
  label: string;
}) {
  return (
    <group position={[x, 0, 0]}>
      <Line points={[[0, 0, 0], [0, height, 0]]} color="#e11d48" lineWidth={1.5} dashed dashSize={0.15} gapSize={0.1} />
      <Line points={[[-0.2, 0, 0], [0.2, 0, 0]]} color="#e11d48" lineWidth={1.5} />
      <Line points={[[-0.2, height, 0], [0.2, height, 0]]} color="#e11d48" lineWidth={1.5} />
      <Html position={[0, height / 2, 0]} center>
        <div className="dim-label">{label}</div>
      </Html>
    </group>
  );
}

function HorizontalDim({
  y,
  length,
  label,
}: {
  y: number;
  length: number;
  label: string;
}) {
  return (
    <group position={[0, y, 0]}>
      <Line points={[[0, 0, 0], [length, 0, 0]]} color="#0ea5e9" lineWidth={1.5} dashed dashSize={0.15} gapSize={0.1} />
      <Line points={[[0, -0.2, 0], [0, 0.2, 0]]} color="#0ea5e9" lineWidth={1.5} />
      <Line points={[[length, -0.2, 0], [length, 0.2, 0]]} color="#0ea5e9" lineWidth={1.5} />
      <Html position={[length / 2, 0.35, 0]} center>
        <div className="dim-label dim-label--blue">{label}</div>
      </Html>
    </group>
  );
}

export default function Viewport({
  spec,
  primitives,
  displayUnits,
  theme,
  selected,
  onSelect,
}: {
  spec: AssetSpec;
  primitives: Primitive[];
  displayUnits: UnitSystem;
  theme: "light" | "dark";
  selected: Selection | null;
  onSelect: (sel: Selection | null) => void;
}) {
  const colors = THEME_COLORS[theme];
  const p = specParams(spec);
  // overall height: trust an explicit height-ish parameter, else measure the
  // primitive list (exact for unrotated primitives, close enough otherwise)
  const measured = Math.max(
    HUMAN_HEIGHT * 0.25,
    ...primitives.map((prim) => {
      const z = prim.location[2];
      if (prim.kind === "cylinder" || prim.kind === "cone")
        return z + (prim.params.depth ?? 0) / 2;
      if (prim.kind === "box") return z + (prim.params.size?.[2] ?? 0) / 2;
      return z + (prim.params.radius ?? 0);
    }),
  );
  const heightM = p.pole_height ?? p.height ?? measured;
  const armM = p.arm_length ?? 0;
  const imperial = displayUnits === "imperial";

  return (
    <Canvas
      shadows
      camera={{ position: [heightM * 1.2, heightM * 0.9, heightM * 1.6], fov: 45 }}
      onPointerMissed={() => onSelect(null)}
    >
      <color attach="background" args={[colors.bg]} />
      <StudioEnvironment />
      <ambientLight intensity={0.35} />
      <directionalLight position={[15, 25, 12]} intensity={1.2} castShadow />

      {/* ground grid: 1 ft / 5 ft cells in imperial, 0.5 m / 5 m in metric */}
      <Grid
        infiniteGrid
        cellSize={imperial ? 0.3048 : 0.5}
        sectionSize={imperial ? 1.524 : 5}
        cellColor={colors.cell}
        sectionColor={colors.section}
        fadeDistance={60}
        fadeStrength={1.5}
      />
      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.001, 0]} receiveShadow>
        <planeGeometry args={[200, 200]} />
        <shadowMaterial opacity={theme === "dark" ? 0.45 : 0.25} />
      </mesh>

      {/* asset is authored Z-up; rotate into Three's Y-up world */}
      <group rotation={[-Math.PI / 2, 0, 0]}>
        <AssetMesh primitives={primitives} spec={spec} selected={selected} onSelect={onSelect} />
      </group>

      <HumanSilhouette x={-Math.max(2, armM * 0.4)} color={colors.silhouette} />
      <VerticalDim
        x={-Math.max(1.2, armM * 0.2)}
        height={heightM}
        label={formatLength(heightM, displayUnits)}
      />
      {armM > 0 && (
        <HorizontalDim
          y={heightM + 0.6}
          length={armM}
          label={formatLength(armM, displayUnits)}
        />
      )}

      <OrbitControls makeDefault target={[0, heightM / 2, 0]} />
    </Canvas>
  );
}
