/** Mirror of blender/builders/hardware.py v4 — connection-hardware
 * orchestrator: detects inter-component contacts on the TRANSFORMED
 * geometry (moved parts take their hardware with them), merges contact
 * regions of the same component pair into one joint per physical junction,
 * honors the spec's declared `connections` intent (weld / slip_fit /
 * band_clamp / carriage / flange_splice / lag_screw / through_bolt /
 * anchor_base / none), infers a fabrication-correct type for undeclared
 * joints (including welds for pipes standing on modeled base plates), and
 * dispatches to the emitter library in connections.ts. Joints are collected
 * then ordered deterministically (anchor bases, declared, inferred;
 * position-stable) so ids survive slider nudges. Keep in exact lockstep
 * with the Python implementation. */
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
/** largest same-pair/type repeat the design blesses before thinning kicks
 * in — exactly test_connection_redesign's "3 slats x 2 rails stay separate
 * joints" (6 declared carriage-bolt joints between one pair). Groups of 6
 * or fewer are left untouched; only genuinely large swarms (a 7-rafter
 * pergola's 14 lag-screw joints) get thinned down to 6 representative
 * joints. Mirror of hardware.py MAX_REPEAT_PER_GROUP. See
 * thinRepeatGroups. */
const MAX_REPEAT_PER_GROUP = 6;
const EMBED = 0.025;
const MIN_FACE = 0.01;
const GRID = 0.06;

const CONNECTION_TYPES = new Set([
  "anchor_base", "through_bolt", "flange_splice", "band_clamp",
  "slip_fit", "weld", "carriage_bolt", "lag_screw", "none",
]);

/** Blender-parity Euler XYZ rotation matrix (mirror of hardware.py):
 * R = Rz·Ry·Rx, X applied first about fixed axes — how Blender interprets
 * `rotation_euler` and how the preview renders (Three Euler order 'ZYX'). */
export function eulerXyzMatrix(rot: Vec3): number[][] {
  const [x, y, z] = rot;
  const c1 = Math.cos(x), s1 = Math.sin(x);
  const c2 = Math.cos(y), s2 = Math.sin(y);
  const c3 = Math.cos(z), s3 = Math.sin(z);
  return [
    [c3 * c2, c3 * s2 * s1 - s3 * c1, c3 * s2 * c1 + s3 * s1],
    [s3 * c2, s3 * s2 * s1 + c3 * c1, s3 * s2 * c1 - c3 * s1],
    [-s2, c2 * s1, c2 * c1],
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

/** Hollow SQUARE stock (HSS) — a tube whose section is square. It is still
 * an upright structural member (same load path, same anchor base), but
 * nothing round-section wraps it: no band clamp, no slip fitter, and its
 * base plate is square. */
function isSquareTube(p: Primitive): boolean {
  return p.kind === "tube" && p.params.section === "square";
}

/** True when the member presents a ROUND cross-section — the thing a band
 * clamp or slip fitter needs. Square stock is deliberately excluded even
 * though it is otherwise a perfectly good upright member. */
function isRoundSection(p: Primitive): boolean {
  return ROUND_KINDS.has(p.kind) && !isSquareTube(p);
}

function isUprightRound(p: Primitive): boolean {
  return (
    (p.kind === "cylinder" || p.kind === "cone" || p.kind === "tube") &&
    p.rotation.every((a) => Math.abs(a) < 1e-3)
  );
}

/** An upright round member meaningfully taller than wide — a pole/post,
 * not a flange disc or a grout pad. */
function isPipeLike(p: Primitive): boolean {
  return isUprightRound(p) && p.params.depth! >= 2 * roundRadius(p);
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

/** C6: fastener sizing scales with the connection's tributary load tier. */
const LOAD_FACTOR: Record<string, number> = { light: 0.75, standard: 1.0, heavy: 1.35 };

/** Metric fastener catalog: name -> shaft radius (m). Mirror of
 * hardware.py BOLT_CATALOG (source of truth: us_codes.json _connections). */
const BOLT_CATALOG: Array<[string, number]> = [
  ["M6", 0.003], ["M8", 0.004], ["M10", 0.005], ["M12", 0.006],
  ["M16", 0.008], ["M20", 0.01], ["M24", 0.012],
];

/** Seating torque (N·m) for slip-fitter set screws by catalog size — cup
 * points bearing on a pole tenon, NOT the bolted-joint torque table. Mirror
 * of hardware.py SET_SCREW_TORQUE (human copy: us_codes.json
 * `_connections.set_screw_torque_nm`). */
const SET_SCREW_TORQUE: Record<string, number> = { M6: 8, M8: 15, M10: 30, M12: 50 };

/** rough densities (t/m³) for the moment proxy, keyed by material family */
const DENSITY = { metal: 7.9, concrete: 2.4, wood: 0.6, other: 1.0 };

function snapBolt(r: number): [string, number] {
  return BOLT_CATALOG.reduce((best, entry) =>
    Math.abs(entry[1] - r) < Math.abs(best[1] - r) ? entry : best,
  );
}

function volume(p: Primitive): number {
  const h = aabb(p).half;
  return 8 * h[0] * h[1] * h[2];
}

function density(slot: string, spec?: AssetSpec): number {
  const preset = presetName(slot, spec);
  if (preset === null || METAL_PRESETS.has(preset)) return DENSITY.metal;
  if (preset === "concrete") return DENSITY.concrete;
  if (WOOD_PRESETS.has(preset)) return DENSITY.wood;
  return DENSITY.other;
}

/** Tributary-moment proxy for the joint: each member's bounding mass times
 * its horizontal lever arm about the joint; the larger governs (mirror of
 * hardware.py _moment — heuristic fabrication convention, not FEA). */
function jointMoment(pa: Primitive, pb: Primitive, center: Vec3, spec?: AssetSpec): number {
  let best = 0;
  for (const p of [pa, pb]) {
    const c = aabb(p).center;
    const lever = Math.max(0.05, Math.hypot(c[0] - center[0], c[1] - center[1]));
    best = Math.max(best, volume(p) * density(p.materialSlot, spec) * lever);
  }
  return best;
}

function loadClassFromMoment(moment: number): string {
  if (moment > 0.12) return "heavy";
  if (moment < 0.004) return "light";
  return "standard";
}

/** volume thresholds (m³) for an AUTO anchor's load tier (a declared `load`
 * on the ground connection always wins) — the 0.15 heavy cutoff matches
 * this pass's pre-existing volume>0.15 heavy check; light is new. Genuinely
 * small members get a LEAN anchor (plate + grout + bolts, no gusset webs —
 * see connections.ts groundConnection's `gussets` param); only HEAVY
 * members earn the full gusseted package. Mirror of hardware.py
 * ANCHOR_LIGHT_VOLUME / ANCHOR_HEAVY_VOLUME. */
const ANCHOR_LIGHT_VOLUME = 0.03;
const ANCHOR_HEAVY_VOLUME = 0.15;

function anchorLoadClass(vol: number): string {
  if (vol > ANCHOR_HEAVY_VOLUME) return "heavy";
  if (vol < ANCHOR_LIGHT_VOLUME) return "light";
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

/** Map a duplicated component's name to its ultimate source component, from
 * spec.edits.duplicates ([{source, name}]), resolved transitively — a
 * duplicate of a duplicate resolves all the way to the root source.
 * Declared-connection matching resolves each side's component through this
 * map first, so a copy inherits the joint intent declared for the component
 * it was cloned from (apply_structure/edits.ts run duplicates before
 * hardware for exactly this reason). Mirror of hardware.py
 * _dup_resolution_map. Cycles are broken defensively: a name caught in a
 * cycle resolves to itself. */
function dupResolutionMap(spec?: AssetSpec): Map<string, string> {
  const raw = new Map<string, string>();
  for (const dup of spec?.edits?.duplicates ?? []) {
    if (typeof dup?.name === "string" && typeof dup?.source === "string") {
      raw.set(dup.name, dup.source);
    }
  }
  const resolved = new Map<string, string>();
  function resolve(name: string, seen: Set<string>): string {
    if (resolved.has(name)) return resolved.get(name)!;
    if (!raw.has(name) || seen.has(name)) return name;
    seen.add(name);
    return resolve(raw.get(name)!, seen);
  }
  for (const name of raw.keys()) {
    resolved.set(name, resolve(name, new Set()));
  }
  return resolved;
}

function resolveComponent(name: string, dupMap: Map<string, string>): string {
  return dupMap.get(name) ?? name;
}

function sideMatches(ref: string, p: Primitive, dupMap: Map<string, string>): boolean {
  const comp = resolveComponent(p.component, dupMap);
  return ref === comp || ref === `${comp}/${p.name}`;
}

/** First declaration whose {a, b} matches this prim pair (unordered),
 * matching through duplicate resolution so a copy inherits its source's
 * declared intent. A pair that resolves to the same component on both
 * sides (a copy touching its own source) declares nothing new — it falls
 * through to inference. */
function findDeclaration(
  decls: SpecConnection[],
  pa: Primitive,
  pb: Primitive,
  dupMap: Map<string, string>,
): SpecConnection | null {
  if (resolveComponent(pa.component, dupMap) === resolveComponent(pb.component, dupMap)) {
    return null;
  }
  for (const d of decls) {
    if (d.b === "ground") continue; // handled by the anchor pass
    if (
      (sideMatches(d.a, pa, dupMap) && sideMatches(d.b, pb, dupMap)) ||
      (sideMatches(d.a, pb, dupMap) && sideMatches(d.b, pa, dupMap))
    ) {
      return d;
    }
  }
  return null;
}

function groundDeclaration(
  decls: SpecConnection[],
  p: Primitive,
  dupMap: Map<string, string>,
): SpecConnection | null {
  for (const d of decls) {
    if (d.b === "ground" && sideMatches(d.a, p, dupMap)) return d;
    if (d.a === "ground" && sideMatches(d.b, p, dupMap)) return d;
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
  moment: number;
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
  declIdx: number;
  /** top (z) of the junction's full-face contacts, set by the merge pass */
  junctionTop?: number;
}

/** contact regions closer than this (m) belong to the same physical junction */
const MERGE_TOL = 0.005;

/** Euclidean gap between two candidates' contact boxes (0 = touching). */
function contactGap(a: PairCandidate, b: PairCandidate): number {
  let d2 = 0;
  for (let k = 0; k < 3; k++) {
    const g = Math.max(a.lo[k] - b.hi[k], b.lo[k] - a.hi[k], 0);
    if (g > 0) d2 += g * g;
  }
  return Math.sqrt(d2);
}

/** Which contact represents a merged junction: larger joint face, then
 * deeper overlap, then the smaller (stable) grid key. */
function betterCandidate(c1: PairCandidate, c2: PairCandidate): boolean {
  const a1 = c1.d1 * c1.d2;
  const a2 = c2.d1 * c2.d2;
  if (a1 !== a2) return a1 > a2;
  const o1 = c1.hi[c1.axis] - c1.lo[c1.axis];
  const o2 = c2.hi[c2.axis] - c2.lo[c2.axis];
  if (o1 !== o2) return o1 > o2;
  for (let k = 0; k < 3; k++) if (c1.key[k] !== c2.key[k]) return c1.key[k] < c2.key[k];
  return false;
}

/** One physical junction -> one joint. A pole meeting its base plate
 * touches the grout pad, the flange, AND every gusset — six AABB contacts
 * that are ONE junction to a fabricator (the old code bolted each of them,
 * which is where horizontal bolts through poles came from). Candidates for
 * the same component pair and same declaration whose contact boxes touch
 * collapse into the best-faced one; genuinely separate contact regions
 * (three bench slats along a rail) keep their own joints. Mirror of
 * hardware.py _merge_pair_candidates. */
function mergePairCandidates(cands: PairCandidate[]): PairCandidate[] {
  const groups = new Map<string, PairCandidate[]>();
  for (const c of cands) {
    const key = `${[c.pa.component, c.pb.component].sort().join("~")}#${c.declIdx}`;
    const arr = groups.get(key);
    if (arr) arr.push(c);
    else groups.set(key, [c]);
  }
  const out: PairCandidate[] = [];
  for (const arr of groups.values()) {
    const remaining = arr.map((_, i) => i);
    while (remaining.length) {
      const blob = [remaining.shift()!];
      let grew = true;
      while (grew) {
        grew = false;
        for (const i of [...remaining]) {
          if (blob.some((j) => contactGap(arr[i], arr[j]) <= MERGE_TOL)) {
            remaining.splice(remaining.indexOf(i), 1);
            blob.push(i);
            grew = true;
          }
        }
      }
      let best = blob[0];
      for (const i of blob.slice(1)) {
        if (betterCandidate(arr[i], arr[best])) best = i;
      }
      const winner = arr[best];
      // where the junction's full-face contact tops out — a pipe welded
      // into a stack of base discs carries its bead at the seam where it
      // exits the TOPMOST disc, not the first one it touches (small side
      // contacts like gusset slivers don't count)
      const face = winner.d1 * winner.d2;
      winner.junctionTop = Math.max(
        ...blob
          .filter((i) => arr[i].d1 * arr[i].d2 >= 0.8 * face)
          .map((i) => arr[i].hi[2]),
      );
      out.push(winner);
    }
  }
  return out;
}

interface AnchorCandidate {
  kind: "anchor";
  rank: number;
  key: [number, number, number];
  centerXY: [number, number];
  memberR: number;
  shape: "round" | "square";
  slot: string;
  component: string;
  load: string;
}

type Candidate = PairCandidate | AnchorCandidate;

/** Cap repeated identical joints: PAIR candidates sharing the same
 * (unordered resolved component pair, connection type) are capped at
 * MAX_REPEAT_PER_GROUP. `candidates` is already sorted by (rank, key) by
 * the caller, so walking it in order and keeping each group's first N is
 * position-stable — a 7-rafter pergola's 14 identical lag-screw joints
 * thin to a representative 6, while a 6-joint declared group (3 slats x 2
 * rails) is left exactly as-is. ANCHOR candidates never pass through
 * here — a structure's feet are never thinned by count. Mirror of
 * hardware.py _thin_repeat_groups. */
function thinRepeatGroups(candidates: Candidate[], dupMap: Map<string, string>): Candidate[] {
  const counts = new Map<string, number>();
  const out: Candidate[] = [];
  for (const cand of candidates) {
    if (cand.kind !== "pair") {
      out.push(cand);
      continue;
    }
    const paComp = resolveComponent(cand.pa.component, dupMap);
    const pbComp = resolveComponent(cand.pb.component, dupMap);
    const ctype = cand.decl ? cand.decl.type : "";
    const groupKey = `${[paComp, pbComp].sort().join("~")}#${ctype}`;
    const n = counts.get(groupKey) ?? 0;
    if (n >= MAX_REPEAT_PER_GROUP) continue;
    counts.set(groupKey, n + 1);
    out.push(cand);
  }
  return out;
}

export function computeHardware(prims: Primitive[], spec?: AssetSpec): Primitive[] {
  const decls = specConnections(spec);
  const dupMap = dupResolutionMap(spec);
  const boxes = prims
    .filter((p) => p.component !== "hardware" && !p.cut)
    .map((p) => {
      const box = aabb(p);
      return { p, c: box.center, h: box.half };
    });

  const pairCands: PairCandidate[] = [];
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

      const decl = findDeclaration(decls, a.p, b.p, dupMap);
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

      // structural (high-moment) inferred joints outrank light ones so the
      // MAX_JOINTS budget never drops a mast arm for a trim strip
      const moment = jointMoment(a.p, b.p, center, spec);
      const rank = decl ? 1 : moment >= 0.01 ? 2 : 3;
      pairCands.push({
        kind: "pair", rank, key, moment,
        pa: a.p, pb: b.p, ca: a.c, ha: a.h, cb: b.c, hb: b.h,
        lo, hi, center, axis, perp, d1, d2, decl,
        declIdx: decl ? decls.indexOf(decl) : -1,
      });
    }
  }

  // one joint per physical junction (see mergePairCandidates)
  const candidates: Candidate[] = mergePairCandidates(pairCands);

  // -------------------------------------------------- anchor bases
  const anchorSeen = new Set<string>();
  for (const { p, c, h } of boxes) {
    if (!isVerticalStructural(p)) continue;
    if (c[2] - h[2] > 0.01) continue; // bottom must land at grade
    const gdecl = groundDeclaration(decls, p, dupMap);
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
    } else if (isSquareTube(p)) {
      memberR = p.params.radius!; // half-width across flats
      shape = "square";
    } else {
      memberR = radiusAtZ(p, 0);
      shape = "round";
    }
    candidates.push({
      kind: "anchor", rank: 0,
      key: [Math.round(c[0] / GRID), Math.round(c[1] / GRID), 0],
      centerXY: [c[0], c[1]], memberR, shape, slot: p.materialSlot,
      component: p.component,
      load: gdecl?.load ?? anchorLoadClass(volume(p)),
    });
  }

  // -------------------------------------------------- deterministic order
  candidates.sort((x, y) => {
    if (x.rank !== y.rank) return x.rank - y.rank;
    for (let k = 0; k < 3; k++) if (x.key[k] !== y.key[k]) return x.key[k] - y.key[k];
    return 0;
  });

  // thin repeated same-pair/type joints down to a representative handful
  // (anchors exempt — see thinRepeatGroups) before the budget loop
  const thinned = thinRepeatGroups(candidates, dupMap);

  const out: Primitive[] = [];
  let joint = 0;
  for (const cand of thinned) {
    if (joint >= MAX_JOINTS) break;
    const emitted = dispatch(joint + 1, cand, spec);
    if (emitted.length) {
      joint += 1;
      out.push(...emitted);
    }
  }
  return out;
}

/** heuristic anchor-rod torque per tier (see us_codes.json _connections) */
const ANCHOR_TORQUE: Record<string, number> = { light: 100, standard: 220, heavy: 400 };

const CATALOG_TORQUE: Record<string, number> = {
  M6: 10, M8: 25, M10: 50, M12: 85, M16: 210, M20: 425, M24: 730,
};

function catalogRow(name: string): { grade: string; code_ref: string; torque_nm: number } {
  return {
    grade: "8.8 / A325",
    code_ref: "AISC J3 / RCSC Table 8.1",
    torque_nm: CATALOG_TORQUE[name],
  };
}

/** Attach the joint record to the first emitted prim (mirror of
 * hardware.py _with_joint_meta) so the schedule can cite what was built. */
function withJointMeta(emitted: Primitive[], record: Record<string, unknown>): Primitive[] {
  if (!emitted.length) return emitted;
  return [{ ...emitted[0], meta: { joint: record } }, ...emitted.slice(1)];
}

/** Emit one joint's hardware from a candidate record. */
function dispatch(joint: number, cand: Candidate, spec?: AssetSpec): Primitive[] {
  if (cand.kind === "anchor") {
    const nBolts = { light: 4, standard: 4, heavy: 6 }[cand.load] ?? 4;
    const diaMm = { light: 16, standard: 22, heavy: 28 }[cand.load] ?? 22;
    return withJointMeta(
      groundConnection(
        cand.memberR, "flange", cand.load, "hardware", cand.slot,
        cand.centerXY, cand.shape, `joint${joint}_`,
        cand.load === "heavy",
      ),
      {
        id: joint, type: "anchor_base", a: cand.component, b: "ground",
        fastener: `${diaMm}mm anchor bolt`, count: nBolts,
        grade: "F1554 Gr.55", torque_nm: ANCHOR_TORQUE[cand.load],
        code_ref: "AASHTO LTS-6 / ACI 318-19 Ch.17",
        center: [cand.centerXY[0], cand.centerXY[1], 0],
      },
    );
  }

  const { pa, pb, lo, hi, center, axis, perp, d1, d2, decl, ca, ha, cb, hb } = cand;

  const upright = isUprightRound(pa) ? pa : isUprightRound(pb) ? pb : null;
  const other = upright === pa ? pb : pa;

  // ------------------------------------------------ pick the joint type
  const pipe = isPipeLike(pa) ? pa : isPipeLike(pb) ? pb : null;
  let ctype: string | null = decl ? decl.type : null;
  if (ctype === null) {
    const [baseC, baseH] = pipe === pa ? [cb, hb] : [ca, ha];
    if (isTelescopingFit(pa, pb, center)) {
      ctype = "slip_fit";
    } else if (
      axis !== 2 &&
      upright !== null &&
      !isUprightRound(other) &&
      isRoundSection(other)
    ) {
      ctype = "band_clamp";
    } else if (axis === 2 && pipe !== null && baseC[2] + baseH[2] <= 0.15) {
      // a standing pipe on a modeled base plate at grade is shop-welded
      // into it — never bolted down its own axis
      ctype = "weld";
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

  // geometric context (center, bolt axis, overlap depth, face size) rides
  // along so the connection auditor can verify the joint without re-deriving
  // contact detection
  const record = (
    fastener: string,
    count: number,
    grade = "",
    torque: number | null = null,
    codeRef = "",
  ): Record<string, unknown> => ({
    id: joint, type: ctype, a: pa.component, b: pb.component,
    fastener, count, grade, torque_nm: torque, code_ref: codeRef,
    center: [...center], axis, overlap: hi[axis] - lo[axis], face: [d1, d2],
  });

  // ------------------------------------------------ emit
  if (ctype === "weld") {
    const roundM =
      upright ??
      (ROUND_KINDS.has(pa.kind) ? pa : ROUND_KINDS.has(pb.kind) ? pb : null);
    if (roundM === null) return []; // shop weld, nothing visible
    // a vertical member welded into a lower part carries the bead at the
    // seam where it exits that part, not at the overlap's midpoint
    let weldZ = center[2];
    if (axis === 2) {
      const seam = cand.junctionTop ?? hi[2];
      const [rc, rh] = roundM === pa ? [ca, ha] : [cb, hb];
      if (rc[2] + rh[2] > seam + 0.01) weldZ = seam;
    }
    const r =
      ["cylinder", "cone", "tube"].includes(roundM.kind) && isUprightRound(roundM)
        ? radiusAtZ(roundM, weldZ)
        : roundRadius(roundM);
    return withJointMeta(
      [
        weldFillet(
          r, Math.max(0.008, r * 0.2), weldZ, "hardware", "hardware",
          `joint${joint}_weld`, [roundM.location[0], roundM.location[1]],
        ),
      ],
      record("fillet weld", 1, "E70XX", null, "AWS D1.1 (heuristic)"),
    );
  }

  if (ctype === "band_clamp") {
    const armR = ROUND_KINDS.has(other.kind) ? roundRadius(other) : Math.min(d1, d2) / 2;
    // ear bolts run along the ARM's horizontal direction (ears sit on the
    // pole's flanks, clear of the arm), not the overlap box's thin axis
    const direction = memberDirection(other);
    const axisH = Math.abs(direction[0]) >= Math.abs(direction[1]) ? 0 : 1;
    const [earName, earR] = snapBolt(Math.min(Math.max(0.4 * armR, 0.004), 0.008));
    const row = catalogRow(earName);
    return withJointMeta(
      splitBandClamp(
        joint, radiusAtZ(upright!, center[2]),
        [upright!.location[0], upright!.location[1]], center[2],
        axisH, armR, earR,
      ),
      record(`${earName} ear bolt (split band clamp)`, 2, row.grade,
             row.torque_nm, row.code_ref),
    );
  }

  if (ctype === "slip_fit") {
    const roundPrims = [pa, pb].filter((p) =>
      ["cylinder", "cone", "tube"].includes(p.kind),
    );
    const outer = roundPrims.reduce((mx, p) => (roundRadius(p) > roundRadius(mx) ? p : mx));
    const outerR = isUprightRound(outer) ? radiusAtZ(outer, center[2]) : roundRadius(outer);
    // C6: set screws scale with the fit and snap to the catalog — an M8
    // that suits a handrail tenon would rattle loose in a 5-inch fitter
    const [screwName, screwR] = snapBolt(Math.min(Math.max(0.16 * outerR, 0.003), 0.006));
    return withJointMeta(
      slipFitter(joint, outerR, [outer.location[0], outer.location[1]], center[2], screwR),
      record(`${screwName} set screw (slip fitter)`, 3, "45H",
             SET_SCREW_TORQUE[screwName],
             "pole-fitter convention (heuristic)"),
    );
  }

  if (ctype === "flange_splice") {
    const memberR = Math.min(d1, d2) / 2;
    const nBolts = decl?.count ?? 6;
    const [spliceName, spliceR] = snapBolt(Math.min(Math.max(0.35 * memberR, 0.005), 0.012));
    const row = catalogRow(spliceName);
    return withJointMeta(
      flangeSplice(joint, center, axis, memberR, nBolts, spliceR),
      record(`${spliceName} flange bolt`, nBolts, row.grade, row.torque_nm,
             row.code_ref),
    );
  }

  // bolted family: through / carriage / lag — snapped to the catalog
  const load =
    decl?.load ?? loadClassFromMoment(cand.kind === "pair" ? cand.moment : 0);
  const factor = LOAD_FACTOR[load] ?? 1.0;
  const [boltName, shaftR] = snapBolt(
    Math.min(Math.max(0.22 * Math.min(d1, d2) * factor, 0.004), 0.012),
  );
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
  const row = catalogRow(boltName);
  const label =
    ctype === "carriage_bolt" ? "carriage bolt"
    : ctype === "lag_screw" ? "lag screw"
    : "through bolt";
  return withJointMeta(
    out,
    record(`${boltName} ${label}`, pattern.length, row.grade, row.torque_nm,
           row.code_ref),
  );
}
