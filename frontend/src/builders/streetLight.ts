/** 1:1 port of blender/builders/street_light.py (pure primitive layer).
 * Any geometry change there MUST be mirrored here — dimensional parity
 * between preview and final export is a hard requirement (T4.3). */
import type { AssetSpec, Primitive } from "../types";
import { mirrorX, register, specParams, specSelects, specToggles } from "./base";
import { groundConnection, gussetPlate } from "./connections";

const ARM_SEGMENTS = 6;
const ARM_RADIUS = 0.035;
const FT = 0.3048;
const IN = 0.0254;

// cobra-head housing (m): length along the arm, width across it, height at
// the door end. The lens is DERIVED from these (see luminairePrimitives),
// so resizing the housing keeps the light fitting its casing.
const HEAD_LEN = 0.75;
const HEAD_W = 0.32;
const HEAD_H = 0.17;
// door-to-nose shrink factors of the lofted housing (width, height)
const NOSE_W = 0.7;
const NOSE_H = 0.6;
// where the drop lens sits along the housing (0 = door end, 1 = nose)
const LENS_STATION = 0.63;
// lens disc thickness and how far its top tucks up into the housing
const LENS_DEPTH = 0.02;
const LENS_RECESS = 0.008;

const DEFAULTS_M = {
  pole_height: 30 * FT,
  arm_length: 8 * FT,
  pole_base_diameter: 8 * IN,
  pole_top_diameter: 4 * IN,
};

// Parameter/toggle/select ids this builder's geometry actually reads
// (specParams/specToggles/specSelects above) — the EXACT vocabulary,
// nothing more. Kept in exact sync with street_light.py's CONSUMED_PARAMS /
// CONSUMED_TOGGLES / CONSUMED_SELECTS (Python side owns the
// connectivity.check_dead_controls gate that consumes these; this mirror is
// constants only, per the mirror rule — no TS equivalent of that checker).
export const CONSUMED_PARAMS = ["pole_height", "arm_length", "pole_base_diameter", "pole_top_diameter"];
export const CONSUMED_TOGGLES = ["double_arm", "banner_bracket"];
export const CONSUMED_SELECTS = { mounting: ["flange", "burial", "embedded"] };

function armPoints(armLength: number, attachZ: number, rise: number) {
  const pts: Array<[number, number]> = [];
  for (let i = 0; i <= ARM_SEGMENTS; i++) {
    const t = i / ARM_SEGMENTS;
    pts.push([t * armLength, attachZ + rise * t * t]);
  }
  return pts;
}

function armPrimitives(
  armLength: number,
  poleHeight: number,
  poleRAtAttach: number,
): Primitive[] {
  // swept tapered tube (B2) + slip-fitter collar (C2) + gusset (C4);
  // the sweep stays FIRST so hardware band-clamps the arm itself
  const rise = Math.min(0.15 * armLength, 0.75);
  const attachZ = poleHeight - 0.25 - rise;
  const path = armPoints(armLength, attachZ, rise).map(
    ([x, z]) => [x, 0, z] as [number, number, number],
  );
  return [
    {
      kind: "sweep",
      name: "mast_arm",
      component: "arm",
      location: [0, 0, 0],
      rotation: [0, 0, 0],
      materialSlot: "pole",
      params: { path, radius: ARM_RADIUS * 1.25, radius_end: ARM_RADIUS * 0.8 },
    },
    {
      kind: "tube",
      name: "slipfitter",
      component: "arm",
      location: [0, 0, attachZ + 0.02],
      rotation: [0, 0, 0],
      materialSlot: "pole",
      params: { radius: poleRAtAttach + 0.012, wall: 0.006, depth: 0.3 },
    },
    // C4: knee brace under the cantilever — top edge hugging the arm's
    // underside, tall edge buried in the pole, hypotenuse below
    ...gussetPlate(
      "arm_gusset", "arm", "pole", [0, 0], 0,
      poleRAtAttach, poleRAtAttach + 0.16, attachZ - 0.02, "top", 0.16,
    ),
  ];
}

/** Cobra-head housing + drop lens. Under the (0, pi/2, 0) roll the loft
 * profiles' `w` spans world Z (housing HEIGHT) and `h` spans world Y
 * (WIDTH). The lens is derived from the housing cross-section at its own
 * station (ring dimensions interpolate linearly along the loft), so the
 * light fits the casing by construction at any housing size. */
function luminairePrimitives(armLength: number, poleHeight: number): Primitive[] {
  const tipZ = poleHeight - 0.25;
  const headX = armLength + HEAD_LEN / 2 - 0.15;
  const headZ = tipZ + HEAD_H / 2 - 0.02; // door-end underside just below the tip
  // housing cross-section at the lens station
  const width = HEAD_W * (1 + (NOSE_W - 1) * LENS_STATION);
  const height = HEAD_H * (1 + (NOSE_H - 1) * LENS_STATION);
  const lensR = 0.38 * width; // clears the shell walls on both sides
  return [
    {
      kind: "loft",
      name: "head",
      component: "luminaire",
      location: [headX, 0, headZ],
      rotation: [0, Math.PI / 2, 0],
      materialSlot: "luminaire",
      params: {
        depth: HEAD_LEN,
        // w = height, h = width (world axes under the roll — see above)
        profile_start: { shape: "rect", w: HEAD_H, h: HEAD_W },
        profile_end: { shape: "ellipse", w: HEAD_H * NOSE_H, h: HEAD_W * NOSE_W },
        shell: 0.003,
      },
    },
    {
      kind: "cylinder",
      name: "lens",
      component: "luminaire",
      location: [
        headX + (LENS_STATION - 0.5) * HEAD_LEN,
        0,
        headZ - height / 2 + LENS_RECESS - LENS_DEPTH / 2,
      ],
      rotation: [0, 0, 0],
      materialSlot: "lens",
      params: { radius: lensR, depth: LENS_DEPTH },
    },
  ];
}

function computeStreetLight(spec: AssetSpec): Primitive[] {
  const p = specParams(spec);
  const toggles = specToggles(spec);
  const selects = specSelects(spec);

  const poleHeight = p.pole_height ?? DEFAULTS_M.pole_height;
  const armLength = p.arm_length ?? DEFAULTS_M.arm_length;
  const baseR = (p.pole_base_diameter ?? DEFAULTS_M.pole_base_diameter) / 2;
  const topR = (p.pole_top_diameter ?? DEFAULTS_M.pole_top_diameter) / 2;

  const prims: Primitive[] = [];

  // C1/C7: engineered ground connection (flange / burial / embedded)
  prims.push(
    ...groundConnection(baseR, selects.mounting ?? "flange", "standard", "base_plate", "base"),
  );

  // tapered pole
  prims.push({
    kind: "cone",
    name: "shaft",
    component: "pole",
    location: [0, 0, poleHeight / 2],
    rotation: [0, 0, 0],
    materialSlot: "pole",
    params: { radius_bottom: baseR, radius_top: topR, depth: poleHeight },
  });
  prims.push({
    kind: "lathe",
    name: "cap",
    component: "pole",
    location: [0, 0, poleHeight - 0.01],
    rotation: [0, 0, 0],
    materialSlot: "pole",
    params: { profile: "dome", radius: topR * 1.25, depth: topR * 1.6 },
  });

  // mast arm + luminaire (mirrored when double_arm is on)
  const rise = Math.min(0.15 * armLength, 0.75);
  const attachZ = poleHeight - 0.25 - rise;
  const poleRAtAttach = baseR + (topR - baseR) * Math.min(1, attachZ / poleHeight);
  const armSide = [
    ...armPrimitives(armLength, poleHeight, poleRAtAttach),
    ...luminairePrimitives(armLength, poleHeight),
  ];
  prims.push(...armSide);
  if (toggles.double_arm) prims.push(...mirrorX(armSide, "_b"));

  // banner bracket
  if (toggles.banner_bracket) {
    const bracketLen = 0.9;
    const lowerZ = Math.min(0.45 * poleHeight, 3.6);
    const brackets: Array<[string, number]> = [
      ["bracket_lower", lowerZ],
      ["bracket_upper", lowerZ + 1.5],
    ];
    for (const [name, z] of brackets) {
      prims.push({
        kind: "cylinder",
        name,
        component: "banner_bracket",
        location: [0, bracketLen / 2, z],
        rotation: [Math.PI / 2, 0, 0],
        materialSlot: "pole",
        params: { radius: 0.016, depth: bracketLen },
      });
    }
  }

  return prims;
}

register("street_light", computeStreetLight);
