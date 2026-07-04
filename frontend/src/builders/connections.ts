/** Mirror of blender/builders/connections.py — standard fabrication
 * connections (Part C). Keep in exact lockstep. */
import type { Primitive } from "../types";

const BOLT_COUNT: Record<string, number> = { light: 4, standard: 4, heavy: 6 };
const BOLT_R: Record<string, number> = { light: 0.008, standard: 0.011, heavy: 0.014 };

export function weldFillet(
  radius: number,
  size: number,
  z: number,
  component: string,
  slot: string,
  name = "weld_bead",
): Primitive {
  return {
    kind: "lathe",
    name,
    component,
    location: [0, 0, z],
    rotation: [0, 0, 0],
    materialSlot: slot,
    params: {
      profile: [
        [radius + size, 0],
        [radius + size * 0.25, size * 0.15],
        [radius, size],
      ],
    },
  };
}

export function groundConnection(
  poleRadius: number,
  mount = "flange",
  loadClass = "standard",
  component = "base_plate",
  slot = "base",
): Primitive[] {
  if (mount === "burial") {
    return [
      {
        kind: "lathe",
        name: "backfill_collar",
        component,
        location: [0, 0, 0],
        rotation: [0, 0, 0],
        materialSlot: slot,
        params: {
          profile: "flared_base",
          radius: poleRadius * 2.0,
          depth: Math.max(0.1, poleRadius * 1.4),
        },
      },
    ];
  }
  if (mount === "embedded") {
    const pierR = poleRadius * 2.6;
    const pierH = Math.max(0.15, poleRadius * 2.0);
    return [
      {
        kind: "cylinder",
        name: "concrete_pier",
        component,
        location: [0, 0, pierH / 2],
        rotation: [0, 0, 0],
        materialSlot: slot,
        params: { radius: pierR, depth: pierH },
      },
      weldFillet(poleRadius, poleRadius * 0.35, pierH, component, slot, "grout_ring"),
    ];
  }

  // flange
  const nBolts = BOLT_COUNT[loadClass] ?? 4;
  const boltR = BOLT_R[loadClass] ?? 0.011;
  const flangeR = Math.max(poleRadius * 2.1, poleRadius + 0.09);
  const flangeT = 0.028;
  const groutT = 0.024;
  const flangeTop = groutT + flangeT;
  const boltCircleR = (poleRadius + flangeR) / 2 + 0.01;

  const prims: Primitive[] = [
    {
      kind: "cylinder",
      name: "grout_pad",
      component,
      location: [0, 0, groutT / 2],
      rotation: [0, 0, 0],
      materialSlot: slot,
      params: { radius: flangeR * 1.12, depth: groutT },
    },
    {
      kind: "cylinder",
      name: "flange",
      component,
      location: [0, 0, groutT + flangeT / 2],
      rotation: [0, 0, 0],
      materialSlot: slot,
      params: { radius: flangeR, depth: flangeT },
    },
    weldFillet(poleRadius, Math.max(0.012, poleRadius * 0.18), flangeTop, component, slot),
  ];

  const washerT = 0.003;
  const nutH = boltR * 1.1;
  for (let i = 0; i < nBolts; i++) {
    const a = (2 * Math.PI * i) / nBolts;
    const x = boltCircleR * Math.cos(a);
    const y = boltCircleR * Math.sin(a);
    const proj = 0.03;
    const shaftDepth = flangeTop + proj;
    prims.push(
      {
        kind: "cylinder",
        name: `anchor_bolt_${i + 1}`,
        component,
        location: [x, y, shaftDepth / 2],
        rotation: [0, 0, 0],
        materialSlot: "hardware",
        params: { radius: boltR, depth: shaftDepth },
      },
      {
        kind: "cylinder",
        name: `anchor_washer_${i + 1}`,
        component,
        location: [x, y, flangeTop + washerT / 2],
        rotation: [0, 0, 0],
        materialSlot: "hardware",
        params: { radius: boltR * 2.2, depth: washerT },
      },
      {
        kind: "cylinder",
        name: `anchor_nut_${i + 1}`,
        component,
        location: [x, y, flangeTop + washerT + nutH / 2],
        rotation: [0, 0, 0],
        materialSlot: "hardware",
        params: { radius: boltR * 1.7, depth: nutH, segments: 6 },
      },
    );
  }

  const gussetH = Math.max(0.08, poleRadius * 1.1);
  const gussetLen = flangeR - poleRadius - 0.006;
  const midR = poleRadius + gussetLen / 2;
  for (let i = 0; i < nBolts; i++) {
    const a = (2 * Math.PI * (i + 0.5)) / nBolts;
    prims.push({
      kind: "loft",
      name: `gusset_${i + 1}`,
      component,
      location: [midR * Math.cos(a), midR * Math.sin(a), flangeTop + gussetH / 2],
      rotation: [0, Math.PI / 2, a],
      materialSlot: slot,
      params: {
        depth: gussetLen,
        profile_start: { shape: "rect", w: gussetH, h: 0.008 },
        profile_end: { shape: "rect", w: 0.016, h: 0.008 },
      },
    });
  }
  return prims;
}
