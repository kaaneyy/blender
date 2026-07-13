/** Deterministic connection auditor — the machine behind the app's
 * "Check connections" button. Mirror of blender/builders/audit.py — keep in
 * exact lockstep.
 *
 * Recomputes the asset with connection hardware forced on, then walks every
 * joint and component the way a fabricator checks a shop drawing:
 * floating components, below-grade geometry, dead/gapped declarations,
 * sliver joints (bolts clamping a knife edge), hardware sticking out into
 * thin air, hardware buried inside unrelated components, and shade/canopy
 * components whose footprint doesn't actually cover the seating below (a
 * component-name heuristic — contact checks alone can't catch a canopy that
 * touches its posts perfectly but sits beside the bench instead of over it).
 *
 * Every finding carries a machine-applicable fix — a small list of data ops
 * on the spec (declare/undeclare a connection, nudge a component's offset) —
 * plus human-readable before/after lines for the UI's hover preview.
 * Nothing is applied here: applyAuditFixes() runs only after the user
 * confirms. */
import type { AssetSpec, ConnectionType, Primitive, Vec3 } from "../types";
import { computePrimitives, specToggles } from "./base";
import { aabb, type Aabb } from "./hardware";

/** parts closer than this (m) count as touching (matches connectivity.py) */
const CONTACT_TOL = 0.0005;
/** a component whose lowest point is within this of z=0 is grounded */
const GROUND_TOL = 0.005;
/** fixes make parts interpenetrate by this much (mid of the 10-20mm rule) */
const EMBED_FIX = 0.014;
/** bolted joints thinner than this clamp on a knife edge */
const SLIVER = 0.006;
/** hardware attachment tolerance (m) */
const ATTACH_TOL = 0.002;
/** the ground, as a box whose top face is grade */
const GROUND_BOX: Aabb = { center: [0, 0, -0.5], half: [1e9, 1e9, 0.5 + CONTACT_TOL] };

const BOLTED_TYPES = new Set(["through_bolt", "carriage_bolt", "lag_screw"]);

/** component-name keyword heuristic for the shade-coverage check below —
 * best-effort, not a fixed vocabulary (AI-authored component names vary). */
const SHADE_KEYWORDS = ["canopy", "roof", "awning", "shade", "sail", "cover"];
const SEATING_KEYWORDS = ["seat", "bench", "table"];
/** minimum fraction of the seating footprint a shade component must cover */
const MIN_SHADE_COVERAGE = 0.5;
/** how far a shade's lowest point may dip below the seat's highest point and
 * still count as "overhead" (a sloped panel's low edge, a mounting bracket)
 * rather than a side panel that isn't a roof at all */
const SHADE_CLEARANCE_TOL = 0.02;

export type FixOp =
  | { op: "nudge"; key: string; delta: Vec3 }
  | { op: "declare"; a: string; b: string; type: ConnectionType }
  | { op: "undeclare"; a: string; b: string };

export interface AuditFix {
  summary: string;
  before: string;
  after: string;
  ops: FixOp[];
}

export interface AuditFinding {
  id: string;
  severity: "error" | "warning";
  kind: string;
  title: string;
  detail: string;
  component: string | null;
  joint: number | null;
  fix: AuditFix | null;
}

export interface AuditReport {
  findings: AuditFinding[];
  joints: number;
  components: number;
}

interface JointRecord {
  id: number;
  type: string;
  a: string;
  b: string;
  overlap?: number;
  axis?: number;
}

const mm = (v: number) => Math.round(v * 1000);

function gap(a: Aabb, b: Aabb): number {
  let d2 = 0;
  for (let k = 0; k < 3; k++) {
    const g = Math.abs(a.center[k] - b.center[k]) - (a.half[k] + b.half[k]);
    if (g > 0) d2 += g * g;
  }
  return Math.sqrt(d2);
}

function intersects(a: Aabb, b: Aabb, tol = 0): boolean {
  for (let k = 0; k < 3; k++) {
    if (Math.abs(a.center[k] - b.center[k]) > a.half[k] + b.half[k] + tol) return false;
  }
  return true;
}

function overlapVolume(a: Aabb, b: Aabb): number {
  let v = 1;
  for (let k = 0; k < 3; k++) {
    const o =
      Math.min(a.center[k] + a.half[k], b.center[k] + b.half[k]) -
      Math.max(a.center[k] - a.half[k], b.center[k] - b.half[k]);
    if (o <= 0) return 0;
    v *= o;
  }
  return v;
}

const volume = (b: Aabb) => 8 * b.half[0] * b.half[1] * b.half[2];

function componentGap(a: Aabb[], b: Aabb[]): number {
  let best = Infinity;
  for (const x of a) for (const y of b) best = Math.min(best, gap(x, y));
  return best;
}

function matchesAny(name: string, keywords: string[]): boolean {
  const low = name.toLowerCase();
  return keywords.some((k) => low.includes(k));
}

/** (minX, maxX, minY, maxY) of a component's combined XY footprint — the
 * outer bounding rectangle across all its primitive boxes. */
function xyRect(boxes: Aabb[]): [number, number, number, number] {
  const xsLo = Math.min(...boxes.map((b) => b.center[0] - b.half[0]));
  const xsHi = Math.max(...boxes.map((b) => b.center[0] + b.half[0]));
  const ysLo = Math.min(...boxes.map((b) => b.center[1] - b.half[1]));
  const ysHi = Math.max(...boxes.map((b) => b.center[1] + b.half[1]));
  return [xsLo, xsHi, ysLo, ysHi];
}

function zRange(boxes: Aabb[]): [number, number] {
  const lo = Math.min(...boxes.map((b) => b.center[2] - b.half[2]));
  const hi = Math.max(...boxes.map((b) => b.center[2] + b.half[2]));
  return [lo, hi];
}

/** Fraction of the seat rect's area covered by the shade rect's area. */
function rectOverlapFraction(
  shadeRect: [number, number, number, number],
  seatRect: [number, number, number, number],
): number {
  const [sx0, sx1, sy0, sy1] = shadeRect;
  const [qx0, qx1, qy0, qy1] = seatRect;
  const ox = Math.max(0, Math.min(sx1, qx1) - Math.max(sx0, qx0));
  const oy = Math.max(0, Math.min(sy1, qy1) - Math.max(sy0, qy0));
  const seatArea = Math.max(qx1 - qx0, 1e-9) * Math.max(qy1 - qy0, 1e-9);
  return (ox * oy) / seatArea;
}

/** Offset that moves `boxesMove` toward `boxesTarget` so the closest box
 * pair interpenetrates EMBED_FIX on every currently-separating axis. */
function closingDelta(boxesMove: Aabb[], boxesTarget: Aabb[]): Vec3 {
  let best: [Aabb, Aabb] | null = null;
  let bestD = Infinity;
  for (const a of boxesMove) {
    for (const b of boxesTarget) {
      const d = gap(a, b);
      if (d < bestD) {
        bestD = d;
        best = [a, b];
      }
    }
  }
  const [a, b] = best!;
  const delta: Vec3 = [0, 0, 0];
  for (let k = 0; k < 3; k++) {
    const g = Math.abs(a.center[k] - b.center[k]) - (a.half[k] + b.half[k]);
    if (g > -EMBED_FIX) {
      const sign = b.center[k] >= a.center[k] ? 1 : -1;
      delta[k] = sign * (g + EMBED_FIX);
    }
  }
  return delta;
}

function fmtDelta(delta: Vec3): string {
  const axes = "xyz";
  const parts: string[] = [];
  for (let k = 0; k < 3; k++) {
    if (Math.abs(delta[k]) > 1e-9) {
      parts.push(`${axes[k]} ${delta[k] >= 0 ? "+" : "−"}${mm(Math.abs(delta[k]))}mm`);
    }
  }
  return parts.length ? parts.join(", ") : "no move";
}

function withHardware(spec: AssetSpec): AssetSpec {
  const s = structuredClone(spec);
  const toggles = s.toggles ?? (s.toggles = []);
  const t = toggles.find((x) => x.id === "connection_hardware");
  if (t) t.value = true;
  else toggles.push({ id: "connection_hardware", label: "Connection Hardware", value: true });
  return s;
}

function declaredPair(spec: AssetSpec, a: string, b: string) {
  return (spec.connections ?? []).find(
    (c) => (c.a === a && c.b === b) || (c.a === b && c.b === a),
  );
}

function finding(
  id: string,
  severity: "error" | "warning",
  kind: string,
  title: string,
  detail: string,
  component: string | null = null,
  joint: number | null = null,
  fix: AuditFix | null = null,
): AuditFinding {
  return { id, severity, kind, title, detail, component, joint, fix };
}

/** Run every check; findings ordered errors-first, position-stable. */
export function auditConnections(spec: AssetSpec): AuditReport {
  const findings: AuditFinding[] = [];
  let prims: Primitive[];
  try {
    prims = computePrimitives(withHardware(spec));
  } catch (e) {
    return {
      findings: [
        finding(
          "build:error", "error", "build",
          "The asset does not build",
          `Geometry computation failed: ${e instanceof Error ? e.message : String(e)}`,
        ),
      ],
      joints: 0,
      components: 0,
    };
  }

  const members = prims.filter((p) => p.component !== "hardware" && !p.cut);
  const hardware = prims.filter((p) => p.component === "hardware");

  const memberBoxes = new Map<string, Aabb[]>();
  for (const p of members) {
    const arr = memberBoxes.get(p.component);
    if (arr) arr.push(aabb(p));
    else memberBoxes.set(p.component, [aabb(p)]);
  }
  const comps = [...memberBoxes.keys()].sort();

  // hardware grouped by joint, with each joint's record
  const jointPrims = new Map<number, Array<{ p: Primitive; box: Aabb }>>();
  const jointRecords = new Map<number, JointRecord>();
  for (const p of hardware) {
    const m = p.name.match(/^joint(\d+)_/);
    if (!m) continue;
    const jid = parseInt(m[1], 10);
    const arr = jointPrims.get(jid);
    if (arr) arr.push({ p, box: aabb(p) });
    else jointPrims.set(jid, [{ p, box: aabb(p) }]);
    const rec = p.meta?.joint as JointRecord | undefined;
    if (rec) jointRecords.set(jid, rec);
  }

  // one positional fix per component per audit round: stacking two nudges
  // computed against the same starting position would overshoot. Later
  // findings keep reporting but defer their fix to the next re-check.
  const nudged = new Set<string>();

  // ---------------------------------------------------------- load paths
  const touching = new Map<string, Set<string>>(comps.map((c) => [c, new Set()]));
  for (let i = 0; i < comps.length; i++) {
    for (let j = i + 1; j < comps.length; j++) {
      if (componentGap(memberBoxes.get(comps[i])!, memberBoxes.get(comps[j])!) <= CONTACT_TOL) {
        touching.get(comps[i])!.add(comps[j]);
        touching.get(comps[j])!.add(comps[i]);
      }
    }
  }
  const grounded = comps.filter((c) =>
    memberBoxes.get(c)!.some((b) => b.center[2] - b.half[2] <= GROUND_TOL),
  );
  const reached = new Set(grounded);
  const frontier = [...grounded];
  while (frontier.length) {
    for (const nxt of touching.get(frontier.pop()!)!) {
      if (!reached.has(nxt)) {
        reached.add(nxt);
        frontier.push(nxt);
      }
    }
  }

  for (const c of comps) {
    if (reached.has(c)) continue;
    const supported = comps.filter((s) => reached.has(s));
    if (supported.length) {
      const nearest = supported.reduce((best, s) =>
        componentGap(memberBoxes.get(c)!, memberBoxes.get(s)!) <
        componentGap(memberBoxes.get(c)!, memberBoxes.get(best)!)
          ? s
          : best,
      );
      const gapMm = mm(componentGap(memberBoxes.get(c)!, memberBoxes.get(nearest)!));
      const delta = closingDelta(memberBoxes.get(c)!, memberBoxes.get(nearest)!);
      nudged.add(c);
      findings.push(
        finding(
          `floating:${c}`, "error", "floating",
          `'${c}' floats in mid-air`,
          `It has no load path to the ground — its nearest support ` +
            `'${nearest}' is ${gapMm}mm away. Nothing holds it up, so it ` +
            `can't be built.`,
          c, null,
          {
            summary: `Move '${c}' onto '${nearest}' so they overlap ${mm(EMBED_FIX)}mm`,
            before: `'${c}' hangs ${gapMm}mm from '${nearest}'`,
            after: `'${c}' seats into '${nearest}' (${fmtDelta(delta)})`,
            ops: [{ op: "nudge", key: c, delta }],
          },
        ),
      );
    } else {
      findings.push(
        finding(
          `floating:${c}`, "error", "floating",
          `'${c}' floats in mid-air`,
          "No component reaches the ground at all — nothing can carry load to grade.",
          c,
        ),
      );
    }
  }

  // ---------------------------------------------------------- below grade
  for (const c of comps) {
    const low = Math.min(...memberBoxes.get(c)!.map((b) => b.center[2] - b.half[2]));
    if (low < -GROUND_TOL) {
      let fix: AuditFix | null = null;
      if (!nudged.has(c)) {
        nudged.add(c);
        fix = {
          summary: `Raise '${c}' ${mm(-low)}mm so it sits on grade`,
          before: `lowest point ${mm(-low)}mm below grade`,
          after: "lowest point at grade (z=0)",
          ops: [{ op: "nudge", key: c, delta: [0, 0, -low] }],
        };
      }
      findings.push(
        finding(
          `below_grade:${c}`, "warning", "below_grade",
          `'${c}' digs ${mm(-low)}mm below grade`,
          "Geometry below z=0 would be underground on site — it can't be " +
            "fabricated as shown.",
          c, null, fix,
        ),
      );
    }
  }

  // ---------------------------------------------------------- declarations
  const declaredComponents = new Set(spec.components ?? []);
  for (const rp of spec.primitives ?? []) {
    if (rp.component) declaredComponents.add(rp.component);
  }
  const sideBoxes = (ref: string): Aabb[] =>
    members
      .filter((p) => ref === p.component || ref === `${p.component}/${p.name}`)
      .map((p) => aabb(p));

  for (const d of spec.connections ?? []) {
    const { a, b } = d;
    if (typeof a !== "string" || typeof b !== "string") continue;
    if (a === "ground" || b === "ground") continue; // anchorage: no pair contact
    const boxesA = sideBoxes(a);
    const boxesB = sideBoxes(b);
    if (!boxesA.length || !boxesB.length) {
      const missing = !boxesA.length ? a : b;
      if (declaredComponents.has(missing.split("/")[0])) continue; // dormant toggle
      findings.push(
        finding(
          `dead_declaration:${a}~${b}`, "warning", "dead_declaration",
          `Declared joint ${a} ↔ ${b} references a missing part`,
          `'${missing}' doesn't exist in the geometry, so this declaration does nothing.`,
          null, null,
          {
            summary: "Remove the dead declaration",
            before: `connections: ${a} ↔ ${b} (${d.type})`,
            after: "declaration removed",
            ops: [{ op: "undeclare", a, b }],
          },
        ),
      );
      continue;
    }
    const g = componentGap(boxesA, boxesB);
    if (g > CONTACT_TOL) {
      let mover = a.split("/")[0];
      let targetBoxes = boxesB;
      // move the smaller side; hardware goes where they overlap
      const volA = boxesA.reduce((s, x) => s + volume(x), 0);
      const volB = boxesB.reduce((s, x) => s + volume(x), 0);
      if (volB < volA) {
        mover = b.split("/")[0];
        targetBoxes = boxesA;
      }
      const moverBoxes =
        memberBoxes.get(mover) ?? (mover === a.split("/")[0] ? boxesA : boxesB);
      let fix: AuditFix | null = null;
      if (!nudged.has(mover)) {
        nudged.add(mover);
        const delta = closingDelta(moverBoxes, targetBoxes);
        fix = {
          summary: `Move '${mover}' to close the ${mm(g)}mm gap`,
          before: `parts ${mm(g)}mm apart, no contact`,
          after: `parts overlap ${mm(EMBED_FIX)}mm (${fmtDelta(delta)})`,
          ops: [{ op: "nudge", key: mover, delta }],
        };
      }
      findings.push(
        finding(
          `gap_declaration:${a}~${b}`, "error", "gap_declaration",
          `Declared joint ${a} ↔ ${b} has an air gap`,
          `The parts are ${mm(g)}mm apart — a ${d.type ?? "joint"} can't ` +
            `fasten parts that don't touch.`,
          mover, null, fix,
        ),
      );
    }
  }

  // ---------------------------------------------------------- shade coverage
  const shadeComps = comps.filter((c) => matchesAny(c, SHADE_KEYWORDS));
  const seatComps = comps.filter((c) => matchesAny(c, SEATING_KEYWORDS));
  for (const shade of shadeComps) {
    const shadeRect = xyRect(memberBoxes.get(shade)!);
    const [shadeLo] = zRange(memberBoxes.get(shade)!);
    for (const seat of seatComps) {
      if (seat === shade) continue;
      const [, seatHi] = zRange(memberBoxes.get(seat)!);
      if (shadeLo < seatHi - SHADE_CLEARANCE_TOL) continue; // side panel, not a roof
      const seatRect = xyRect(memberBoxes.get(seat)!);
      const frac = rectOverlapFraction(shadeRect, seatRect);
      if (frac < MIN_SHADE_COVERAGE) {
        const pct = Math.round(frac * 100);
        const detail =
          frac > 0
            ? `Its footprint overlaps only ${pct}% of '${seat}' — it won't ` +
              `actually shade the area below.`
            : `Its footprint doesn't overlap '${seat}' at all — it won't ` +
              `shade anything below.`;
        findings.push(
          finding(
            `no_coverage:${shade}~${seat}`, "warning", "no_coverage",
            `'${shade}' does not cover '${seat}'`, detail,
            shade,
          ),
        );
      }
    }
  }

  // ---------------------------------------------------------- joint checks
  const sliverSeen = new Set<string>();
  for (const jid of [...jointPrims.keys()].sort((x, y) => x - y)) {
    const rec = jointRecords.get(jid);
    const primsJ = jointPrims.get(jid)!;
    const a = rec?.a ?? null;
    const b = rec?.b ?? null;

    // -- detached hardware: every prim must chain to a member
    const anchorBoxes: Aabb[] = [];
    for (const side of [a, b]) {
      if (side === "ground") anchorBoxes.push(GROUND_BOX);
      else if (side && memberBoxes.has(side)) anchorBoxes.push(...memberBoxes.get(side)!);
    }
    if (anchorBoxes.length) {
      const attached = primsJ.map(({ box }) =>
        anchorBoxes.some((mb) => intersects(box, mb, ATTACH_TOL)),
      );
      let changed = true;
      while (changed) {
        changed = false;
        for (let i = 0; i < primsJ.length; i++) {
          if (attached[i]) continue;
          if (primsJ.some((q, k) => attached[k] && intersects(primsJ[i].box, q.box, ATTACH_TOL))) {
            attached[i] = changed = true;
          }
        }
      }
      const loose = primsJ.filter((_, i) => !attached[i]).map(({ p }) => p.name);
      if (loose.length) {
        const pair = a && b ? `${a} ↔ ${b}` : `joint ${jid}`;
        const decl = a && b ? declaredPair(spec, a, b) : undefined;
        const removable = a && b && !(decl && decl.type !== "none");
        findings.push(
          finding(
            `detached:${jid}`, "error", "detached",
            `Joint ${jid} hardware hangs in mid-air`,
            `${loose.length} of its ${primsJ.length} parts (e.g. '${loose[0]}') ` +
              `don't touch either member at ${pair} — fasteners must bear on ` +
              `the parts they join.`,
            a, jid,
            removable
              ? {
                  summary: `Remove the unbuildable hardware at ${pair}`,
                  before: `${primsJ.length} hardware parts, ${loose.length} floating`,
                  after: `joint ${pair} declared 'none' — no hardware generated`,
                  ops: [{ op: "declare", a: a!, b: b!, type: "none" }],
                }
              : null,
          ),
        );
      }
    }

    // -- sliver joints: bolted members that merely graze
    if (rec && BOLTED_TYPES.has(rec.type) && a && b && b !== "ground") {
      const overlap = rec.overlap;
      const pairKey = [a, b].sort().join("~");
      if (overlap !== undefined && overlap < SLIVER && !sliverSeen.has(pairKey)) {
        sliverSeen.add(pairKey);
        const axis = rec.axis ?? 2;
        const boxesA = memberBoxes.get(a) ?? [];
        const boxesB = memberBoxes.get(b) ?? [];
        if (boxesA.length && boxesB.length) {
          const volA = boxesA.reduce((s, x) => s + volume(x), 0);
          const volB = boxesB.reduce((s, x) => s + volume(x), 0);
          const [mover, target] = volA <= volB ? [a, b] : [b, a];
          let fix: AuditFix | null = null;
          if (!nudged.has(mover)) {
            nudged.add(mover);
            const mvBoxes = memberBoxes.get(mover)!;
            const tgBoxes = memberBoxes.get(target)!;
            const cM = mvBoxes.reduce((s, x) => s + x.center[axis], 0) / mvBoxes.length;
            const cT = tgBoxes.reduce((s, x) => s + x.center[axis], 0) / tgBoxes.length;
            const sign = cT >= cM ? 1 : -1;
            const delta: Vec3 = [0, 0, 0];
            delta[axis] = sign * (EMBED_FIX - overlap);
            fix = {
              summary: `Seat '${mover}' ${mm(EMBED_FIX - overlap)}mm deeper into '${target}'`,
              before: `members overlap ${mm(overlap)}mm`,
              after: `members overlap ${mm(EMBED_FIX)}mm (${fmtDelta(delta)})`,
              ops: [{ op: "nudge", key: mover, delta }],
            };
          }
          findings.push(
            finding(
              `sliver:${pairKey.replace("~", "~")}`, "warning", "sliver",
              `${a} ↔ ${b} barely touch (${mm(overlap)}mm)`,
              `The ${rec.type.replace(/_/g, " ")}s at joint ${jid} clamp on a ` +
                `${mm(overlap)}mm sliver — thinner than a washer. Real joints ` +
                `overlap 10-20mm.`,
              mover, jid, fix,
            ),
          );
        }
      }
    }

    // -- collisions with unrelated components
    for (const other of comps) {
      if (other === a || other === b) continue;
      let hit: string | null = null;
      for (const { p, box } of primsJ) {
        for (const mb of memberBoxes.get(other)!) {
          if (volume(box) > 0 && overlapVolume(box, mb) > 0.3 * volume(box)) {
            hit = p.name;
            break;
          }
        }
        if (hit) break;
      }
      if (hit) {
        const decl = a && b ? declaredPair(spec, a, b) : undefined;
        const inferred = !decl && a && b;
        findings.push(
          finding(
            `collision:${jid}:${other}`, "warning", "collision",
            `Joint ${jid} hardware buried inside '${other}'`,
            `'${hit}' passes through '${other}', which isn't part of this ` +
              `joint — a wrench could never reach it.`,
            other, jid,
            inferred
              ? {
                  summary: `Remove the colliding hardware at ${a} ↔ ${b}`,
                  before: `hardware intersects '${other}'`,
                  after: `joint ${a} ↔ ${b} declared 'none' — no hardware generated`,
                  ops: [{ op: "declare", a: a!, b: b!, type: "none" }],
                }
              : null,
          ),
        );
      }
    }
  }

  // errors first, position-stable within severity
  const errors = findings.filter((f) => f.severity === "error");
  const warnings = findings.filter((f) => f.severity !== "error");
  return {
    findings: [...errors, ...warnings],
    joints: jointPrims.size,
    components: comps.length,
  };
}

/** Pure: return a new spec with the given findings' fix ops applied.
 * Findings without a fix are ignored. Called only after user confirmation. */
export function applyAuditFixes(spec: AssetSpec, findings: AuditFinding[]): AssetSpec {
  const out = structuredClone(spec);
  const samePair = (c: { a?: string; b?: string }, a: string, b: string) =>
    (c.a === a && c.b === b) || (c.a === b && c.b === a);
  for (const f of findings) {
    if (!f.fix) continue;
    for (const op of f.fix.ops) {
      if (op.op === "nudge") {
        const offsets = out.offsets ?? (out.offsets = {});
        const cur = offsets[op.key] ?? [0, 0, 0];
        offsets[op.key] = [
          cur[0] + op.delta[0],
          cur[1] + op.delta[1],
          cur[2] + op.delta[2],
        ];
      } else if (op.op === "declare") {
        const conns = (out.connections ?? []).filter((c) => !samePair(c, op.a, op.b));
        conns.push({ a: op.a, b: op.b, type: op.type });
        out.connections = conns;
      } else {
        out.connections = (out.connections ?? []).filter((c) => !samePair(c, op.a, op.b));
      }
    }
  }
  return out;
}
