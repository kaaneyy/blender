/** Material takeoff: what the thing actually weighs.
 *
 * A fabrication drawing states a weight ("APPROXIMATE WEIGHT: 34.6 LB")
 * because it drives shipping, handling, and anchorage. This computes that
 * from the built primitives: real per-kind volume (hollow where the part is
 * hollow) times the material density implied by each part's material slot.
 *
 * Deliberately approximate, exactly like the drawings it mirrors:
 * bevels/welds/fillets are ignored (finish, not mass); drilled holes
 * (`cut`) are NOT subtracted — they remove a fraction of a percent and the
 * cutters are authored over-long so they punch cleanly; swept and lofted
 * members use the standard prismatic approximations below.
 *
 * MIRROR of `blender/builders/takeoff.py` — keep the two in lockstep.
 */
import type { AssetSpec, Primitive } from "../types";
import { resolveProfile } from "../shapes";
import { materialPresetName } from "./base";

/** kg per cubic metre, by material family. */
export const DENSITY_KG_M3: Record<string, number> = {
  metal: 7850,
  concrete: 2400,
  wood: 600,
  plastic: 1200,
  glass: 2500,
  other: 1000,
};

export const KG_PER_LB = 0.45359237;

const FAMILY_BY_PRESET: Record<string, string> = {
  galvanized_steel: "metal",
  cast_iron: "metal",
  brushed_aluminum: "metal",
  powder_coat_black: "metal",
  powder_coat_green: "metal",
  stainless: "metal",
  concrete: "concrete",
  wood_slat: "wood",
  lamp_lens: "glass",
  glass: "glass",
  plastic: "plastic",
};

/** Density family for a material preset name. Unknown reads as metal — the
 * conservative (heavier) assumption for street furniture. */
export function materialFamily(preset: string | null | undefined): string {
  if (!preset) return "metal";
  if (FAMILY_BY_PRESET[preset]) return FAMILY_BY_PRESET[preset];
  for (const [token, family] of [
    ["wood", "wood"],
    ["oak", "wood"],
    ["teak", "wood"],
    ["cedar", "wood"],
    ["timber", "wood"],
    ["ipe", "wood"],
    ["concrete", "concrete"],
    ["glass", "glass"],
    ["lens", "glass"],
    ["plastic", "plastic"],
  ] as const) {
    if (preset.includes(token)) return family;
  }
  return "metal";
}

function pathLength(path: number[][]): number {
  let total = 0;
  for (let i = 1; i < path.length; i++) {
    const [ax, ay, az] = path[i - 1];
    const [bx, by, bz] = path[i];
    total += Math.hypot(bx - ax, by - ay, bz - az);
  }
  return total;
}

function profileArea(profile: { shape?: string; w?: number; h?: number }): number {
  const w = profile.w ?? 0;
  const h = profile.h ?? 0;
  return profile.shape === "rect" ? w * h : (Math.PI / 4) * w * h;
}

/** Disc method over the resolved profile: a solid of revolution is the sum
 * of pi*r^2*dz over its (r, z) samples. `shrink` pulls each radius in (the
 * hollow core of a shelled part). */
function latheVolume(params: Primitive["params"], shrink = 0): number {
  const pts = resolveProfile(params.profile!, params.radius, params.depth);
  let total = 0;
  for (let i = 1; i < pts.length; i++) {
    const [r0, z0] = pts[i - 1];
    const [r1, z1] = pts[i];
    const dz = Math.abs(z1 - z0);
    if (dz <= 0) continue;
    const a = Math.max(0, r0 - shrink);
    const b = Math.max(0, r1 - shrink);
    total += (Math.PI * dz * (a * a + a * b + b * b)) / 3;
  }
  return total;
}

const shrink = (value: number, by: number) => Math.max(0, value - by);

/** Volume of the hollow core left by solidifying `prim` inward by `shell` —
 * the same solid with every dimension pulled in. Computed with the SAME
 * formulas rather than a blanket ratio: the right ratio differs per kind. */
function coreVolume(prim: Primitive, shell: number): number {
  const p = prim.params;
  switch (prim.kind) {
    case "box": {
      const [sx, sy, sz] = p.size!;
      return shrink(sx, 2 * shell) * shrink(sy, 2 * shell) * shrink(sz, 2 * shell);
    }
    case "sphere":
      return (4 / 3) * Math.PI * shrink(p.radius!, shell) ** 3;
    case "cylinder":
      return Math.PI * shrink(p.radius!, shell) ** 2 * shrink(p.depth!, 2 * shell);
    case "cone": {
      const rb = shrink(p.radius_bottom!, shell);
      const rt = shrink(p.radius_top!, shell);
      return (Math.PI * shrink(p.depth!, 2 * shell) * (rb * rb + rb * rt + rt * rt)) / 3;
    }
    case "loft": {
      const shrunk = (profile: { shape?: string; w?: number; h?: number }) => ({
        shape: profile.shape,
        w: shrink(profile.w ?? 0, 2 * shell),
        h: shrink(profile.h ?? 0, 2 * shell),
      });
      const a0 = profileArea(shrunk(p.profile_start!));
      const a1 = profileArea(shrunk(p.profile_end!));
      return ((a0 + a1) / 2) * shrink(p.depth!, 2 * shell);
    }
    case "sweep": {
      const r0 = shrink(p.radius!, shell);
      const r1 = shrink(p.radius_end ?? p.radius!, shell);
      const rAvg = (r0 + r1) / 2;
      return Math.PI * rAvg * rAvg * pathLength(p.path!);
    }
    case "lathe":
      return latheVolume(p, shell);
    default:
      return 0;
  }
}

/** Volume of one primitive in cubic metres, hollow where it is hollow. */
export function solidVolume(prim: Primitive): number {
  const p = prim.params;
  let vol = 0;
  switch (prim.kind) {
    case "box": {
      const [sx, sy, sz] = p.size!;
      vol = sx * sy * sz;
      break;
    }
    case "sphere":
      vol = (4 / 3) * Math.PI * p.radius! ** 3;
      break;
    case "cylinder":
      vol = Math.PI * p.radius! ** 2 * p.depth!;
      break;
    case "cone": {
      const rb = p.radius_bottom!;
      const rt = p.radius_top!;
      vol = (Math.PI * p.depth! * (rb * rb + rb * rt + rt * rt)) / 3;
      break;
    }
    case "tube": {
      const r = p.radius!;
      const wall = p.wall ?? 0;
      const depth = p.depth!;
      const inner = Math.max(0, r - wall);
      const square = p.section === "square";
      if (wall <= 0) {
        vol = (square ? 4 * r * r : Math.PI * r * r) * depth;
      } else if (square) {
        // square hollow section: outer 2r across flats, wall each side
        vol = depth * (4 * r * r - 4 * inner * inner);
      } else {
        vol = Math.PI * depth * (r * r - inner * inner);
      }
      break;
    }
    case "sweep": {
      const r0 = p.radius!;
      const r1 = p.radius_end ?? r0;
      const rAvg = (r0 + r1) / 2;
      vol = Math.PI * rAvg * rAvg * pathLength(p.path!);
      break;
    }
    case "loft":
      vol = ((profileArea(p.profile_start!) + profileArea(p.profile_end!)) / 2) * p.depth!;
      break;
    case "lathe":
      vol = latheVolume(p);
      break;
    default:
      return 0;
  }

  // a sheet/cast part is a shell, not a billet: keep only the skin
  const shell = p.shell;
  if (typeof shell === "number" && shell > 0 && prim.kind !== "tube") {
    vol = Math.max(0, vol - coreVolume(prim, shell));
  }
  return Math.max(0, vol);
}

export interface PartWeight {
  component: string;
  name: string;
  slot: string;
  family: string;
  volume_m3: number;
  kg: number;
  lb: number;
}

export interface Takeoff {
  parts: PartWeight[];
  by_component: Array<{ component: string; kg: number; lb: number }>;
  total_kg: number;
  total_lb: number;
}

/** Per-part, per-component and total mass for a built asset. `cut`
 * primitives are negative space and contribute nothing. */
export function computeTakeoff(primitives: Primitive[], spec?: AssetSpec): Takeoff {
  const parts: PartWeight[] = [];
  for (const prim of primitives) {
    if (prim.cut) continue;
    const volume = solidVolume(prim);
    if (volume <= 0) continue;
    const preset = spec ? materialPresetName(spec, prim.materialSlot) : null;
    const family = materialFamily(preset);
    const kg = volume * DENSITY_KG_M3[family];
    parts.push({
      component: prim.component,
      name: prim.name,
      slot: prim.materialSlot,
      family,
      volume_m3: volume,
      kg,
      lb: kg / KG_PER_LB,
    });
  }

  const byComponent = new Map<string, number>();
  for (const part of parts) {
    byComponent.set(part.component, (byComponent.get(part.component) ?? 0) + part.kg);
  }
  const total_kg = parts.reduce((sum, p) => sum + p.kg, 0);
  return {
    parts,
    by_component: [...byComponent.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([component, kg]) => ({ component, kg, lb: kg / KG_PER_LB })),
    total_kg,
    total_lb: total_kg / KG_PER_LB,
  };
}

/** Shop description of a member's stock, e.g. "2.0 SQ x 0.188 wall tube" or
 * "3.5 OD x 0.125 wall pipe" — null for parts that aren't stock shapes. */
export function stockCallout(prim: Primitive, imperial = true): string | null {
  if (prim.kind !== "tube") return null;
  const p = prim.params;
  let wall = p.wall ?? 0;
  let across = 2 * p.radius!;
  let unit = "";
  if (imperial) {
    across /= 0.0254;
    wall /= 0.0254;
  } else {
    across *= 1000;
    wall *= 1000;
    unit = "mm ";
  }
  const fmt = (v: number) => Number(v.toPrecision(3)).toString();
  return p.section === "square"
    ? `${fmt(across)} ${unit}SQ x ${fmt(wall)} ${unit}wall tube`
    : `${fmt(across)} ${unit}OD x ${fmt(wall)} ${unit}wall pipe`;
}
