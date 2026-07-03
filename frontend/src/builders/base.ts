/** Mirror of blender/builders/base.py — registry, spec helpers, materials.
 * Only the pure primitive layer is mirrored; realization is Three.js. */
import type { AssetSpec, Primitive, Unit } from "../types";
import { convert } from "../units";

export const MATERIAL_PRESETS: Record<
  string,
  { color: string; metalness: number; roughness: number }
> = {
  // metalness is softened vs the Blender presets: the preview has no
  // environment map, and full metals render near-black without one
  galvanized_steel: { color: "#9ea3a6", metalness: 0.5, roughness: 0.45 },
  powder_coat_black: { color: "#1e2022", metalness: 0.1, roughness: 0.5 },
  powder_coat_green: { color: "#28513a", metalness: 0.1, roughness: 0.5 },
  cast_iron: { color: "#35363a", metalness: 0.4, roughness: 0.75 },
  concrete: { color: "#c0bcb4", metalness: 0.0, roughness: 0.9 },
  brushed_aluminum: { color: "#d7d9db", metalness: 0.5, roughness: 0.35 },
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
  if (!fn) {
    throw new Error(
      `No preview builder for asset_type "${spec.asset_type}" ` +
        `(known: ${Object.keys(BUILDERS).join(", ") || "<none>"})`,
    );
  }
  return fn(spec);
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
