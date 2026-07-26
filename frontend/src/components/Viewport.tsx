/** 3D viewport: orbit/pan/zoom + WASD fly-through, ground grid, 6 ft human
 * silhouette, dimension annotations, click-to-select with camera focus, a
 * guided camera tour of connection hardware, a maps-style nav column (zoom,
 * home, top view, compass, sun direction), and a SketchUp-style edit column
 * (move / rotate / stretch / duplicate / delete) that drives a transform
 * gizmo and bakes the result back into the spec. */
import { useEffect, useMemo, useRef, useState } from "react";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Grid, Html, Line, OrbitControls, TransformControls } from "@react-three/drei";
import * as THREE from "three";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import { GLTFExporter } from "three/examples/jsm/exporters/GLTFExporter.js";
import type { AssetSpec, Primitive, UnitSystem, Vec3 } from "../types";
import { aabb, resolveMaterial, specParams, preEditPrimitives, componentPivot } from "../builders";
import { formatLength } from "../units";
import { lightProfile } from "../lighting";
import AssetMesh, { type Selection, PrimitiveMesh } from "./AssetMesh";

/** Which manipulation the transform gizmo performs. */
export type EditMode = "translate" | "rotate" | "scale";

/** A gizmo transform read back for baking into the spec. */
export interface CommittedTransform {
  offset: Vec3;
  rotation: Vec3;
  scale: Vec3;
}

const HUMAN_HEIGHT = 1.8288; // 6 ft

/** z-index cap for the in-scene <Html> dimension labels. drei defaults Html to
 * ~16.7M, which paints these labels OVER app chrome (modals show through them).
 * 40 keeps them above the viewport (grid/scene) and above the nav/hint chrome
 * as before, but below the modal overlay (z-index 50 in styles.css), so a modal
 * cleanly covers them. */
const LABEL_Z_RANGE: [number, number] = [40, 0];

const THEME_COLORS = {
  light: { bg: "#eef1f5", cell: "#c3cad4", section: "#8d99a8", silhouette: "#3f4a5a" },
  dark: { bg: "#15181d", cell: "#2b323c", section: "#48525f", silhouette: "#8b98ab" },
};

interface FocusPoint {
  target: THREE.Vector3;
  distance: number;
  dwell: number; // seconds to linger once arrived
  /** joint number being visited (tour stops only) — used for highlighting */
  joint?: number;
}

interface FocusState {
  queue: FocusPoint[];
  dwellUntil: number;
}

interface ViewportApi {
  zoom(factor: number): void;
  home(): void;
  topView(): void;
  frontView(): void;
  sideView(): void;
  faceNorth(): void;
  screenshot(): void;
  /** Export the asset-only subtree as a binary .glb and trigger a browser
   * download under `filename`. */
  exportGlb(filename: string): void;
}

/** Object3D `.type` values that must never end up inside a GLB export of the
 * asset — viewport chrome (lights, grid/ground helpers, gizmo pieces,
 * dimension lines) lives outside the asset group by construction, but this
 * is a cheap defensive check run on the export clone right before
 * serializing, so a future regression fails loudly instead of shipping a
 * bad export. Pure and Three-free (duck-typed) so it's checkable without a
 * renderer by feeding it plain {type, name, children} objects — no THREE.js
 * scene or DOM required. */
const FORBIDDEN_EXPORT_TYPES = new Set([
  "Light",
  "PointLight",
  "DirectionalLight",
  "AmbientLight",
  "SpotLight",
  "HemisphereLight",
  "GridHelper",
  "Line",
  "Line2",
  "LineSegments",
]);

interface ExportNodeLike {
  type: string;
  name: string;
  children?: readonly ExportNodeLike[];
}

/** Pure: walks a node tree (real Object3D or a plain duck-typed stand-in)
 * and returns the name/type of the first disallowed node found, or null if
 * the subtree is clean. */
export function findForbiddenExportNode(node: ExportNodeLike): string | null {
  if (FORBIDDEN_EXPORT_TYPES.has(node.type)) return node.name || node.type;
  for (const child of node.children ?? []) {
    const hit = findForbiddenExportNode(child);
    if (hit) return hit;
  }
  return null;
}

/** Pure: turn the spec's display name (or asset_type as fallback) into a
 * safe .glb download filename — lowercase, spaces to underscores, and
 * anything outside [a-z0-9_-] stripped outright. Falls back to "asset" if
 * nothing survives sanitization. */
export function sanitizeExportFilename(raw: string): string {
  const base = raw
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "_")
    .replace(/[^a-z0-9_-]/g, "");
  return `${base || "asset"}.glb`;
}

/** Blender Z-up point -> Three Y-up world (matches the asset group's -90° X). */
function zUpToYUp(v: readonly number[]): THREE.Vector3 {
  return new THREE.Vector3(v[0], v[2], -v[1]);
}

/** Light-emitter primitives: lens parts or anything with material emission. */
function findEmitters(primitives: Primitive[], spec: AssetSpec): Primitive[] {
  return primitives.filter(
    (p) =>
      !p.cut &&
      (p.materialSlot === "lens" || resolveMaterial(spec, p.materialSlot).emission > 0),
  );
}

function boundsOf(prims: Primitive[]) {
  const lo = [Infinity, Infinity, Infinity];
  const hi = [-Infinity, -Infinity, -Infinity];
  for (const p of prims) {
    const box = aabb(p);
    for (let k = 0; k < 3; k++) {
      lo[k] = Math.min(lo[k], box.center[k] - box.half[k]);
      hi[k] = Math.max(hi[k], box.center[k] + box.half[k]);
    }
  }
  const center = [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2];
  const radius = Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) / 2;
  return { center, radius };
}

/** Generated-in-memory studio environment (no network fetch) so metallic
 * materials have something real to reflect. Dimmed at night so metals stop
 * reflecting a bright room. */
function StudioEnvironment({ dim = false }: { dim?: boolean }) {
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
  useEffect(() => {
    // three r155+ scales IBL contribution; harmless no-op on older builds
    (scene as { environmentIntensity?: number }).environmentIntensity = dim ? 0.12 : 1.0;
  }, [scene, dim]);
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
  assetGroupRef,
  onFocusChange,
  onExportError,
}: {
  focusRef: React.MutableRefObject<FocusState | null>;
  apiRef: React.MutableRefObject<ViewportApi | null>;
  compassRef: React.RefObject<HTMLDivElement>;
  sunNeedleRef: React.RefObject<HTMLDivElement>;
  sunAzRef: React.MutableRefObject<number>;
  homeHeightRef: React.MutableRefObject<number>;
  /** Wraps only the rendered asset primitives (see the `assetGroupRef` group
   * in the main render tree) — no lights, grid/ground, silhouette, dimension
   * lines, or gizmo widgets. That's what gets exported. */
  assetGroupRef: React.RefObject<THREE.Group>;
  onFocusChange: (fp: FocusPoint | null) => void;
  onExportError: (message: string) => void;
}) {
  const lastHeadRef = useRef<FocusPoint | null>(null);
  const camera = useThree((s) => s.camera);
  const gl = useThree((s) => s.gl);
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
      frontView() {
        focusRef.current = null;
        const d = camera.position.distanceTo(controls.target);
        camera.position.set(controls.target.x, controls.target.y, controls.target.z + d);
        controls.update();
      },
      sideView() {
        focusRef.current = null;
        const d = camera.position.distanceTo(controls.target);
        camera.position.set(controls.target.x + d, controls.target.y, controls.target.z);
        controls.update();
      },
      faceNorth() {
        focusRef.current = null;
        const off = camera.position.clone().sub(controls.target);
        const horiz = Math.hypot(off.x, off.z) || 1;
        camera.position.set(controls.target.x, camera.position.y, controls.target.z + horiz);
        controls.update();
      },
      screenshot() {
        // preserveDrawingBuffer on the Canvas keeps the buffer readable
        const url = gl.domElement.toDataURL("image/png");
        const a = document.createElement("a");
        a.href = url;
        a.download = "assetforge.png";
        a.click();
      },
      exportGlb(filename) {
        const assetRoot = assetGroupRef.current;
        if (!assetRoot) {
          onExportError("Nothing to export yet.");
          return;
        }
        try {
          // Clone so the live scene is untouched (materials/geometries are
          // shared by reference — fine for a read-only export walk). Wrap in
          // a fresh group carrying the same Z-up -> Y-up rotation the asset
          // renders under, so the exported file matches what's on screen
          // instead of coming out on its side.
          const clone = assetRoot.clone(true);
          const bad = findForbiddenExportNode(clone as unknown as ExportNodeLike);
          if (bad) throw new Error(`export subtree contains a non-asset node: ${bad}`);
          const wrapper = new THREE.Group();
          wrapper.rotation.set(-Math.PI / 2, 0, 0);
          wrapper.add(clone);
          const exporter = new GLTFExporter();
          exporter.parse(
            wrapper,
            (result) => {
              const blob = new Blob([result as ArrayBuffer], { type: "model/gltf-binary" });
              const url = URL.createObjectURL(blob);
              const a = document.createElement("a");
              a.href = url;
              a.download = filename;
              a.click();
              URL.revokeObjectURL(url);
            },
            (err) => {
              console.error("GLB export failed", err);
              onExportError("Could not export .glb — see console for details.");
            },
            { binary: true },
          );
        } catch (err) {
          console.error("GLB export failed", err);
          onExportError("Could not export .glb — see console for details.");
        }
      },
    };
    apiRef.current = api;
    if (!homedRef.current) {
      homedRef.current = true;
      api.home();
    }
  }, [controls, camera, gl, apiRef, focusRef, homeHeightRef, assetGroupRef, onExportError]);

  useFrame((state, dt) => {
    if (!controls) return;

    // report which focus point is active (drives tour joint highlighting)
    const head = focusRef.current?.queue[0] ?? null;
    if (head !== lastHeadRef.current) {
      lastHeadRef.current = head;
      onFocusChange(head);
    }

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
      <Html position={[0, HUMAN_HEIGHT + 0.25, 0]} center zIndexRange={LABEL_Z_RANGE}>
        <div className="dim-label dim-label--muted">6 ft</div>
      </Html>
    </group>
  );
}

/** A fixed real-world streetscape the asset stands on — a concrete sidewalk
 * (top face at y=0, where the asset and the 6 ft figure stand), a ~6 in curb,
 * an asphalt road with a dashed centerline, and a grass verge. Purely a scale
 * reference like the silhouette: authored in Three's Y-up world, rendered
 * OUTSIDE the asset group, so it never enters the .glb export. Real-world
 * dimensions are the whole point — a hairline pole reads as obviously wrong
 * next to an 8 ft walk and a 6 in curb. */
function SceneContext() {
  return (
    <group>
      {/* sidewalk slab (~8 ft deep) — its top face is the ground plane (y=0) */}
      <mesh position={[0, -0.06, 0.2]} receiveShadow>
        <boxGeometry args={[16, 0.12, 2.4]} />
        <meshStandardMaterial color="#b7b5ab" roughness={0.96} />
      </mesh>
      {/* raised curb (~6 in) along the road edge */}
      <mesh position={[0, 0.01, -1.0]} receiveShadow castShadow>
        <boxGeometry args={[16, 0.3, 0.16]} />
        <meshStandardMaterial color="#c9c7be" roughness={0.9} />
      </mesh>
      {/* asphalt roadway, set below the sidewalk */}
      <mesh position={[0, -0.13, -4.5]} receiveShadow>
        <boxGeometry args={[16, 0.12, 7]} />
        <meshStandardMaterial color="#37373b" roughness={1} />
      </mesh>
      {/* dashed yellow centerline */}
      {[-6, -4, -2, 0, 2, 4, 6].map((x) => (
        <mesh key={x} position={[x, -0.063, -4.5]}>
          <boxGeometry args={[1.1, 0.012, 0.16]} />
          <meshStandardMaterial color="#e6bd35" roughness={0.7} />
        </mesh>
      ))}
      {/* grass verge on the building side */}
      <mesh position={[0, -0.06, 2.2]} receiveShadow>
        <boxGeometry args={[16, 0.1, 1.6]} />
        <meshStandardMaterial color="#6d9350" roughness={1} />
      </mesh>
    </group>
  );
}

function VerticalDim({ x, height, label }: { x: number; height: number; label: string }) {
  return (
    <group position={[x, 0, 0]}>
      <Line points={[[0, 0, 0], [0, height, 0]]} color="#e11d48" lineWidth={1.5} dashed dashSize={0.15} gapSize={0.1} />
      <Line points={[[-0.2, 0, 0], [0.2, 0, 0]]} color="#e11d48" lineWidth={1.5} />
      <Line points={[[-0.2, height, 0], [0.2, height, 0]]} color="#e11d48" lineWidth={1.5} />
      <Html position={[0, height / 2, 0]} center zIndexRange={LABEL_Z_RANGE}>
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
      <Html position={[length / 2, 0.35, 0]} center zIndexRange={LABEL_Z_RANGE}>
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
  onDuplicate,
  onDelete,
  onCommitTransform,
  onResetEdits,
  hasEdits,
  flash,
  banner = null,
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
  /** Duplicate the selected component group. */
  onDuplicate: (component: string) => void;
  /** Delete the current selection (a whole component or a single part). */
  onDelete: (sel: Selection) => void;
  /** Bake a gizmo transform back into the spec for a component. */
  onCommitTransform: (key: string, t: CommittedTransform) => void;
  /** Clear every manual move/rotate/scale/delete/duplicate edit. */
  onResetEdits: () => void;
  /** Whether any manual edits exist (enables the reset button). */
  hasEdits: boolean;
  /** Component names (or 'joint:N') to highlight — the connection check
   * uses this for hovered findings and the fix preview. */
  flash?: Set<string>;
  /** Status banner over the viewport (e.g. "previewing proposed fixes"). */
  banner?: string | null;
}) {
  const colors = THEME_COLORS[theme];
  const p = specParams(spec);
  const measured = Math.max(
    HUMAN_HEIGHT * 0.25,
    ...primitives
      .filter((prim) => !prim.cut)
      .map((prim) => {
        const box = aabb(prim);
        return box.center[2] + box.half[2];
      }),
  );
  const heightM = p.pole_height ?? p.height ?? measured;
  const armM = p.arm_length ?? 0;
  const imperial = displayUnits === "imperial";

  const focusRef = useRef<FocusState | null>(null);
  const apiRef = useRef<ViewportApi | null>(null);
  const compassRef = useRef<HTMLDivElement>(null);
  const sunNeedleRef = useRef<HTMLDivElement>(null);
  // Wraps only the rendered asset primitives (+ the live edit-gizmo proxy,
  // when a transform tool is dragging) — never lights, grid/ground,
  // silhouette, dimension lines, or the gizmo widget itself. This is the
  // subtree the .glb download exports.
  const assetGroupRef = useRef<THREE.Group>(null);
  const [exportNotice, setExportNotice] = useState<string | null>(null);
  useEffect(() => {
    if (!exportNotice) return;
    const t = window.setTimeout(() => setExportNotice(null), 4000);
    return () => window.clearTimeout(t);
  }, [exportNotice]);
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
  const [tourJoint, setTourJoint] = useState<number | null>(null);
  const [lightsOn, setLightsOn] = useState(false);
  const [wireframe, setWireframe] = useState(false);
  const [exploded, setExploded] = useState(false);
  const [scene, setScene] = useState(false);
  const [tool, setTool] = useState<EditMode | null>(null);
  // the proxy group the transform gizmo drives (lives in the Z-up group, so
  // its local transform is authored coords); the gizmo widget itself renders
  // at scene root so the Z-up rotation isn't applied to it twice
  const [gizmoTarget, setGizmoTarget] = useState<THREE.Group | null>(null);

  // edit target (only when a tool is active): the drilled-down PART when one
  // is selected — so rotate/stretch act on just that part — else the group
  const editComponent = tool && selected ? selected.component : null;
  const editPart = tool && selected?.part ? selected.part : null;
  // pre-edit geometry + pivot for that target; the gizmo renders these and
  // applies the live transform, then bakes it back into the spec
  const editData = useMemo(() => {
    if (!editComponent) return null;
    const pre = preEditPrimitives(spec).filter(
      (p) => p.component === editComponent && (!editPart || p.name === editPart),
    );
    return pre.length ? { prims: pre, pivot: componentPivot(pre) } : null;
  }, [editComponent, editPart, spec]);
  const editKey = editComponent ? (editPart ? `${editComponent}/${editPart}` : editComponent) : "";
  const curOffset = (spec.offsets?.[editKey] ?? [0, 0, 0]) as Vec3;
  const curRotation = (spec.edits?.rotations?.[editKey] ?? [0, 0, 0]) as Vec3;
  const curScale = (spec.edits?.scales?.[editKey] ?? [1, 1, 1]) as Vec3;

  // read the dragged proxy transform back into the spec (moves → offsets,
  // rotate/scale → edits). The proxy is a child of the Z-up group, so its
  // local position/rotation/scale are already in authored coordinates.
  const commitGizmo = () => {
    const g = gizmoTarget;
    if (!g || !editKey || !editData) return;
    const p = editData.pivot;
    // spec rotations are Blender-XYZ Eulers = Three's 'ZYX' order
    const e = new THREE.Euler().setFromQuaternion(g.quaternion, "ZYX");
    onCommitTransform(editKey, {
      offset: [g.position.x - p[0], g.position.y - p[1], g.position.z - p[2]],
      rotation: [e.x, e.y, e.z],
      scale: [g.scale.x, g.scale.y, g.scale.z],
    });
  };

  // fixtures that emit light (lens parts / anything with material emission)
  const emitters = lightsOn ? findEmitters(primitives, spec) : [];

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
      .map(([n, pr]) => ({
        target: zUpToYUp(pr.location),
        distance: 0.85,
        dwell: 1.0,
        joint: n,
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
        gl={{ preserveDrawingBuffer: true }}
        camera={{ position: [heightM * 1.2, heightM * 0.9, heightM * 1.6], fov: 45 }}
        onPointerMissed={() => onSelect(null)}
      >
        <color attach="background" args={[lightsOn ? "#0a0d14" : colors.bg]} />
        <StudioEnvironment dim={lightsOn} />
        {/* night: dim ambient + faint moonlight; day: sun */}
        <ambientLight intensity={lightsOn ? 0.06 : 0.35} />
        <directionalLight position={sunPos} intensity={lightsOn ? 0.08 : 1.2} castShadow />

        {/* the asset's own fixtures, lit at night */}
        {emitters.map((prim) => {
          const prof = lightProfile(spec.asset_type, resolveMaterial(spec, prim.materialSlot).emission);
          return (
            <pointLight
              key={`light-${prim.name}`}
              position={zUpToYUp(prim.location)}
              color={prof.color}
              intensity={prof.intensity}
              distance={prof.distance}
              decay={2}
              castShadow
            />
          );
        })}

        {/* ground: the streetscape scale-context when toggled on, else the
            reference grid + shadow catcher. The scene meshes receive shadow
            themselves, so the asset still casts onto the sidewalk. */}
        {scene ? (
          <SceneContext />
        ) : (
          <>
            {/* grid: 1 ft / 5 ft cells in imperial, 0.5 m / 5 m in metric */}
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
          </>
        )}

        {/* asset is authored Z-up; rotate into Three's Y-up world */}
        <group rotation={[-Math.PI / 2, 0, 0]}>
          {/* asset-only subtree: what the .glb export reads (see
              assetGroupRef above) — deliberately excludes lights, grid,
              ground, silhouette, dims, and the gizmo widget, all of which
              render as siblings outside this group. */}
          <group ref={assetGroupRef}>
            <AssetMesh
              primitives={primitives}
              spec={spec}
              selected={selected}
              onSelect={onSelect}
              tourJoint={tourJoint}
              wireframe={wireframe}
              explode={exploded}
              lightsOn={lightsOn}
              editingKey={editData ? editKey : null}
              flash={flash}
            />
            {editData && editKey && (
              <group
                key={`${editKey}-${tool}`}
                ref={setGizmoTarget}
                position={[
                  editData.pivot[0] + curOffset[0],
                  editData.pivot[1] + curOffset[1],
                  editData.pivot[2] + curOffset[2],
                ]}
                rotation={new THREE.Euler(curRotation[0], curRotation[1], curRotation[2], "ZYX")}
                scale={curScale}
              >
                {editData.prims.map((prim) => (
                  <PrimitiveMesh
                    key={`${prim.component}/${prim.name}`}
                    prim={prim}
                    spec={spec}
                    selected={{ component: prim.component }}
                    onSelect={() => {}}
                    tourJoint={null}
                    wireframe={wireframe}
                    lightsOn={lightsOn}
                    explodeOffset={[-editData.pivot[0], -editData.pivot[1], -editData.pivot[2]]}
                  />
                ))}
              </group>
            )}
          </group>
        </group>

        {/* gizmo widget at scene root (outside the Z-up group) so its axes
            aren't rotated twice; it still tracks the proxy's world transform */}
        {editData && editKey && gizmoTarget && (
          <TransformControls
            object={gizmoTarget}
            mode={tool!}
            size={0.9}
            onMouseUp={commitGizmo}
          />
        )}

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
          assetGroupRef={assetGroupRef}
          onFocusChange={(fp) => setTourJoint(fp?.joint ?? null)}
          onExportError={setExportNotice}
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
        <button className="nav-btn" onClick={() => apiRef.current?.frontView()} title="Front view">
          ▥
        </button>
        <button className="nav-btn" onClick={() => apiRef.current?.sideView()} title="Side view">
          ◫
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
        <button
          className={`nav-btn${lightsOn ? " nav-btn--active" : ""}`}
          onClick={() => setLightsOn((v) => !v)}
          title={lightsOn ? "Night / lights on — click for day" : "Night — turn the sun off and the lights on"}
        >
          {lightsOn ? "🌙" : "☀"}
        </button>
        <button
          className={`nav-btn${wireframe ? " nav-btn--active" : ""}`}
          onClick={() => setWireframe((v) => !v)}
          title="Wireframe — see through to the structure"
        >
          ◧
        </button>
        <button
          className={`nav-btn${exploded ? " nav-btn--active" : ""}`}
          onClick={() => setExploded((v) => !v)}
          title="Exploded view — separate the components"
        >
          ✱
        </button>
        <button
          className={`nav-btn${scene ? " nav-btn--active" : ""}`}
          onClick={() => setScene((v) => !v)}
          title="Streetscape — set the asset on a sidewalk & curb to judge real-world scale"
        >
          🛣
        </button>
        <button
          className="nav-btn"
          onClick={() => apiRef.current?.screenshot()}
          title="Screenshot (download PNG)"
        >
          📷
        </button>
        <button
          className="nav-btn"
          onClick={() => {
            try {
              apiRef.current?.exportGlb(sanitizeExportFilename(spec.name || spec.asset_type));
            } catch (err) {
              console.error("GLB export failed", err);
              setExportNotice("Could not export .glb — see console for details.");
            }
          }}
          title="Download 3D model (.glb)"
        >
          ⬇
        </button>
      </div>

      {/* SketchUp-style edit tools: right side, top-down column */}
      <div className="edit-col">
        <span className="edit-col__label">Edit</span>
        <button
          className={`nav-btn${tool === "translate" ? " nav-btn--active" : ""}`}
          disabled={!selected}
          onClick={() => setTool((t) => (t === "translate" ? null : "translate"))}
          title="Move — drag the selected object around in 3D"
        >
          ✥
        </button>
        <button
          className={`nav-btn${tool === "rotate" ? " nav-btn--active" : ""}`}
          disabled={!selected}
          onClick={() => setTool((t) => (t === "rotate" ? null : "rotate"))}
          title="Rotate — spin the selected object"
        >
          ⟳
        </button>
        <button
          className={`nav-btn${tool === "scale" ? " nav-btn--active" : ""}`}
          disabled={!selected}
          onClick={() => setTool((t) => (t === "scale" ? null : "scale"))}
          title="Stretch / scale — resize the selected object"
        >
          ⤢
        </button>
        <button
          className="nav-btn"
          disabled={!selected}
          onClick={() => selected && onDuplicate(selected.component)}
          title="Duplicate the selected object"
        >
          ⧉
        </button>
        <button
          className="nav-btn nav-btn--danger"
          disabled={!selected}
          onClick={() => {
            if (selected) {
              onDelete(selected);
              setTool(null);
            }
          }}
          title="Delete the selected object"
        >
          🗑
        </button>
        <button
          className="nav-btn"
          disabled={!hasEdits}
          onClick={() => {
            onResetEdits();
            setTool(null);
          }}
          title="Reset all manual move / rotate / stretch / delete / duplicate edits"
        >
          ↺
        </button>
      </div>

      <div className="nav-hint">
        {tool && selected
          ? `${tool === "translate" ? "Move" : tool === "rotate" ? "Rotate" : "Stretch"} — drag the gizmo · click empty space to finish`
          : selected
            ? "Edit tools ▸ move · rotate · stretch · duplicate · delete"
            : "click a part to select · drag orbit · WASD move · Q/E down/up"}
      </div>
      {banner && <div className="tour-hint tour-hint--check">{banner}</div>}
      {exportNotice && <div className="tour-hint tour-hint--error">{exportNotice}</div>}
      {touring && <div className="tour-hint">🔩 Touring connection points…</div>}
      {scene && !touring && (
        <div className="tour-hint">🛣 Streetscape scale — 6 ft figure · ~6 in curb · 8 ft walk</div>
      )}
      {lightsOn && emitters.length > 0 && (
        <div className="tour-hint tour-hint--night">
          🌙 {emitters.length} fixture{emitters.length > 1 ? "s" : ""} lit ·{" "}
          {lightProfile(spec.asset_type, 4).usage} ~{lightProfile(spec.asset_type, 4).targetLux} lux
          <span className="night-note"> (approx. design target)</span>
        </div>
      )}
      {lightsOn && emitters.length === 0 && (
        <div className="tour-hint tour-hint--night">🌙 Night — this asset has no light fixtures</div>
      )}
    </div>
  );
}
