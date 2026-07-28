/** Mirror of blender/builders/connections.py — standard fabrication
 * connections: the pure primitive emitter library the hardware orchestrator
 * dispatches to (through-bolts, carriage bolts, lag screws, slip fitters,
 * split band clamps, flange splices, weld fillets, ground packages).
 * Keep in exact lockstep with the Python implementation. */
import type { Primitive, Vec3 } from "../types";

const BOLT_COUNT: Record<string, number> = { light: 4, standard: 4, heavy: 6 };
const BOLT_R: Record<string, number> = { light: 0.008, standard: 0.011, heavy: 0.014 };

/** bolt axis -> rotation that maps a Z-axis cylinder onto that axis */
export const AXIS_ROT: Record<number, Vec3> = {
  0: [0, Math.PI / 2, 0],
  1: [Math.PI / 2, 0, 0],
  2: [0, 0, 0],
};

/** shop-standard clearance of a drilled hole over its bolt diameter
 * (1/16 in, AISC J3.2 nominal) — a 1/2 in anchor is drilled 9/16 in. */
const HOLE_CLEARANCE = 0.0016;

function pos(center: readonly number[], axis: number, along: number): Vec3 {
  const out = [...center] as Vec3;
  out[axis] = along;
  return out;
}

/** A triangular stiffener plate in the vertical plane through `angle`,
 * radiating from a member of radius `attachR` at `centerXY` out to `reachR`
 * (mirror of connections.py gusset_plate).
 *
 * Lofts bridge two centered cross-sections, so a plain tapered loft is a
 * symmetric wedge whose edges both slope — the "floating arrowhead" look.
 * This helper tilts the wedge by half its taper angle so ONE long edge lies
 * perfectly flat, and shifts it so the raked tall edge is buried inside the
 * member (the visible junction is a clean weld line):
 * hug="bottom" — bottom edge flat ON flushZ (base-plate gusset);
 * hug="top" — top edge flat AT flushZ (knee brace under an arm).
 * Returns [] when the radial run is too short for a plate. */
export function gussetPlate(
  name: string,
  component: string,
  slot: string,
  centerXY: [number, number],
  angle: number,
  attachR: number,
  reachR: number,
  flushZ: number,
  hug: "bottom" | "top" = "bottom",
  height = 0.1,
  tipRatio = 0.35,
  thickness = 0.008,
): Primitive[] {
  const w0 = height;
  const w1 = Math.max(tipRatio * height, 0.012);
  const taper = (w0 - w1) / 2;
  const run = reachR - attachR;
  if (run < 0.02 || height <= 0) return [];
  // solve the plate run d and tilt t so the far tip lands at reachR with
  // the flush edge level (fixed point; 4 rounds converge well under 0.1mm)
  let d = run;
  let t = 0;
  for (let i = 0; i < 4; i++) {
    t = Math.atan2(taper, d);
    d = (run + Math.sin(t) * taper) / Math.cos(t);
  }
  const s = hug === "bottom" ? 1 : -1;
  const locR = attachR + (Math.cos(t) * d) / 2 - (Math.sin(t) * w0) / 2;
  const locZ = flushZ + s * ((Math.cos(t) * w0) / 2 - (Math.sin(t) * d) / 2);
  const [cx, cy] = centerXY;
  return [
    {
      kind: "loft",
      name,
      component,
      location: [cx + locR * Math.cos(angle), cy + locR * Math.sin(angle), locZ],
      // local Z -> radial (tilted by the half-taper), spun to the angle
      rotation: [0, Math.PI / 2 + s * t, angle],
      materialSlot: slot,
      params: {
        depth: d,
        profile_start: { shape: "rect", w: w0, h: thickness },
        profile_end: { shape: "rect", w: w1, h: thickness },
      },
    },
  ];
}

export function weldFillet(
  radius: number,
  size: number,
  z: number,
  component: string,
  slot: string,
  name = "weld_bead",
  center: [number, number] = [0, 0],
): Primitive {
  return {
    kind: "lathe",
    name,
    component,
    location: [center[0], center[1], z],
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

/** Through-bolt: washer+hex head at spanHi, washer+hex nut at spanLo. */
export function throughBoltAssembly(
  joint: number,
  idx: number,
  center: readonly number[],
  axis: number,
  shaftR: number,
  spanLo: number,
  spanHi: number,
): Primitive[] {
  const rot = AXIS_ROT[axis];
  const headR = 1.8 * shaftR;
  const headH = Math.max(1.2 * shaftR, 0.004);
  const nutR = 1.6 * shaftR;
  const nutH = Math.max(shaftR, 0.003);
  const wR = 2.2 * shaftR;
  const wH = 0.002;
  const depth = Math.max(spanHi - spanLo, 0.012) + 2 * wH;
  const mid = (spanLo + spanHi) / 2;
  const name = `joint${joint}_bolt${idx}`;

  const prim = (
    kindName: string,
    along: number,
    radius: number,
    d: number,
    segments?: number,
  ): Primitive => ({
    kind: "cylinder",
    name: `${name}_${kindName}`,
    component: "hardware",
    location: pos(center, axis, along),
    rotation: rot,
    materialSlot: "hardware",
    params: segments ? { radius, depth: d, segments } : { radius, depth: d },
  });

  return [
    prim("shaft", mid, shaftR, depth),
    prim("washer_h", spanHi + wH / 2, wR, wH),
    prim("head", spanHi + wH + headH / 2, headR, headH, 6),
    prim("washer_n", spanLo - wH / 2, wR, wH),
    prim("nut", spanLo - wH - nutH / 2, nutR, nutH, 6),
  ];
}

/** Carriage bolt: smooth dome head half-proud of the timber face (no washer
 * under it), flat washer + hex nut on the opposite (steel) face. */
export function carriageBoltAssembly(
  joint: number,
  idx: number,
  center: readonly number[],
  axis: number,
  shaftR: number,
  spanLo: number,
  spanHi: number,
  domeAtHi = true,
): Primitive[] {
  const rot = AXIS_ROT[axis];
  const domeR = 1.6 * shaftR;
  const nutR = 1.6 * shaftR;
  const nutH = Math.max(shaftR, 0.003);
  const wR = 2.2 * shaftR;
  const wH = 0.002;
  const depth = Math.max(spanHi - spanLo, 0.012);
  const mid = (spanLo + spanHi) / 2;
  const name = `joint${joint}_bolt${idx}`;
  const [domeEnd, nutEnd, nutDir] = domeAtHi
    ? [spanHi, spanLo, -1]
    : [spanLo, spanHi, 1];

  const cyl = (
    kindName: string,
    along: number,
    radius: number,
    d: number,
    segments?: number,
  ): Primitive => ({
    kind: "cylinder",
    name: `${name}_${kindName}`,
    component: "hardware",
    location: pos(center, axis, along),
    rotation: rot,
    materialSlot: "hardware",
    params: segments ? { radius, depth: d, segments } : { radius, depth: d },
  });

  return [
    cyl("shaft", mid, shaftR, depth),
    {
      kind: "sphere",
      name: `${name}_dome`,
      component: "hardware",
      location: pos(center, axis, domeEnd),
      rotation: rot,
      materialSlot: "hardware",
      params: { radius: domeR },
    },
    cyl("washer_n", nutEnd + nutDir * (wH / 2), wR, wH),
    cyl("nut", nutEnd + nutDir * (wH + nutH / 2), nutR, nutH, 6),
  ];
}

/** Lag screw: hex head + washer at spanHi, shank embedded — no nut. */
export function lagScrewAssembly(
  joint: number,
  idx: number,
  center: readonly number[],
  axis: number,
  shaftR: number,
  spanLo: number,
  spanHi: number,
): Primitive[] {
  const rot = AXIS_ROT[axis];
  const headR = 1.8 * shaftR;
  const headH = Math.max(1.2 * shaftR, 0.004);
  const wR = 2.2 * shaftR;
  const wH = 0.002;
  const depth = Math.max(spanHi - spanLo, 0.012);
  const mid = (spanLo + spanHi) / 2;
  const name = `joint${joint}_bolt${idx}`;

  const cyl = (
    kindName: string,
    along: number,
    radius: number,
    d: number,
    segments?: number,
  ): Primitive => ({
    kind: "cylinder",
    name: `${name}_${kindName}`,
    component: "hardware",
    location: pos(center, axis, along),
    rotation: rot,
    materialSlot: "hardware",
    params: segments ? { radius, depth: d, segments } : { radius, depth: d },
  });

  return [
    cyl("shaft", mid, shaftR, depth),
    cyl("washer_h", spanHi + wH / 2, wR, wH),
    cyl("head", spanHi + wH + headH / 2, headR, headH, 6),
  ];
}

/** Slip-fitter: a collar gripping a round-over-round telescoping fit with
 * 3 radial set screws at 120°. outerR is the outer member's fit radius;
 * screwR is the set-screw shaft radius — the orchestrator passes a catalog
 * size scaled to the fit so the drawn screw is the scheduled screw (the
 * default reproduces the original fixed M8). */
export function slipFitter(
  joint: number,
  outerR: number,
  centerXY: [number, number],
  centerZ: number,
  screwR = 0.004,
): Primitive[] {
  const [cx, cy] = centerXY;
  const collarR = outerR + 0.004;
  const collarD = Math.min(Math.max(1.2 * outerR, 0.04), 0.12);
  const prims: Primitive[] = [
    {
      kind: "tube",
      name: `joint${joint}_fitter`,
      component: "hardware",
      location: [cx, cy, centerZ],
      rotation: [0, 0, 0],
      materialSlot: "hardware",
      params: { radius: collarR, wall: 0.004, depth: collarD },
    },
  ];
  const screwLen = Math.max(0.03, 7.5 * screwR);
  const headH = 1.25 * screwR;
  for (let i = 0; i < 3; i++) {
    const a = (2 * Math.PI * i) / 3;
    const midR = collarR + screwLen / 2 - 0.012;
    prims.push({
      kind: "cylinder",
      name: `joint${joint}_setscrew${i + 1}`,
      component: "hardware",
      location: [cx + midR * Math.cos(a), cy + midR * Math.sin(a), centerZ],
      rotation: [0, Math.PI / 2, a],
      materialSlot: "hardware",
      params: { radius: screwR, depth: screwLen },
    });
    const headRDist = collarR + screwLen - 0.012 + headH / 2;
    prims.push({
      kind: "cylinder",
      name: `joint${joint}_setscrew${i + 1}_head`,
      component: "hardware",
      location: [cx + headRDist * Math.cos(a), cy + headRDist * Math.sin(a), centerZ],
      rotation: [0, Math.PI / 2, a],
      materialSlot: "hardware",
      params: { radius: 1.75 * screwR, depth: headH, segments: 6 },
    });
  }
  return prims;
}

/** Two-piece saddle band: split band + ear tabs, bolted through the EARS.
 * The orchestrator passes its catalog-snapped shaftR so the drawn ear bolts
 * match the scheduled fastener (undefined falls back to the raw formula). */
export function splitBandClamp(
  joint: number,
  poleR: number,
  centerXY: [number, number],
  centerZ: number,
  axisH: number,
  armR: number,
  shaftR?: number,
): Primitive[] {
  const [cx, cy] = centerXY;
  const bandR = poleR + 0.006;
  const bandW = Math.min(Math.max(3 * armR, 0.03), 0.08);
  const prims: Primitive[] = [
    {
      kind: "tube",
      name: `joint${joint}_band`,
      component: "hardware",
      location: [cx, cy, centerZ],
      rotation: [0, 0, 0],
      materialSlot: "hardware",
      params: { radius: bandR, wall: 0.003, depth: bandW },
    },
  ];
  const perpH = 1 - axisH;
  const earLen = 0.025;
  const earThick = 0.024;
  const earBoltR = shaftR ?? Math.min(Math.max(0.4 * armR, 0.004), 0.008);
  [1, -1].forEach((side, i) => {
    const earCenter: Vec3 = [cx, cy, centerZ];
    earCenter[perpH] += side * (bandR + earLen / 2);
    const size: Vec3 = [0, 0, 0];
    size[axisH] = earThick;
    size[perpH] = earLen;
    size[2] = bandW * 0.8;
    prims.push({
      kind: "box",
      name: `joint${joint}_ear${i + 1}`,
      component: "hardware",
      location: earCenter,
      rotation: [0, 0, 0],
      materialSlot: "hardware",
      params: { size },
    });
    const spanLo = earCenter[axisH] - earThick / 2 - 0.002;
    const spanHi = earCenter[axisH] + earThick / 2 + 0.002;
    prims.push(
      ...throughBoltAssembly(joint, i + 1, earCenter, axisH, earBoltR, spanLo, spanHi),
    );
  });
  return prims;
}

/** Bolted flange splice: two mating discs + a bolt circle through both.
 * The orchestrator passes its catalog-snapped shaftR so the drawn bolts
 * match the scheduled fastener (undefined falls back to the raw formula). */
export function flangeSplice(
  joint: number,
  center: readonly number[],
  axis: number,
  memberR: number,
  nBolts = 6,
  shaftR?: number,
): Primitive[] {
  const rot = AXIS_ROT[axis];
  const discR = Math.max(memberR * 1.6, memberR + 0.03);
  const discT = 0.01;
  const bcr = (memberR + discR) / 2;
  const boltR = shaftR ?? Math.min(Math.max(0.35 * memberR, 0.005), 0.012);
  const prims: Primitive[] = [];
  [-1, 1].forEach((side, i) => {
    prims.push({
      kind: "cylinder",
      name: `joint${joint}_flange${i + 1}`,
      component: "hardware",
      location: pos(center, axis, center[axis] + (side * discT) / 2),
      rotation: rot,
      materialSlot: "hardware",
      params: { radius: discR, depth: discT },
    });
  });
  const perp = [0, 1, 2].filter((k) => k !== axis);
  for (let i = 0; i < nBolts; i++) {
    const a = (2 * Math.PI * i) / nBolts;
    const c = [...center] as Vec3;
    c[perp[0]] += bcr * Math.cos(a);
    c[perp[1]] += bcr * Math.sin(a);
    const spanLo = center[axis] - discT - 0.002;
    const spanHi = center[axis] + discT + 0.002;
    prims.push(...throughBoltAssembly(joint, i + 1, c, axis, boltR, spanLo, spanHi));
  }
  return prims;
}

/** Ground connection for a vertical member at grade. center is the member's
 * (x, y); shape "square" swaps the round flange for a box plate with corner
 * anchor bolts (no weld ring). `gussets` (default true) toggles the
 * triangular stiffener webs; the orchestrator passes false for
 * light/standard auto anchors so a modest structure's feet stay lean (plate
 * + grout + bolts only), reserving the full gusseted package for genuinely
 * heavy members. Defaults reproduce the original street_light flange
 * byte-for-byte. */
export function groundConnection(
  poleRadius: number,
  mount = "flange",
  loadClass = "standard",
  component = "base_plate",
  slot = "base",
  center: [number, number] = [0, 0],
  shape: "round" | "square" = "round",
  namePrefix = "",
  gussets = true,
): Primitive[] {
  const [cx, cy] = center;
  const n = namePrefix;
  if (mount === "burial") {
    return [
      {
        kind: "lathe",
        name: `${n}backfill_collar`,
        component,
        location: [cx, cy, 0],
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
        name: `${n}concrete_pier`,
        component,
        location: [cx, cy, pierH / 2],
        rotation: [0, 0, 0],
        materialSlot: slot,
        params: { radius: pierR, depth: pierH },
      },
      weldFillet(poleRadius, poleRadius * 0.35, pierH, component, slot, `${n}grout_ring`, center),
    ];
  }

  // flange
  const nBolts = BOLT_COUNT[loadClass] ?? 4;
  const boltR = BOLT_R[loadClass] ?? 0.011;
  const flangeR = Math.max(poleRadius * 2.1, poleRadius + 0.09);
  const flangeT = 0.028;
  const groutT = 0.024;
  const flangeTop = groutT + flangeT;
  const square = shape === "square";

  const prims: Primitive[] = [];
  if (square) {
    const side = 2 * flangeR;
    prims.push(
      {
        kind: "box",
        name: `${n}grout_pad`,
        component,
        location: [cx, cy, groutT / 2],
        rotation: [0, 0, 0],
        materialSlot: slot,
        params: { size: [side * 1.12, side * 1.12, groutT] },
      },
      {
        kind: "box",
        name: `${n}flange`,
        component,
        location: [cx, cy, groutT + flangeT / 2],
        rotation: [0, 0, 0],
        materialSlot: slot,
        params: { size: [side, side, flangeT] },
      },
    );
  } else {
    prims.push(
      {
        kind: "cylinder",
        name: `${n}grout_pad`,
        component,
        location: [cx, cy, groutT / 2],
        rotation: [0, 0, 0],
        materialSlot: slot,
        params: { radius: flangeR * 1.12, depth: groutT },
      },
      {
        kind: "cylinder",
        name: `${n}flange`,
        component,
        location: [cx, cy, groutT + flangeT / 2],
        rotation: [0, 0, 0],
        materialSlot: slot,
        params: { radius: flangeR, depth: flangeT },
      },
      weldFillet(
        poleRadius,
        Math.max(0.012, poleRadius * 0.18),
        flangeTop,
        component,
        slot,
        `${n}weld_bead`,
        center,
      ),
    );
  }

  // anchor bolts: circle on a real BCD for round; corner pattern for square
  const washerT = 0.003;
  const nutH = boltR * 1.1;
  let anchorXY: Array<[number, number]>;
  if (square) {
    const inset = Math.max(0.02, 3 * boltR);
    const d = flangeR - inset;
    anchorXY = [
      [cx + d, cy + d],
      [cx - d, cy + d],
      [cx - d, cy - d],
      [cx + d, cy - d],
    ];
  } else {
    const boltCircleR = (poleRadius + flangeR) / 2 + 0.01;
    anchorXY = Array.from({ length: nBolts }, (_, i) => {
      const a = (2 * Math.PI * i) / nBolts;
      return [cx + boltCircleR * Math.cos(a), cy + boltCircleR * Math.sin(a)];
    });
  }
  // clearance of a drilled hole over its bolt — 1/16 in, the shop standard
  // (AISC J3.2 nominal): a 1/2 in anchor gets a 9/16 in hole. The drawing's
  // FOOT DETAIL calls this out as the (0.535) 4x hole.
  const holeR = boltR + HOLE_CLEARANCE / 2;
  anchorXY.forEach(([x, y], i) => {
    const proj = 0.03;
    const shaftDepth = flangeTop + proj;
    prims.push({
      // the drilled hole itself: negative space through plate AND grout,
      // boolean-subtracted in Blender (never rendered in the preview) so the
      // plate is actually drilled rather than merely having a bolt standing
      // on it. Over-long on purpose so both faces cut clean.
      kind: "cylinder",
      name: `${n}anchor_hole_${i + 1}`,
      component,
      location: [x, y, flangeTop / 2],
      rotation: [0, 0, 0],
      materialSlot: slot,
      cut: true,
      params: { radius: holeR, depth: flangeTop * 2.4 },
    });
    prims.push(
      {
        kind: "cylinder",
        name: `${n}anchor_bolt_${i + 1}`,
        component,
        location: [x, y, shaftDepth / 2],
        rotation: [0, 0, 0],
        materialSlot: "hardware",
        params: { radius: boltR, depth: shaftDepth },
      },
      {
        kind: "cylinder",
        name: `${n}anchor_washer_${i + 1}`,
        component,
        location: [x, y, flangeTop + washerT / 2],
        rotation: [0, 0, 0],
        materialSlot: "hardware",
        params: { radius: boltR * 2.2, depth: washerT },
      },
      {
        kind: "cylinder",
        name: `${n}anchor_nut_${i + 1}`,
        component,
        location: [x, y, flangeTop + washerT + nutH / 2],
        rotation: [0, 0, 0],
        materialSlot: "hardware",
        params: { radius: boltR * 1.7, depth: nutH, segments: 6 },
      },
    );
  });

  // triangular gusset webs between the member and the plate edge — flat on
  // the flange, tall edge buried in the member, hypotenuse down to the rim
  // — skipped entirely for lean (non-heavy) auto anchors, see `gussets`
  if (gussets) {
    const gussetH = Math.max(0.08, poleRadius * 1.1);
    const gussetAngles = square
      ? [0, 1, 2, 3].map((i) => (2 * Math.PI * i) / 4)
      : Array.from({ length: nBolts }, (_, i) => (2 * Math.PI * (i + 0.5)) / nBolts);
    gussetAngles.forEach((a, i) => {
      prims.push(
        ...gussetPlate(
          `${n}gusset_${i + 1}`, component, slot, center, a,
          poleRadius, flangeR - 0.004, flangeTop, "bottom", gussetH,
        ),
      );
    });
  }
  return prims;
}
