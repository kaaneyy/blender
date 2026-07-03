/** Mirror of blender/builders/hardware.py v2 — engineered connection
 * hardware: through-bolt assemblies (shaft spanning the real joint, washers,
 * hex head/nut, member-scaled diameters, multi-bolt patterns) and band
 * clamps where horizontal round members meet upright poles. Keep in exact
 * lockstep with the Python implementation. */
import type { Primitive, Vec3 } from "../types";

const MAX_JOINTS = 24;
const EMBED = 0.025;
const MIN_FACE = 0.01;
const GRID = 0.06;

const AXIS_ROT: Record<number, Vec3> = {
  0: [0, Math.PI / 2, 0],
  1: [Math.PI / 2, 0, 0],
  2: [0, 0, 0],
};

/** Unit axis of a cylinder/cone after a Blender XYZ Euler rotation. */
function cylinderAxis(rotation: Vec3): Vec3 {
  const [rx, ry, rz] = rotation;
  let x = 0;
  let y = -Math.sin(rx);
  let z = Math.cos(rx);
  [x, z] = [x * Math.cos(ry) + z * Math.sin(ry), -x * Math.sin(ry) + z * Math.cos(ry)];
  [x, y] = [x * Math.cos(rz) - y * Math.sin(rz), x * Math.sin(rz) + y * Math.cos(rz)];
  return [x, y, z];
}

export function halfExtents(p: Primitive): Vec3 {
  if (p.kind === "box") {
    const [sx, sy, sz] = p.params.size!;
    const hx = sx / 2;
    const hy = sy / 2;
    const hz = sz / 2;
    if (p.rotation.some((a) => Math.abs(a) > 1e-6)) {
      const m = Math.max(hx, hy, hz);
      return [m, m, m];
    }
    return [hx, hy, hz];
  }
  if (p.kind === "sphere") {
    const r = p.params.radius!;
    return [r, r, r];
  }
  const r =
    p.params.radius ?? Math.max(p.params.radius_bottom ?? 0, p.params.radius_top ?? 0);
  const hd = p.params.depth! / 2;
  const u = cylinderAxis(p.rotation);
  return [0, 1, 2].map(
    (k) => hd * Math.abs(u[k]) + r * Math.sqrt(Math.max(0, 1 - u[k] * u[k])),
  ) as Vec3;
}

function radiusAtZ(prim: Primitive, z: number): number {
  if (prim.kind === "cylinder") return prim.params.radius!;
  const rb = prim.params.radius_bottom!;
  const rt = prim.params.radius_top!;
  const depth = prim.params.depth!;
  const t = depth ? (z - (prim.location[2] - depth / 2)) / depth : 0;
  return rb + (rt - rb) * Math.min(1, Math.max(0, t));
}

function isUprightRound(p: Primitive): boolean {
  return (
    (p.kind === "cylinder" || p.kind === "cone") &&
    p.rotation.every((a) => Math.abs(a) < 1e-3)
  );
}

function pos(center: readonly number[], axis: number, along: number): Vec3 {
  const out = [...center] as Vec3;
  out[axis] = along;
  return out;
}

function bolt(
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

function bandClamp(
  joint: number,
  vert: Primitive,
  centerZ: number,
  axisH: number,
): Primitive[] {
  const r = radiusAtZ(vert, centerZ) + 0.006;
  const cx = vert.location[0];
  const cy = vert.location[1];
  const prims: Primitive[] = [
    {
      kind: "cylinder",
      name: `joint${joint}_band`,
      component: "hardware",
      location: [cx, cy, centerZ],
      rotation: [0, 0, 0],
      materialSlot: "hardware",
      params: { radius: r, depth: 0.05 },
    },
  ];
  const perpH = 1 - axisH;
  const shaftR = 0.005;
  [1, -1].forEach((side, i) => {
    const center: Vec3 = [cx, cy, centerZ];
    center[perpH] += side * r * 0.85;
    const spanLo = center[axisH] - r * 0.8;
    const spanHi = center[axisH] + r * 0.8;
    prims.push(...bolt(joint, i + 1, center, axisH, shaftR, spanLo, spanHi));
  });
  return prims;
}

function boltPattern(d1: number, d2: number, headR: number): Array<[number, number]> {
  const edge = 1.5 * headR;
  const big1 = d1 >= 0.22 && d1 / 2 - 0.3 * d1 >= edge;
  const big2 = d2 >= 0.22 && d2 / 2 - 0.3 * d2 >= edge;
  if (big1 && big2) {
    return [
      [-0.3 * d1, -0.3 * d2],
      [0.3 * d1, -0.3 * d2],
      [-0.3 * d1, 0.3 * d2],
      [0.3 * d1, 0.3 * d2],
    ];
  }
  if (big1) return [[-0.3 * d1, 0], [0.3 * d1, 0]];
  if (big2) return [[0, -0.3 * d2], [0, 0.3 * d2]];
  return [[0, 0]];
}

export function computeHardware(prims: Primitive[]): Primitive[] {
  const boxes = prims
    .filter((p) => p.component !== "hardware")
    .map((p) => ({ p, c: p.location, h: halfExtents(p) }));
  const out: Primitive[] = [];
  const seen = new Set<string>();
  let joint = 0;

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
      const perp = [0, 1, 2].filter((k) => k !== axis);
      const d1 = hi[perp[0]] - lo[perp[0]];
      const d2 = hi[perp[1]] - lo[perp[1]];
      if (Math.min(d1, d2) < MIN_FACE) continue;

      joint += 1;

      const upright = isUprightRound(a.p) ? a.p : isUprightRound(b.p) ? b.p : null;
      const other = upright === a.p ? b.p : a.p;
      if (
        axis !== 2 &&
        upright !== null &&
        !isUprightRound(other) &&
        (other.kind === "cylinder" || other.kind === "cone")
      ) {
        out.push(...bandClamp(joint, upright, center[2], axis));
      } else {
        const shaftR = Math.min(Math.max(0.22 * Math.min(d1, d2), 0.004), 0.011);
        const above = Math.max(a.c[axis] + a.h[axis], b.c[axis] + b.h[axis]) - hi[axis];
        const below = lo[axis] - Math.min(a.c[axis] - a.h[axis], b.c[axis] - b.h[axis]);
        const spanHi = hi[axis] + Math.min(above, EMBED);
        const spanLo = lo[axis] - Math.min(below, EMBED);
        boltPattern(d1, d2, 1.8 * shaftR).forEach(([o1, o2], idx) => {
          const c = [...center] as Vec3;
          c[perp[0]] += o1;
          c[perp[1]] += o2;
          out.push(...bolt(joint, idx + 1, c, axis, shaftR, spanLo, spanHi));
        });
      }

      if (joint >= MAX_JOINTS) return out;
    }
  }
  return out;
}
