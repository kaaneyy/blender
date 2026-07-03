/** Mirror of blender/builders/hardware.py — visible bolt/nut assemblies at
 * the joints between components (hex heads = 6-segment cylinders). */
import type { Primitive, Vec3 } from "../types";

const HEAD_R = 0.016;
const HEAD_H = 0.01;
const SHAFT_R = 0.007;
const SHAFT_LEN = 0.026;
const NUT_R = 0.014;
const NUT_H = 0.008;
const MAX_CONNECTIONS = 24;
const GRID = 0.06;

const AXIS_ROT: Record<number, Vec3> = {
  0: [0, Math.PI / 2, 0],
  1: [Math.PI / 2, 0, 0],
  2: [0, 0, 0],
};

export function halfExtents(p: Primitive): Vec3 {
  let hx: number;
  let hy: number;
  let hz: number;
  if (p.kind === "box") {
    const [sx, sy, sz] = p.params.size!;
    hx = sx / 2;
    hy = sy / 2;
    hz = sz / 2;
  } else if (p.kind === "cylinder" || p.kind === "cone") {
    const r = p.params.radius ?? Math.max(p.params.radius_bottom ?? 0, p.params.radius_top ?? 0);
    hx = r;
    hy = r;
    hz = p.params.depth! / 2;
  } else {
    hx = hy = hz = p.params.radius!;
  }
  if (p.rotation.some((a) => Math.abs(a) > 1e-6)) {
    const m = Math.max(hx, hy, hz);
    return [m, m, m];
  }
  return [hx, hy, hz];
}

function bolt(index: number, center: Vec3, axis: number): Primitive[] {
  const rot = AXIS_ROT[axis];
  const along = (dist: number): Vec3 => {
    const out: Vec3 = [...center];
    out[axis] += dist;
    return out;
  };
  return [
    {
      kind: "cylinder", name: `bolt_${index}_shaft`, component: "hardware",
      location: center, rotation: rot, materialSlot: "hardware",
      params: { radius: SHAFT_R, depth: SHAFT_LEN },
    },
    {
      kind: "cylinder", name: `bolt_${index}_head`, component: "hardware",
      location: along(SHAFT_LEN / 2 + HEAD_H / 2), rotation: rot, materialSlot: "hardware",
      params: { radius: HEAD_R, depth: HEAD_H, segments: 6 },
    },
    {
      kind: "cylinder", name: `bolt_${index}_nut`, component: "hardware",
      location: along(-(SHAFT_LEN / 2 + NUT_H / 2)), rotation: rot, materialSlot: "hardware",
      params: { radius: NUT_R, depth: NUT_H, segments: 6 },
    },
  ];
}

export function computeHardware(prims: Primitive[]): Primitive[] {
  const boxes = prims
    .filter((p) => p.component !== "hardware")
    .map((p) => ({ p, c: p.location, h: halfExtents(p) }));
  const out: Primitive[] = [];
  const seen = new Set<string>();
  let n = 0;

  for (let i = 0; i < boxes.length; i++) {
    for (let j = i + 1; j < boxes.length; j++) {
      const a = boxes[i];
      const b = boxes[j];
      if (a.p.component === b.p.component) continue;

      const lo: Vec3 = [0, 0, 0];
      const hi: Vec3 = [0, 0, 0];
      let overlaps = true;
      for (let k = 0; k < 3; k++) {
        lo[k] = Math.max(a.c[k] - a.h[k], b.c[k] - b.h[k]);
        hi[k] = Math.min(a.c[k] + a.h[k], b.c[k] + b.h[k]);
        if (hi[k] <= lo[k]) {
          overlaps = false;
          break;
        }
      }
      if (!overlaps) continue;

      const center: Vec3 = [
        (lo[0] + hi[0]) / 2,
        (lo[1] + hi[1]) / 2,
        (lo[2] + hi[2]) / 2,
      ];
      const key = center.map((c) => Math.round(c / GRID)).join(",");
      if (seen.has(key)) continue;
      seen.add(key);

      let axis = 0;
      for (let k = 1; k < 3; k++) if (hi[k] - lo[k] < hi[axis] - lo[axis]) axis = k;
      n += 1;
      out.push(...bolt(n, center, axis));
      if (n >= MAX_CONNECTIONS) return out;
    }
  }
  return out;
}
