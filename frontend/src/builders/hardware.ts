/** Mirror of blender/builders/hardware.py v3 — connection-hardware
 * orchestrator: detects inter-component contacts, honors the spec's
 * declared `connections` intent (weld / slip_fit / band_clamp / carriage /
 * flange_splice / lag_screw / through_bolt / anchor_base / none), infers a
 * fabrication-correct type for undeclared joints, and dispatches to the
 * emitter library in connections.ts. Joints are collected then ordered
 * deterministically (anchor bases, declared, inferred; position-stable) so
 * ids survive slider nudges. Keep in exact lockstep with the Python
 * implementation. */
import type { AssetSpec, Primitive, SpecConnection, Vec3 } from "../types";
import { profileBounds, resolveProfile } from "../shapes";
import { materialPresetName } from "./base";
import {
  carriageBoltAssembly,
  flangeSplice,
  groundConnection,
  lagScrewAssembly,
  slipFitter,
  splitBandClamp,
  throughBoltAssembly,
  weldFillet,
} from "./connections";

/** kinds treated as round members for band-clamp/slip-fit detection */
const ROUND_KINDS = new Set(["cylinder", "cone", "sweep", "tube"]);

/** preset names treated as structural metal (mirror of hardware.py). */
const METAL_PRESETS = new Set([
  "galvanized_steel", "cast_iron", "brushed_aluminum",
  "powder_coat_black", "powder_coat_green",
]);

/** preset names treated as timber (carriage-bolt territory) */
const WOOD_PRESETS = new Set(["wood_slat"]);

function presetName(slot: string, spec?: AssetSpec): string | null {
  if (!spec) return null;
  return materialPresetName(spec, slot);
}

function isSoft(slot: string, spec?: AssetSpec): boolean {
  const preset = presetName(slot, spec);
  return preset !== null && !METAL_PRESETS.has(preset);
}

function isWood(slot: string, spec?: AssetSpec): boolean {
  const preset = presetName(slot, spec);
  return preset !== null && WOOD_PRESETS.has(preset);
}

const MAX_JOINTS = 24;
const EMBED = 0.025;
const MIN_FACE = 0.01;
const GRID = 0.06;

const CONNECTION_TYPES = new Set([
  "anchor_base", "through_bolt", "flange_splice", "band_clamp",
  "slip_fit", "weld", "carriage_bolt", "lag_screw", "none",
]);

/** Three.js-parity Euler XYZ rotation matrix (mirror of hardware.py). */
function eulerXyzMatrix(rot: Vec3): number[][] {
  const [x, y, z] = rot;
  const c1 = Math.cos(x), s1 = Math.sin(x);
  const c2 = Math.cos(y), s2 = Math.sin(y);
  const c3 = Math.cos(z), s3 = Math.sin(z);
  return [
    [c2 * c3, -c2 * s3, s2],
    [c1 * s3 + c3 * s1 * s2, c1 * c3 - s1 * s2 * s3, -c2 * s1],
    [s1 * s3 - c1 * c3 * s2, c3 * s1 + c1 * s2 * s3, c1 * c2],
  ];
}

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

export interface Aabb {
  center: Vec3;
  half: Vec3;
}

/** World AABB of a rotated local box: rotate its 8 corners and take the
 * extremes. Tight (unlike the old max-extent cube), still axis-aligned. */
function rotatedAabb(loc: Vec3, centerOff: Vec3, h: Vec3, rotation: Vec3): Aabb {
  const m = eulerXyzMatrix(rotation);
  const lo: Vec3 = [Infinity, Infinity, Infinity];
  const hi: Vec3 = [-Infinity, -Infinity, -Infinity];
  for (const sx of [-1, 1]) {
    for (const sy of [-1, 1]) {
      for (const sz of [-1, 1]) {
        const v: Vec3 = [
          centerOff[0] + sx * h[0],
          centerOff[1] + sy * h[1],
          centerOff[2] + sz * h[2],
        ];
        for (let k = 0; k < 3; k++) {
          const w = m[k][0] * v[0] + m[k][1] * v[1] + m[k][2] * v[2];
          lo[k] = Math.min(lo[k], w);
          hi[k] = Math.max(hi[k], w);
        }
      }
    }
  }
  return {
    center: [0, 1, 2].map((k) => loc[k] + (lo[k] + hi[k]) / 2) as Vec3,
    half: [0, 1, 2].map((k) => (hi[k] - lo[k]) / 2) as Vec3,
  };
}

/** Mirror of hardware.py _aabb: (center, half extents) of the world AABB. */
export function aabb(p: Primitive): Aabb {
  const loc = p.location;
  const rotated = p.rotation.some((a) => Math.abs(a) > 1e-6);

  if (p.kind === "box") {
    const [sx, sy, sz] = p.params.size!;
    const h: Vec3 = [sx / 2, sy / 2, sz / 2];
    return rotated ? rotatedAabb(loc, [0, 0, 0], h, p.rotation) : { center: loc, half: h };
  }
  if (p.kind === "sphere") {
    const r = p.params.radius!;
    return { center: loc, half: [r, r, r] };
  }
  if (p.kind === "lathe") {
    const pts = resolveProfile(p.params.profile!, p.params.radius, p.params.depth);
    const [maxR, z0, z1] = profileBounds(pts);
    const h: Vec3 = [maxR, maxR, (z1 - z0) / 2];
    const off: Vec3 = [0, 0, (z0 + z1) / 2];
    if (rotated) return rotatedAabb(loc, off, h, p.rotation);
    return { center: [loc[0], loc[1], loc[2] + off[2]], half: h };
  }
  if (p.kind === "sweep") {
    const r = Math.max(p.params.radius!, p.params.radius_end ?? 0);
    const xs = p.params.path!.map((pt) => pt[0]);
    const ys = p.params.path!.map((pt) => pt[1]);
    const zs = p.params.path!.map((pt) => pt[2]);
    const h: Vec3 = [
      (Math.max(...xs) - Math.min(...xs)) / 2 + r,
      (Math.max(...ys) - Math.min(...ys)) / 2 + r,
      (Math.max(...zs) - Math.min(...zs)) / 2 + r,
    ];
    const off: Vec3 = [
      (Math.max(...xs) + Math.min(...xs)) / 2,
      (Math.max(...ys) + Math.min(...ys)) / 2,
      (Math.max(...zs) + Math.min(...zs)) / 2,
    ];
    if (rotated) return rotatedAabb(loc, off, h, p.rotation);
    return {
      center: [loc[0] + off[0], loc[1] + off[1], loc[2] + off[2]],
      half: h,
    };
  }
  if (p.kind === "loft") {
    const ps = p.params.profile_start!;
    const pe = p.params.profile_end!;
    const h: Vec3 = [
      Math.max(ps.w, pe.w) / 2,
      Math.max(ps.h, pe.h) / 2,
      p.params.depth! / 2,
    ];
    return rotated ? rotatedAabb(loc, [0, 0, 0], h, p.rotation) : { center: loc, half: h };
  }
  // cylinder / cone / tube
  const r =
    p.params.radius ?? Math.max(p.params.radius_bottom ?? 0, p.params.radius_top ?? 0);
  const hd = p.params.depth! / 2;
  const u = cylinderAxis(p.rotation);
  return {
    center: loc,
    half: [0, 1, 2].map(
      (k) => hd * Math.abs(u[k]) + r * Math.sqrt(Math.max(0, 1 - u[k] * u[k])),
    ) as Vec3,
  };
}

/** Back-compat: extents only (center may differ for sweeps/lathes). */
export function halfExtents(p: Primitive): Vec3 {
  return aabb(p).half;
}

function radiusAtZ(prim: Primitive, z: number): number {
  if (prim.kind === "cylinder" || prim.kind === "tube") return prim.params.radius!;
  const rb = prim.params.radius_bottom!;
  const rt = prim.params.radius_top!;
  const depth = prim.params.depth!;
  const t = depth ? (z - (prim.location[2] - depth / 2)) / depth : 0;
  return rb + (rt - rb) * Math.min(1, Math.max(0, t));
}

function roundRadius(prim: Primitive): number {
  return (
    prim.params.radius ??
    Math.max(prim.params.radius_bottom ?? 0, prim.params.radius_top ?? 0)
  );
}

function isUprightRound(p: Primitive): boolean {
  return (
    (p.kind === "cylinder" || p.kind === "cone" || p.kind === "tube") &&
    p.rotation.every((a) => Math.abs(a) < 1e-3)
  );
}

/** A member that carries load down to grade: an upright round, or an
 * unrotated box clearly taller than it is wide. */
function isVerticalStructural(p: Primitive): boolean {
  if (isUprightRound(p)) return true;
  if (p.kind === "box" && p.rotation.every((a) => Math.abs(a) < 1e-3)) {
    const [sx, sy, sz] = p.params.size!;
    return sz >= 2 * Math.max(sx, sy);
  }
  return false;
}

/** C6: fastener sizing scales with the connection's tributary load tier
 * (larger joined member's bounding volume — heuristic, not FEA). */
const LOAD_FACTOR: Record<string, number> = { light: 0.75, standard: 1.0, heavy: 1.35 };

function volume(p: Primitive): number {
  const h = aabb(p).half;
  return 8 * h[0] * h[1] * h[2];
}

function loadClass(pa: Primitive, pb: Primitive): string {
  const v = Math.max(volume(pa), volume(pb));
  if (v > 0.15) return "heavy";
  if (v < 0.01) return "light";
  return "standard";
}

/** Bolt offsets on the joint face (mirror of hardware.py _bolt_pattern):
 * round faces shrink the offsets so corner bolts stay inside the circle; an
 * explicit count overrides with an evenly spaced row along the longer axis. */
function boltPattern(
  d1: number,
  d2: number,
  headR: number,
  roundFace = false,
  count?: number,
): Array<[number, number]> {
  const scale = roundFace ? 0.7 : 1.0;
  if (count) {
    if (count === 1) return [[0, 0]];
    const along1 = d1 >= d2;
    const span = 0.6 * (along1 ? d1 : d2) * scale;
    return Array.from({ length: count }, (_, i) => {
      const o = -span / 2 + (span * i) / (count - 1);
      return (along1 ? [o, 0] : [0, o]) as [number, number];
    });
  }
  const edge = 1.5 * headR;
  const big1 = d1 >= 0.22 && d1 / 2 - 0.3 * d1 >= edge;
  const big2 = d2 >= 0.22 && d2 / 2 - 0.3 * d2 >= edge;
  const o1 = 0.3 * d1 * scale;
  const o2 = 0.3 * d2 * scale;
  if (big1 && big2) {
    return [
      [-o1, -o2],
      [o1, -o2],
      [-o1, o2],
      [o1, o2],
    ];
  }
  if (big1) return [[-o1, 0], [o1, 0]];
  if (big2) return [[0, -o2], [0, o2]];
  return [[0, 0]];
}

// ---------------------------------------------------------------------------
// Declared connections (the spec's `connections` array)
// ---------------------------------------------------------------------------

function specConnections(spec?: AssetSpec): SpecConnection[] {
  if (!spec) return [];
  return (spec.connections ?? []).filter(
    (c) =>
      typeof c?.a === "string" &&
      typeof c?.b === "string" &&
      CONNECTION_TYPES.has(c.type),
  );
}

function sideMatches(ref: string, p: Primitive): boolean {
  return ref === p.component || ref === `${p.component}/${p.name}`;
}

/** First declaration whose {a, b} matches this prim pair (unordered). */
function findDeclaration(
  decls: SpecConnection[],
  pa: Primitive,
  pb: Primitive,
): SpecConnection | null {
  for (const d of decls) {
    if (d.b === "ground") continue; // handled by the anchor pass
    if (
      (sideMatches(d.a, pa) && sideMatches(d.b, pb)) ||
      (sideMatches(d.a, pb) && sideMatches(d.b, pa))
    ) {
      return d;
    }
  }
  return null;
}

function groundDeclaration(decls: SpecConnection[], p: Primitive): SpecConnection | null {
  for (const d of decls) {
    if (d.b === "ground" && sideMatches(d.a, p)) return d;
    if (d.a === "ground" && sideMatches(d.b, p)) return d;
  }
  return null;
}

/** True when the member is round and its cylinder axis is the bolt axis. */
function roundAboutAxis(p: Primitive, axis: number): boolean {
  if (p.kind !== "cylinder" && p.kind !== "cone" && p.kind !== "tube") return false;
  const u = cylinderAxis(p.rotation);
  return Math.abs(u[axis]) > 0.9;
}

/** Unit direction a member runs along: the cylinder axis for round kinds,
 * the path chord for sweeps, +Z otherwise. */
function memberDirection(p: Primitive): Vec3 {
  if (p.kind === "sweep") {
    const path = p.params.path ?? [];
    if (path.length >= 2) {
      const dx = path[path.length - 1][0] - path[0][0];
      const dy = path[path.length - 1][1] - path[0][1];
      const dz = path[path.length - 1][2] - path[0][2];
      const n = Math.hypot(dx, dy, dz) || 1;
      return [dx / n, dy / n, dz / n];
    }
  }
  if (p.kind === "cylinder" || p.kind === "cone" || p.kind === "tube") {
    return cylinderAxis(p.rotation);
  }
  return [0, 0, 1];
}

/** Post-top slip fit: two coaxial upright pipe-like members telescoping
 * vertically with meaningfully different radii (mirror of hardware.py). */
function isTelescopingFit(pa: Primitive, pb: Primitive, center: Vec3): boolean {
  if (!isUprightRound(pa) || !isUprightRound(pb)) return false;
  for (const p of [pa, pb]) {
    if (p.params.depth! < 2 * roundRadius(p)) return false; // disc, not a pipe
  }
  const ra = radiusAtZ(pa, center[2]);
  const rb = radiusAtZ(pb, center[2]);
  const big = Math.max(ra, rb);
  const small = Math.min(ra, rb);
  if (small <= 0 || (big - small) / big < 0.15) return false;
  const dx = pa.location[0] - pb.location[0];
  const dy = pa.location[1] - pb.location[1];
  return Math.hypot(dx, dy) <= 0.3 * big; // coaxial
}

// ---------------------------------------------------------------------------
// Orchestrator
// ---------------------------------------------------------------------------

interface PairCandidate {
  kind: "pair";
  rank: number;
  key: [number, number, number];
  pa: Primitive;
  pb: Primitive;
  ca: Vec3;
  ha: Vec3;
  cb: Vec3;
  hb: Vec3;
  lo: Vec3;
  hi: Vec3;
  center: Vec3;
  axis: number;
  perp: number[];
  d1: number;
  d2: number;
  decl: SpecConnection | null;
}

interface AnchorCandidate {
  kind: "anchor";
  rank: number;
  key: [number, number, number];
  centerXY: [number, number];
  memberR: number;
  shape: "round" | "square";
  slot: string;
  load: string;
}

type Candidate = PairCandidate | AnchorCandidate;

export function computeHardware(prims: Primitive[], spec?: AssetSpec): Primitive[] {
  const decls = specConnections(spec);
  const boxes = prims
    .filter((p) => p.component !== "hardware" && !p.cut)
    .map((p) => {
      const box = aabb(p);
      return { p, c: box.center, h: box.half };
    });

  const candidates: Candidate[] = [];
  const seen = new Set<string>();

  // -------------------------------------------------- pair contacts
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

      const decl = findDeclaration(decls, a.p, b.p);
      if (decl?.type === "none") continue; // explicitly no visible hardware
      if (!decl && isSoft(a.p.materialSlot, spec) && isSoft(b.p.materialSlot, spec)) {
        continue; // non-structural joint — concealed joinery, no bolts
      }

      const center: Vec3 = [
        (lo[0] + hi[0]) / 2,
        (lo[1] + hi[1]) / 2,
        (lo[2] + hi[2]) / 2,
      ];
      const key = center.map((c) => Math.round(c / GRID)) as [number, number, number];
      const keyStr = key.join(",");
      if (seen.has(keyStr)) continue;
      seen.add(keyStr);

      let axis = 0;
      for (let k = 1; k < 3; k++) if (hi[k] - lo[k] < hi[axis] - lo[axis]) axis = k;
      const perp = [0, 1, 2].filter((k) => k !== axis);
      const d1 = hi[perp[0]] - lo[perp[0]];
      const d2 = hi[perp[1]] - lo[perp[1]];
      if (Math.min(d1, d2) < MIN_FACE) continue;

      candidates.push({
        kind: "pair", rank: decl ? 1 : 2, key,
        pa: a.p, pb: b.p, ca: a.c, ha: a.h, cb: b.c, hb: b.h,
        lo, hi, center, axis, perp, d1, d2, decl,
      });
    }
  }

  // -------------------------------------------------- anchor bases
  const anchorSeen = new Set<string>();
  for (const { p, c, h } of boxes) {
    if (!isVerticalStructural(p)) continue;
    if (c[2] - h[2] > 0.01) continue; // bottom must land at grade
    const gdecl = groundDeclaration(decls, p);
    if (gdecl?.type === "none") continue;
    const forced = gdecl?.type === "anchor_base";
    if (!forced) {
      if (isSoft(p.materialSlot, spec)) continue;
      if (volume(p) < 0.01) continue; // light member — no anchors uninvited
    }
    // a modeled base (any other component at this member's foot) wins
    const footR = Math.max(h[0], h[1]);
    const hasBase = boxes.some(
      ({ p: q, c: qc, h: qh }) =>
        q.component !== p.component &&
        qc[2] + qh[2] <= 0.15 &&
        Math.abs(qc[0] - c[0]) < footR + qh[0] &&
        Math.abs(qc[1] - c[1]) < footR + qh[1],
    );
    if (hasBase) continue;
    const dedupeKey = `${p.component},${Math.round(c[0] / 0.1)},${Math.round(c[1] / 0.1)}`;
    if (anchorSeen.has(dedupeKey)) continue;
    anchorSeen.add(dedupeKey);
    let memberR: number;
    let shape: "round" | "square";
    if (p.kind === "box") {
      memberR = Math.max(p.params.size![0], p.params.size![1]) / 2;
      shape = "square";
    } else {
      memberR = radiusAtZ(p, 0);
      shape = "round";
    }
    candidates.push({
      kind: "anchor", rank: 0,
      key: [Math.round(c[0] / GRID), Math.round(c[1] / GRID), 0],
      centerXY: [c[0], c[1]], memberR, shape, slot: p.materialSlot,
      load: gdecl?.load ?? (volume(p) > 0.15 ? "heavy" : "standard"),
    });
  }

  // -------------------------------------------------- deterministic order
  candidates.sort((x, y) => {
    if (x.rank !== y.rank) return x.rank - y.rank;
    for (let k = 0; k < 3; k++) if (x.key[k] !== y.key[k]) return x.key[k] - y.key[k];
    return 0;
  });

  const out: Primitive[] = [];
  let joint = 0;
  for (const cand of candidates) {
    if (joint >= MAX_JOINTS) break;
    const emitted = dispatch(joint + 1, cand, spec);
    if (emitted.length) {
      joint += 1;
      out.push(...emitted);
    }
  }
  return out;
}

/** Emit one joint's hardware from a candidate record. */
function dispatch(joint: number, cand: Candidate, spec?: AssetSpec): Primitive[] {
  if (cand.kind === "anchor") {
    return groundConnection(
      cand.memberR, "flange", cand.load, "hardware", cand.slot,
      cand.centerXY, cand.shape, `joint${joint}_`,
    );
  }

  const { pa, pb, lo, hi, center, axis, perp, d1, d2, decl, ca, ha, cb, hb } = cand;

  const upright = isUprightRound(pa) ? pa : isUprightRound(pb) ? pb : null;
  const other = upright === pa ? pb : pa;

  // ------------------------------------------------ pick the joint type
  let ctype: string | null = decl ? decl.type : null;
  if (ctype === null) {
    if (isTelescopingFit(pa, pb, center)) {
      ctype = "slip_fit";
    } else if (
      axis !== 2 &&
      upright !== null &&
      !isUprightRound(other) &&
      ROUND_KINDS.has(other.kind)
    ) {
      ctype = "band_clamp";
    } else if (
      axis === 2 &&
      isWood(pa.materialSlot, spec) !== isWood(pb.materialSlot, spec)
    ) {
      ctype = "carriage_bolt";
    } else {
      ctype = "through_bolt";
    }
  }

  // declared types that need geometry they don't have fall back to bolts
  if (ctype === "band_clamp" && upright === null) ctype = "through_bolt";
  if (
    ctype === "slip_fit" &&
    !["cylinder", "cone", "tube"].includes(pa.kind) &&
    !["cylinder", "cone", "tube"].includes(pb.kind)
  ) {
    ctype = "through_bolt";
  }
  if (ctype === "anchor_base") ctype = "through_bolt"; // pair-declared: no grade side

  // ------------------------------------------------ emit
  if (ctype === "weld") {
    const roundM =
      upright ??
      (ROUND_KINDS.has(pa.kind) ? pa : ROUND_KINDS.has(pb.kind) ? pb : null);
    if (roundM === null) return []; // shop weld, nothing visible
    const r =
      ["cylinder", "cone", "tube"].includes(roundM.kind) && isUprightRound(roundM)
        ? radiusAtZ(roundM, center[2])
        : roundRadius(roundM);
    return [
      weldFillet(
        r, Math.max(0.008, r * 0.2), center[2], "hardware", "hardware",
        `joint${joint}_weld`, [roundM.location[0], roundM.location[1]],
      ),
    ];
  }

  if (ctype === "band_clamp") {
    const armR = ROUND_KINDS.has(other.kind) ? roundRadius(other) : Math.min(d1, d2) / 2;
    // ear bolts run along the ARM's horizontal direction (ears sit on the
    // pole's flanks, clear of the arm), not the overlap box's thin axis
    const direction = memberDirection(other);
    const axisH = Math.abs(direction[0]) >= Math.abs(direction[1]) ? 0 : 1;
    return splitBandClamp(
      joint, radiusAtZ(upright!, center[2]),
      [upright!.location[0], upright!.location[1]], center[2],
      axisH, armR,
    );
  }

  if (ctype === "slip_fit") {
    const roundPrims = [pa, pb].filter((p) =>
      ["cylinder", "cone", "tube"].includes(p.kind),
    );
    const outer = roundPrims.reduce((mx, p) => (roundRadius(p) > roundRadius(mx) ? p : mx));
    const outerR = isUprightRound(outer) ? radiusAtZ(outer, center[2]) : roundRadius(outer);
    return slipFitter(joint, outerR, [outer.location[0], outer.location[1]], center[2]);
  }

  if (ctype === "flange_splice") {
    const memberR = Math.min(d1, d2) / 2;
    return flangeSplice(joint, center, axis, memberR, decl?.count ?? 6);
  }

  // bolted family: through / carriage / lag
  const load = decl?.load ?? loadClass(pa, pb);
  const factor = LOAD_FACTOR[load] ?? 1.0;
  const shaftR = Math.min(Math.max(0.22 * Math.min(d1, d2) * factor, 0.004), 0.014);
  const above = Math.max(ca[axis] + ha[axis], cb[axis] + hb[axis]) - hi[axis];
  const below = lo[axis] - Math.min(ca[axis] - ha[axis], cb[axis] - hb[axis]);
  const spanHi = hi[axis] + Math.min(above, EMBED);
  const spanLo = lo[axis] - Math.min(below, EMBED);
  const roundFace = roundAboutAxis(pa, axis) || roundAboutAxis(pb, axis);
  const pattern = boltPattern(d1, d2, 1.8 * shaftR, roundFace, decl?.count);

  const out: Primitive[] = [];
  pattern.forEach(([o1, o2], i) => {
    const c = [...center] as Vec3;
    c[perp[0]] += o1;
    c[perp[1]] += o2;
    if (ctype === "carriage_bolt") {
      const wood = isWood(pa.materialSlot, spec) ? pa : pb;
      const domeAtHi = spec ? wood.location[axis] >= center[axis] : true;
      out.push(
        ...carriageBoltAssembly(joint, i + 1, c, axis, shaftR, spanLo, spanHi, domeAtHi),
      );
    } else if (ctype === "lag_screw") {
      out.push(...lagScrewAssembly(joint, i + 1, c, axis, shaftR, spanLo, spanHi));
    } else {
      out.push(...throughBoltAssembly(joint, i + 1, c, axis, shaftR, spanLo, spanHi));
    }
  });
  return out;
}
