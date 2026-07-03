/** 1:1 port of blender/builders/street_light.py (pure primitive layer).
 * Any geometry change there MUST be mirrored here — dimensional parity
 * between preview and final export is a hard requirement (T4.3). */
import type { AssetSpec, Primitive } from "../types";
import { mirrorX, register, specParams, specToggles } from "./base";

const ARM_SEGMENTS = 6;
const ARM_RADIUS = 0.035;
const FT = 0.3048;
const IN = 0.0254;

const DEFAULTS_M = {
  pole_height: 30 * FT,
  arm_length: 8 * FT,
  pole_base_diameter: 8 * IN,
  pole_top_diameter: 4 * IN,
};

function armPoints(armLength: number, attachZ: number, rise: number) {
  const pts: Array<[number, number]> = [];
  for (let i = 0; i <= ARM_SEGMENTS; i++) {
    const t = i / ARM_SEGMENTS;
    pts.push([t * armLength, attachZ + rise * t * t]);
  }
  return pts;
}

function armPrimitives(armLength: number, poleHeight: number): Primitive[] {
  const rise = Math.min(0.15 * armLength, 0.75);
  const attachZ = poleHeight - 0.25 - rise;
  const pts = armPoints(armLength, attachZ, rise);
  const prims: Primitive[] = [];
  for (let i = 1; i <= ARM_SEGMENTS; i++) {
    const [x0, z0] = pts[i - 1];
    const [x1, z1] = pts[i];
    const dx = x1 - x0;
    const dz = z1 - z0;
    prims.push({
      kind: "cylinder",
      name: `arm_seg_${i}`,
      component: "arm",
      location: [(x0 + x1) / 2, 0, (z0 + z1) / 2],
      rotation: [0, Math.atan2(dx, dz), 0],
      materialSlot: "pole",
      params: { radius: ARM_RADIUS, depth: Math.hypot(dx, dz) * 1.08 },
    });
  }
  return prims;
}

function luminairePrimitives(armLength: number, poleHeight: number): Primitive[] {
  const tipZ = poleHeight - 0.25;
  const headLen = 0.75;
  const headW = 0.32;
  const headH = 0.17;
  const headX = armLength + headLen / 2 - 0.15;
  return [
    {
      kind: "box",
      name: "head",
      component: "luminaire",
      location: [headX, 0, tipZ + headH / 2 - 0.02],
      rotation: [0, 0, 0],
      materialSlot: "luminaire",
      params: { size: [headLen, headW, headH] },
    },
    {
      kind: "cylinder",
      name: "lens",
      component: "luminaire",
      location: [headX + 0.1, 0, tipZ - 0.03],
      rotation: [0, 0, 0],
      materialSlot: "lens",
      params: { radius: 0.1, depth: 0.02 },
    },
  ];
}

function computeStreetLight(spec: AssetSpec): Primitive[] {
  const p = specParams(spec);
  const toggles = specToggles(spec);

  const poleHeight = p.pole_height ?? DEFAULTS_M.pole_height;
  const armLength = p.arm_length ?? DEFAULTS_M.arm_length;
  const baseR = (p.pole_base_diameter ?? DEFAULTS_M.pole_base_diameter) / 2;
  const topR = (p.pole_top_diameter ?? DEFAULTS_M.pole_top_diameter) / 2;

  const prims: Primitive[] = [];

  // base plate + anchor bolts
  const plate = Math.max(0.45, baseR * 4);
  prims.push({
    kind: "box",
    name: "plate",
    component: "base_plate",
    location: [0, 0, 0.016],
    rotation: [0, 0, 0],
    materialSlot: "base",
    params: { size: [plate, plate, 0.032] },
  });
  if (toggles.anchor_bolts ?? true) {
    const offset = plate / 2 - 0.05;
    const corners: Array<[number, number]> = [[1, 1], [1, -1], [-1, 1], [-1, -1]];
    corners.forEach(([sx, sy], i) => {
      prims.push({
        kind: "cylinder",
        name: `anchor_bolt_${i + 1}`,
        component: "base_plate",
        location: [sx * offset, sy * offset, 0.05],
        rotation: [0, 0, 0],
        materialSlot: "base",
        params: { radius: 0.014, depth: 0.1 },
      });
    });
  }

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
    kind: "sphere",
    name: "cap",
    component: "pole",
    location: [0, 0, poleHeight],
    rotation: [0, 0, 0],
    materialSlot: "pole",
    params: { radius: topR * 1.15 },
  });

  // mast arm + luminaire (mirrored when double_arm is on)
  const armSide = [
    ...armPrimitives(armLength, poleHeight),
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
