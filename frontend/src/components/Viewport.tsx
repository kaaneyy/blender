/** 3D viewport: orbit/pan/zoom + WASD fly-through, ground grid, 6 ft human
 * silhouette, dimension annotations, click-to-select with camera focus, a
 * guided camera tour of connection hardware, and a maps-style nav column
 * (zoom, home, top view, compass, sun direction). */
import { useEffect, useRef, useState } from "react";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Grid, Html, Line, OrbitControls } from "@react-three/drei";
import * as THREE from "three";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import type { AssetSpec, Primitive, UnitSystem } from "../types";
import { halfExtents, specParams } from "../builders";
import { formatLength } from "../units";
import AssetMesh, { type Selection } from "./AssetMesh";

const HUMAN_HEIGHT = 1.8288; // 6 ft

const THEME_COLORS = {
  light: { bg: "#eef1f5", cell: "#c3cad4", section: "#8d99a8", silhouette: "#3f4a5a" },
  dark: { bg: "#15181d", cell: "#2b323c", section: "#48525f", silhouette: "#8b98ab" },
};

interface FocusPoint {
  target: THREE.Vector3;
  distance: number;
  dwell: number; // seconds to linger once arrived
}

interface FocusState {
  queue: FocusPoint[];
  dwellUntil: number;
}

interface ViewportApi {
  zoom(factor: number): void;
  home(): void;
  topView(): void;
  faceNorth(): void;
}

/** Blender Z-up point -> Three Y-up world (matches the asset group's -90° X). */
function zUpToYUp(v: readonly number[]): THREE.Vector3 {
  return new THREE.Vector3(v[0], v[2], -v[1]);
}

function boundsOf(prims: Primitive[]) {
  const lo = [Infinity, Infinity, Infinity];
  const hi = [-Infinity, -Infinity, -Infinity];
  for (const p of prims) {
    const h = halfExtents(p);
    for (let k = 0; k < 3; k++) {
      lo[k] = Math.min(lo[k], p.location[k] - h[k]);
      hi[k] = Math.max(hi[k], p.location[k] + h[k]);
    }
  }
  const center = [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2];
  const radius = Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) / 2;
  return { center, radius };
}

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

/** Inside-Canvas worker: WASD movement, smooth focus/tour animation, nav-API
 * exposure, and compass/sun needle updates (written straight to the DOM to
 * avoid re-rendering React every frame). */
function ViewportBridge({
  focusRef,
  apiRef,
  compassRef,
  sunNeedleRef,
  sunAzRef,
  homeHeightRef,
}: {
  focusRef: React.MutableRefObject<FocusState | null>;
  apiRef: React.MutableRefObject<ViewportApi | null>;
  compassRef: React.RefObject<HTMLDivElement>;
  sunNeedleRef: React.RefObject<HTMLDivElement>;
  sunAzRef: React.MutableRefObject<number>;
  homeHeightRef: React.MutableRefObject<number>;
}) {
  const camera = useThree((s) => s.camera);
  const controls = useThree((s) => s.controls) as unknown as {
    target: THREE.Vector3;
    update(): void;
    addEventListener(t: string, fn: () => void): void;
    removeEventListener(t: string, fn: () => void): void;
  } | null;
  const keysRef = useRef<Set<string>>(new Set());
  const homedRef = useRef(false);

  useEffect(() => {
    const isTyping = () => {
      const el = document.activeElement;
      return !!el && ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
    };
    const down = (e: KeyboardEvent) => {
      if (isTyping() || e.metaKey || e.ctrlKey || e.altKey) return;
      const k = e.key.toLowerCase();
      if ("wasdqe".includes(k) && k.length === 1) {
        keysRef.current.add(k);
        e.preventDefault();
      }
    };
    const up = (e: KeyboardEvent) => keysRef.current.delete(e.key.toLowerCase());
    const blur = () => keysRef.current.clear();
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    window.addEventListener("blur", blur);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
      window.removeEventListener("blur", blur);
    };
  }, []);

  // user drag cancels any focus animation / tour
  useEffect(() => {
    if (!controls) return;
    const onStart = () => {
      focusRef.current = null;
    };
    controls.addEventListener("start", onStart);
    return () => controls.removeEventListener("start", onStart);
  }, [controls, focusRef]);

  useEffect(() => {
    if (!controls) return;
    const api: ViewportApi = {
      zoom(factor) {
        focusRef.current = null;
        camera.position.sub(controls.target).multiplyScalar(factor).add(controls.target);
        controls.update();
      },
      home() {
        focusRef.current = null;
        const h = homeHeightRef.current;
        controls.target.set(0, h / 2, 0);
        camera.position.set(h * 1.2, h * 0.9, h * 1.6);
        controls.update();
      },
      topView() {
        focusRef.current = null;
        const d = camera.position.distanceTo(controls.target);
        camera.position.set(controls.target.x, controls.target.y + d, controls.target.z + 0.01);
        controls.update();
      },
      faceNorth() {
        focusRef.current = null;
        const off = camera.position.clone().sub(controls.target);
        const horiz = Math.hypot(off.x, off.z) || 1;
        camera.position.set(controls.target.x, camera.position.y, controls.target.z + horiz);
        controls.update();
      },
    };
    apiRef.current = api;
    if (!homedRef.current) {
      homedRef.current = true;
      api.home();
    }
  }, [controls, camera, apiRef, focusRef, homeHeightRef]);

  useFrame((state, dt) => {
    if (!controls) return;

    // WASD / QE fly-through: move camera and target together on the ground
    // plane (Q/E for down/up); speed scales with zoom distance
    const keys = keysRef.current;
    if (keys.size) {
      const fwd = new THREE.Vector3();
      camera.getWorldDirection(fwd);
      fwd.y = 0;
      if (fwd.lengthSq() < 1e-6) fwd.set(0, 0, -1);
      fwd.normalize();
      const right = new THREE.Vector3().crossVectors(fwd, camera.up).normalize();
      const move = new THREE.Vector3();
      if (keys.has("w")) move.add(fwd);
      if (keys.has("s")) move.sub(fwd);
      if (keys.has("d")) move.add(right);
      if (keys.has("a")) move.sub(right);
      if (keys.has("e")) move.y += 1;
      if (keys.has("q")) move.y -= 1;
      if (move.lengthSq() > 0) {
        focusRef.current = null;
        const speed = Math.max(2, camera.position.distanceTo(controls.target) * 0.7) * dt;
        move.normalize().multiplyScalar(speed);
        camera.position.add(move);
        controls.target.add(move);
        controls.update();
      }
    }

    // smooth focus / tour animation
    const f = focusRef.current;
    if (f && f.queue.length) {
      const fp = f.queue[0];
      const t = 1 - Math.exp(-5 * dt);
      controls.target.lerp(fp.target, t);
      const dir = camera.position.clone().sub(controls.target);
      const len = dir.length() || 1;
      dir.divideScalar(len);
      const desired = fp.target.clone().add(dir.multiplyScalar(fp.distance));
      camera.position.lerp(desired, t);
      controls.update();
      const arrived =
        controls.target.distanceTo(fp.target) < 0.04 &&
        Math.abs(camera.position.distanceTo(fp.target) - fp.distance) < 0.15;
      if (arrived) {
        if (!f.dwellUntil) {
          f.dwellUntil = state.clock.elapsedTime + fp.dwell;
        } else if (state.clock.elapsedTime >= f.dwellUntil) {
          f.queue.shift();
          f.dwellUntil = 0;
          if (!f.queue.length) focusRef.current = null;
        }
      }
    }

    // compass + sun needles (direct DOM writes, no React churn)
    const off = camera.position.clone().sub(controls.target);
    const camAz = Math.atan2(off.x, off.z);
    if (compassRef.current) {
      compassRef.current.style.transform = `rotate(${camAz}rad)`;
    }
    if (sunNeedleRef.current) {
      sunNeedleRef.current.style.transform = `rotate(${camAz - sunAzRef.current}rad)`;
    }
  });
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

function VerticalDim({ x, height, label }: { x: number; height: number; label: string }) {
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

function HorizontalDim({ y, length, label }: { y: number; length: number; label: string }) {
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
  tourId,
  homeId,
}: {
  spec: AssetSpec;
  primitives: Primitive[];
  displayUnits: UnitSystem;
  theme: "light" | "dark";
  selected: Selection | null;
  onSelect: (sel: Selection | null) => void;
  /** increments whenever connection hardware is switched ON → camera tour */
  tourId: number;
  /** increments when a new asset is adopted → glide back to the overview */
  homeId: number;
}) {
  const colors = THEME_COLORS[theme];
  const p = specParams(spec);
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

  const focusRef = useRef<FocusState | null>(null);
  const apiRef = useRef<ViewportApi | null>(null);
  const compassRef = useRef<HTMLDivElement>(null);
  const sunNeedleRef = useRef<HTMLDivElement>(null);
  const primsRef = useRef(primitives);
  primsRef.current = primitives;
  const homeHeightRef = useRef(heightM);
  homeHeightRef.current = heightM;

  const [sunAzDeg, setSunAzDeg] = useState(50);
  const sunAzRef = useRef(THREE.MathUtils.degToRad(50));
  sunAzRef.current = THREE.MathUtils.degToRad(sunAzDeg);
  const sunRad = THREE.MathUtils.degToRad(sunAzDeg);
  const sunPos: [number, number, number] = [Math.sin(sunRad) * 20, 24, Math.cos(sunRad) * 20];

  const [touring, setTouring] = useState(false);

  // a newly adopted asset (AI generate/refine) may be a completely different
  // size — glide the camera back to a framing overview
  useEffect(() => {
    if (!homeId) return;
    focusRef.current = {
      queue: [
        {
          target: new THREE.Vector3(0, homeHeightRef.current / 2, 0),
          distance: Math.max(homeHeightRef.current * 2.1, 3),
          dwell: 0,
        },
      ],
      dwellUntil: 0,
    };
  }, [homeId]);

  // clicking a part centers the camera on it
  useEffect(() => {
    if (!selected) return;
    const prims = primsRef.current.filter(
      (pr) => pr.component === selected.component && (!selected.part || pr.name === selected.part),
    );
    if (!prims.length) return;
    const { center, radius } = boundsOf(prims);
    focusRef.current = {
      queue: [{ target: zUpToYUp(center), distance: Math.max(radius * 3.2, 1.3), dwell: 0 }],
      dwellUntil: 0,
    };
  }, [selected]);

  // switching hardware ON tours every connection point in order, then zooms
  // back out to the whole asset
  useEffect(() => {
    if (!tourId) return;
    // one stop per joint (a joint may hold several bolts / a band clamp)
    const byJoint = new Map<number, Primitive>();
    for (const pr of primsRef.current) {
      if (pr.component !== "hardware") continue;
      const m = pr.name.match(/^joint(\d+)_/);
      if (!m) continue;
      const n = parseInt(m[1], 10);
      if (!byJoint.has(n)) byJoint.set(n, pr);
    }
    if (!byJoint.size) return;
    const queue: FocusPoint[] = [...byJoint.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([, pr]) => ({
        target: zUpToYUp(pr.location),
        distance: 0.85,
        dwell: 1.0,
      }));
    queue.push({
      target: new THREE.Vector3(0, homeHeightRef.current / 2, 0),
      distance: Math.max(homeHeightRef.current * 2.1, 3),
      dwell: 0,
    });
    focusRef.current = { queue, dwellUntil: 0 };
    setTouring(true);
    const timer = window.setInterval(() => {
      if (!focusRef.current) {
        setTouring(false);
        window.clearInterval(timer);
      }
    }, 400);
    return () => window.clearInterval(timer);
  }, [tourId]);

  return (
    <div className="viewport-wrap">
      <Canvas
        shadows
        camera={{ position: [heightM * 1.2, heightM * 0.9, heightM * 1.6], fov: 45 }}
        onPointerMissed={() => onSelect(null)}
      >
        <color attach="background" args={[colors.bg]} />
        <StudioEnvironment />
        <ambientLight intensity={0.35} />
        <directionalLight position={sunPos} intensity={1.2} castShadow />

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
          <HorizontalDim y={heightM + 0.6} length={armM} label={formatLength(armM, displayUnits)} />
        )}

        <OrbitControls makeDefault />
        <ViewportBridge
          focusRef={focusRef}
          apiRef={apiRef}
          compassRef={compassRef}
          sunNeedleRef={sunNeedleRef}
          sunAzRef={sunAzRef}
          homeHeightRef={homeHeightRef}
        />
      </Canvas>

      {/* maps-style navigation column */}
      <div className="nav-col">
        <button className="nav-btn" onClick={() => apiRef.current?.zoom(0.78)} title="Zoom in">
          ＋
        </button>
        <button className="nav-btn" onClick={() => apiRef.current?.zoom(1.28)} title="Zoom out">
          −
        </button>
        <button className="nav-btn" onClick={() => apiRef.current?.home()} title="Reset view">
          ⌂
        </button>
        <button className="nav-btn" onClick={() => apiRef.current?.topView()} title="Top view">
          ⬒
        </button>
        <button
          className="nav-btn nav-btn--dial"
          onClick={() => apiRef.current?.faceNorth()}
          title="Compass — click to face north"
        >
          <div className="dial" ref={compassRef}>
            <span className="dial__n">N</span>
            <div className="dial__needle" />
          </div>
        </button>
        <button
          className="nav-btn nav-btn--dial"
          onClick={() => setSunAzDeg((d) => (d + 45) % 360)}
          title={`Sun from ${sunAzDeg}° — click to rotate the sun`}
        >
          <span className="dial__sun">☀</span>
          <div className="dial" ref={sunNeedleRef}>
            <div className="dial__sundot" />
          </div>
        </button>
      </div>

      <div className="nav-hint">drag orbit · WASD move · Q/E down/up</div>
      {touring && <div className="tour-hint">🔩 Touring connection points…</div>}
    </div>
  );
}
