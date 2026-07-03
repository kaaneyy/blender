/** Mirror of blender/builders/base.py — registry, spec helpers, materials.
 * Only the pure primitive layer is mirrored; realization is Three.js. */
import type { AssetSpec, Primitive, Unit } from "../types";
import { convert } from "../units";

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

export function computePrimitives(spec: AssetSpec): Primitive[] {
  const fn = BUILDERS[spec.asset_type];
  if (fn) return fn(spec);
  if (spec.primitives?.length) {
    // lazy import avoided: generic.ts imports helpers from this module, so
    // the dependency is wired in builders/index.ts instead
    return customBuilder!(spec);
  }
  throw new Error(
    `No builder for asset_type "${spec.asset_type}" and the spec has no ` +
      `primitives (curated: ${Object.keys(BUILDERS).join(", ") || "<none>"})`,
  );
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
}

/** Preset merged with per-slot overrides — mirror of base.py resolve_material. */
export function resolveMaterial(spec: AssetSpec, slot: string): ResolvedMaterial {
  const entry = (spec.materials ?? []).find((m) => m.slot === slot);
  const presetName = entry?.preset ?? (slot === "lens" ? "lamp_lens" : "galvanized_steel");
  const preset = MATERIAL_PRESETS[presetName] ?? MATERIAL_PRESETS.galvanized_steel;
  return {
    color: entry?.color ?? preset.color,
    metalness: entry?.metalness ?? preset.metalness,
    roughness: entry?.roughness ?? preset.roughness,
    uvScale: entry?.uv_scale ?? 1,
    emission: entry?.emission ?? 0,
  };
}

/** Numeric parameter values keyed by id, converted to meters. */
export function specParams(spec: AssetSpec): Record<string, number> {
  const defaultUnit: Unit = spec.units === "imperial" ? "ft" : "m";
  const out: Record<string, number> = {};
  for (const p of spec.parameters) {
    if (typeof p.value === "number") {
      out[p.id] = convert(p.value, p.unit ?? defaultUnit, "m");
    }
  }
  return out;
}

export function specToggles(spec: AssetSpec): Record<string, boolean> {
  const out: Record<string, boolean> = {};
  for (const t of spec.toggles ?? []) out[t.id] = t.value;
  return out;
}

/** Mirror primitives across the YZ plane (same math as base.py mirror_x). */
export function mirrorX(prims: Primitive[], suffix = "_b"): Primitive[] {
  return prims.map((p) => ({
    ...p,
    name: p.name + suffix,
    location: [-p.location[0], p.location[1], p.location[2]],
    rotation: [
      p.rotation[0],
      -p.rotation[1],
      p.rotation[2] === 0 ? 0 : Math.PI - p.rotation[2],
    ],
    params: { ...p.params },
  }));
}
