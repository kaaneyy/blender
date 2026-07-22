/** Mirror of blender/builders/base.py — registry, spec helpers, materials.
 * Only the pure primitive layer is mirrored; realization is Three.js. */
import type { AssetSpec, Primitive, Unit } from "../types";
import { convert, isLengthUnit } from "../units";
import { applyStructure, applyTransforms } from "./edits";

export const MATERIAL_PRESETS: Record<
  string,
  { color: string; metalness: number; roughness: number }
> = {
  // parity with blender/builders/base.py — the viewport has a real
  // environment map (RoomEnvironment), so full metals shade correctly
  galvanized_steel: { color: "#9ea3a6", metalness: 1.0, roughness: 0.45 },
  powder_coat_black: { color: "#1e2022", metalness: 0.2, roughness: 0.5 },
  powder_coat_green: { color: "#28513a", metalness: 0.2, roughness: 0.5 },
  cast_iron: { color: "#35363a", metalness: 0.9, roughness: 0.75 },
  concrete: { color: "#c0bcb4", metalness: 0.0, roughness: 0.9 },
  brushed_aluminum: { color: "#d7d9db", metalness: 1.0, roughness: 0.35 },
  wood_slat: { color: "#96652f", metalness: 0.0, roughness: 0.65 },
  lamp_lens: { color: "#f7ecc3", metalness: 0.0, roughness: 0.15 },
};

type BuilderFn = (spec: AssetSpec) => Primitive[];
const BUILDERS: Record<string, BuilderFn> = {};

export function register(assetType: string, fn: BuilderFn): void {
  BUILDERS[assetType] = fn;
}

/** Build the curated/custom primitives, before hardware and edit overlays.
 * PRIMITIVES ALWAYS WIN: a non-empty spec.primitives array is built even
 * when spec.asset_type also matches a registered curated builder — a
 * curated builder's geometry can't express a styled/extended request (e.g.
 * a "victorian post with lantern" typed as asset_type street_light), so a
 * spec that already modeled its own geometry must never have that geometry
 * silently discarded in favor of the curated stand-in. Only when the spec
 * has no primitives do we fall back to the registered curated builder for
 * spec.asset_type, and only when neither is available do we throw. Mirror
 * of base.py compute_primitives dispatch order. */
function buildBase(spec: AssetSpec): Primitive[] {
  if (spec.primitives?.length) {
    // lazy import avoided: generic.ts imports helpers from this module, so
    // the dependency is wired in builders/index.ts instead
    return customBuilder!(spec);
  }
  const fn = BUILDERS[spec.asset_type];
  if (fn) return fn(spec);
  throw new Error(
    `No builder for asset_type "${spec.asset_type}" and the spec has no ` +
      `primitives (curated: ${Object.keys(BUILDERS).join(", ") || "<none>"})`,
  );
}

/** Primitives with the structural overlay (duplicate/delete) applied and
 * connection hardware generated, but NOT the move/rotate/scale transform —
 * the viewport gizmo renders these and applies the transform live, then
 * bakes it back into the spec. The structural overlay runs BEFORE hardware
 * so duplicated components get their own joints and deleted parts don't
 * attract bolts. Hardware is generated from the TRANSFORMED members, so
 * joints land where parts actually are after user edits (a moved component
 * takes its bolts with it) — the generated hardware itself stays
 * pre-transform here so the gizmo can drive the 'hardware' group like any
 * other component. Mirror of base.py compute_primitives ordering. */
export function preEditPrimitives(spec: AssetSpec): Primitive[] {
  const base = applyStructure(buildBase(spec), spec);
  if (specToggles(spec).connection_hardware && hardwareFn) {
    return base.concat(hardwareFn(applyTransforms(base, spec), spec));
  }
  return base;
}

export function computePrimitives(spec: AssetSpec): Primitive[] {
  // structural overlay (duplicate/delete) then user transforms (move/rotate/
  // scale), baked so bounds, hardware, and the Blender export all agree
  return applyTransforms(preEditPrimitives(spec), spec);
}

/** Wired by builders/index.ts (keeps this module dependency-free). */
type HardwareFn = (prims: Primitive[], spec: AssetSpec) => Primitive[];
let hardwareFn: HardwareFn | null = null;
export function setHardwareBuilder(fn: HardwareFn): void {
  hardwareFn = fn;
}

/** Set by builders/index.ts to avoid a circular import with generic.ts. */
export let customBuilder: BuilderFn | null = null;
export function setCustomBuilder(fn: BuilderFn): void {
  customBuilder = fn;
}

export interface ResolvedMaterial {
  color: string;
  metalness: number;
  roughness: number;
  uvScale: number;
  emission: number;
  weathering: number;
}

//: grime tint weathering lerps toward (mirror of base.py GRIME_COLOR).
const GRIME = [0.16, 0.14, 0.12];
const FINISH_ROUGHNESS: Record<string, number> = {
  cast: 0.72, machined: 0.35, sheet: 0.28, rough: 0.85,
};

/** Resolved preset name for a slot (mirror of base.py material_preset_name).
 * Shared by resolveMaterial and the hardware appropriateness check. */
export function materialPresetName(spec: AssetSpec, slot: string): string {
  const entry = (spec.materials ?? []).find((m) => m.slot === slot);
  const fallbacks: Record<string, string> = { lens: "lamp_lens", hardware: "brushed_aluminum" };
  return entry?.preset ?? fallbacks[slot] ?? "galvanized_steel";
}

/** Preset merged with per-slot overrides — mirror of base.py resolve_material.
 * Returns AUTHORED values; call weatheredShading() for the aged appearance. */
export function resolveMaterial(spec: AssetSpec, slot: string): ResolvedMaterial {
  const entry = (spec.materials ?? []).find((m) => m.slot === slot);
  const presetName = materialPresetName(spec, slot);
  const preset = MATERIAL_PRESETS[presetName] ?? MATERIAL_PRESETS.galvanized_steel;
  const finishRough =
    entry?.finish && entry.finish in FINISH_ROUGHNESS ? FINISH_ROUGHNESS[entry.finish] : undefined;
  return {
    color: entry?.color ?? preset.color,
    metalness: entry?.metalness ?? preset.metalness,
    roughness: entry?.roughness ?? finishRough ?? preset.roughness,
    uvScale: entry?.uv_scale ?? 1,
    emission: entry?.emission ?? 0,
    weathering: entry?.weathering ?? 0,
  };
}

function hexToRgb(hex: string): [number, number, number] {
  const c = hex.replace("#", "");
  return [
    parseInt(c.slice(0, 2), 16) / 255,
    parseInt(c.slice(2, 4), 16) / 255,
    parseInt(c.slice(4, 6), 16) / 255,
  ];
}

function rgbToHex(rgb: number[]): string {
  return (
    "#" +
    rgb
      .map((v) => Math.max(0, Math.min(255, Math.round(v * 255))).toString(16).padStart(2, "0"))
      .join("")
  );
}

export interface ShadedMaterial {
  color: string;
  metalness: number;
  roughness: number;
  emission: number;
}

/** Apply the weathering aging model — mirror of base.py weathered(). */
export function weatheredShading(m: ResolvedMaterial): ShadedMaterial {
  const w = Math.max(0, Math.min(1, m.weathering));
  if (w <= 0) {
    return { color: m.color, metalness: m.metalness, roughness: m.roughness, emission: m.emission };
  }
  const base = hexToRgb(m.color);
  const mix = 0.5 * w;
  const color = rgbToHex(base.map((c, i) => c * (1 - mix) + GRIME[i] * mix));
  return {
    color,
    metalness: m.metalness * (1 - 0.4 * w),
    roughness: Math.min(1, m.roughness + 0.45 * w),
    emission: m.emission,
  };
}

/** Numeric parameter values keyed by id. Length-unit values (ft/in/m/cm/mm)
 * convert to meters; dimensionless units (deg/W/x) and unit-less parameters
 * (angles in degrees, counts, ratios) pass through unchanged so expressions
 * can use them directly (mirror of base.py spec_params). */
export function specParams(spec: AssetSpec): Record<string, number> {
  const out: Record<string, number> = {};
  for (const p of spec.parameters) {
    if (typeof p.value === "number") {
      out[p.id] = isLengthUnit(p.unit) ? convert(p.value, p.unit, "m") : p.value;
    }
  }
  return out;
}

export function specToggles(spec: AssetSpec): Record<string, boolean> {
  const out: Record<string, boolean> = {};
  for (const t of spec.toggles ?? []) out[t.id] = t.value;
  return out;
}

/** String-valued (type=select) parameters keyed by id (mirror of base.py). */
export function specSelects(spec: AssetSpec): Record<string, string> {
  const out: Record<string, string> = {};
  for (const p of spec.parameters) {
    if (typeof p.value === "string") out[p.id] = p.value;
  }
  return out;
}

/** Mirror primitives across the YZ plane (same math as base.py mirror_x). */
export function mirrorX(prims: Primitive[], suffix = "_b"): Primitive[] {
  return prims.map((p) => {
    const params = { ...p.params };
    if (params.path) {
      // sweep paths carry their own coordinates
      params.path = params.path.map(([x, y, z]) => [-x, y, z] as Primitive["location"]);
    }
    return {
      ...p,
      name: p.name + suffix,
      location: [-p.location[0], p.location[1], p.location[2]] as Primitive["location"],
      rotation: [
        p.rotation[0],
        -p.rotation[1],
        p.rotation[2] === 0 ? 0 : Math.PI - p.rotation[2],
      ] as Primitive["rotation"],
      params,
    };
  });
}
