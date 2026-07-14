/** SketchUp-style direct-manipulation overlay, baked into the primitive list
 * so bounds, connection hardware, and the Blender export all agree with what
 * the viewport shows. Mirror of blender/builders/edits.py — keep in lockstep.
 *
 * Two passes:
 *   applyStructure — duplicate component groups, then drop deleted keys.
 *   applyTransforms — rotate/scale about the target's center, then position
 *                     offsets. Every overlay is keyed by a whole component
 *                     ('pole') or a single part ('pole/shaft'); a part edit
 *                     turns/stretches that part about its OWN center and
 *                     composes with any group edit (part first, then group,
 *                     then offsets).
 *
 * Rotation uses the Blender Euler-XYZ convention (R = Rz·Ry·Rx, X applied
 * first about fixed axes) — Three's Euler order 'ZYX', the same one the
 * meshes render with; the Python mirror replicates that exact matrix so
 * exports match. */
import * as THREE from "three";
import type { AssetSpec, Primitive, SpecEdits, Vec3 } from "../types";
import { aabb, eulerXyzMatrix } from "./hardware";

const ZERO: Vec3 = [0, 0, 0];
const ONE: Vec3 = [1, 1, 1];

function isMoved(v: Vec3): boolean {
  return v[0] !== 0 || v[1] !== 0 || v[2] !== 0;
}

/** Center of the combined AABB of a component's non-cut parts — the pivot the
 * rotate/scale gizmo turns around. */
export function componentPivot(prims: Primitive[]): Vec3 {
  const lo: Vec3 = [Infinity, Infinity, Infinity];
  const hi: Vec3 = [-Infinity, -Infinity, -Infinity];
  let n = 0;
  for (const p of prims) {
    if (p.cut) continue;
    const b = aabb(p);
    for (let k = 0; k < 3; k++) {
      lo[k] = Math.min(lo[k], b.center[k] - b.half[k]);
      hi[k] = Math.max(hi[k], b.center[k] + b.half[k]);
    }
    n += 1;
  }
  if (!n) return [0, 0, 0];
  return [(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2];
}

function cloneParams(params: Primitive["params"]): Primitive["params"] {
  return JSON.parse(JSON.stringify(params));
}

function cloneComponent(prims: Primitive[], source: string, name: string): Primitive[] {
  return prims
    .filter((p) => p.component === source)
    .map((p) => ({
      ...p,
      component: name,
      location: [...p.location] as Vec3,
      rotation: [...p.rotation] as Vec3,
      params: cloneParams(p.params),
    }));
}

/** Structural pass: duplicate component groups, then remove deleted keys.
 * Runs before the move/rotate/scale transform (which the gizmo previews). */
export function applyStructure(prims: Primitive[], spec: AssetSpec): Primitive[] {
  const edits: SpecEdits = spec.edits ?? {};
  let out = prims;

  if (edits.duplicates?.length) {
    const extra: Primitive[] = [];
    for (const d of edits.duplicates) extra.push(...cloneComponent(out, d.source, d.name));
    out = out.concat(extra);
  }

  const hidden = new Set(edits.hidden ?? []);
  if (hidden.size) {
    out = out.filter(
      (p) => !hidden.has(p.component) && !hidden.has(`${p.component}/${p.name}`),
    );
  }
  return out;
}

/** Per-LOCAL-axis stretch equivalent to a world-axis scale `s` on a
 * primitive currently rotated by `rotation` (Blender XYZ Euler, entering the
 * stage before the stage's own rotation is composed). For rotation matrix R
 * (eulerXyzMatrix) and S = diag(s), the local basis vector e_i lands at
 * S·R·e_i in world space, so its length is the equivalent local stretch:
 * f_i = ||S·R·e_i|| = sqrt(sum_j (s_j * R[j][i])^2). Exact (f === s) for
 * identity rotation. For rotations that aren't axis-aligned this is the
 * best diagonal approximation — a true non-uniform scale of a rotated solid
 * is a shear, which primitives (box/cylinder/etc, defined by
 * size/radius/depth) can't represent. Mirror of edits.py _scale_factors. */
function scaleFactors(s: Vec3, rotation: Vec3): Vec3 {
  const m = eulerXyzMatrix(rotation);
  const f: Vec3 = [0, 0, 0];
  for (let i = 0; i < 3; i++) {
    let sum = 0;
    for (let j = 0; j < 3; j++) sum += (s[j] * m[j][i]) ** 2;
    f[i] = Math.sqrt(sum);
  }
  return f;
}

/** Scale a primitive's LOCAL dimensions by per-local-axis factors `f` (see
 * scaleFactors — for identity rotation `f` is exactly the caller's
 * world-axis scale `s`, so unrotated behavior is unchanged). Exact for
 * axis-aligned boxes; round parts scale radius by the mean of the two
 * in-plane axes and length by the third (an ellipse can't be represented, so
 * this is a faithful-enough preview/export). */
function scaleParams(source: Primitive["params"], f: Vec3): Primitive["params"] {
  const params = { ...source };
  const rxy = (f[0] + f[1]) / 2;
  if (params.size) {
    params.size = [params.size[0] * f[0], params.size[1] * f[1], params.size[2] * f[2]];
  }
  if (typeof params.radius === "number") params.radius *= rxy;
  if (typeof params.radius_bottom === "number") params.radius_bottom *= rxy;
  if (typeof params.radius_top === "number") params.radius_top *= rxy;
  if (typeof params.radius_end === "number") params.radius_end *= rxy;
  if (typeof params.wall === "number") params.wall *= rxy;
  if (typeof params.depth === "number") params.depth *= f[2];
  if (params.path) {
    params.path = params.path.map(([x, y, z]) => [x * f[0], y * f[1], z * f[2]] as Vec3);
  }
  if (params.profile_start) {
    params.profile_start = {
      ...params.profile_start,
      w: params.profile_start.w * f[0],
      h: params.profile_start.h * f[1],
    };
  }
  if (params.profile_end) {
    params.profile_end = {
      ...params.profile_end,
      w: params.profile_end.w * f[0],
      h: params.profile_end.h * f[1],
    };
  }
  if (Array.isArray(params.profile)) {
    params.profile = params.profile.map(([r, z]) => [r * rxy, z * f[2]] as [number, number]);
  }
  return params;
}

interface Staged {
  location: Vec3;
  rotation: Vec3;
  params: Primitive["params"];
}

/** One rotate/scale stage about a pivot (used for the part-level edit, then
 * again for the component-level edit). Mirror of edits.py _apply_stage. */
function applyStage(
  st: Staged,
  pivot: Vec3,
  rot: Vec3 | undefined,
  scl: Vec3 | undefined,
): Staged {
  const s = scl ?? ONE;
  let rel: Vec3 = [
    (st.location[0] - pivot[0]) * s[0],
    (st.location[1] - pivot[1]) * s[1],
    (st.location[2] - pivot[2]) * s[2],
  ];
  const rotationIn = st.rotation;
  let rotation = st.rotation;
  if (rot) {
    const rMat = new THREE.Matrix4().makeRotationFromEuler(
      new THREE.Euler(rot[0], rot[1], rot[2], "ZYX"),
    );
    const v = new THREE.Vector3(rel[0], rel[1], rel[2]).applyMatrix4(rMat);
    rel = [v.x, v.y, v.z];
    const rp = new THREE.Matrix4().makeRotationFromEuler(
      new THREE.Euler(st.rotation[0], st.rotation[1], st.rotation[2], "ZYX"),
    );
    const composed = new THREE.Euler().setFromRotationMatrix(rMat.multiply(rp), "ZYX");
    rotation = [composed.x, composed.y, composed.z];
  }
  return {
    location: [pivot[0] + rel[0], pivot[1] + rel[1], pivot[2] + rel[2]],
    rotation,
    params: scl ? scaleParams(st.params, scaleFactors(s, rotationIn)) : st.params,
  };
}

/** Move/rotate/scale pass. Rotate/scale turn about the target's center —
 * the whole group for a 'component' key, the single part for a
 * 'component/part' key (part stage first, then group stage) — and offsets
 * (component + part) translate afterward. */
export function applyTransforms(prims: Primitive[], spec: AssetSpec): Primitive[] {
  const edits: SpecEdits = spec.edits ?? {};
  const rotations = (edits.rotations ?? {}) as Record<string, Vec3>;
  const scales = (edits.scales ?? {}) as Record<string, Vec3>;
  const offsets = (spec.offsets ?? {}) as Record<string, Vec3>;

  const hasGroupEdits = Object.keys(rotations).length || Object.keys(scales).length;
  const hasOffsets = Object.keys(offsets).length;
  if (!hasGroupEdits && !hasOffsets) return prims;

  // pivots per edit key, from the pre-transform prims: a component key
  // turns about the group's center, a 'component/part' key about that
  // part's own center
  const pivots = new Map<string, Vec3>();
  for (const key of new Set([...Object.keys(rotations), ...Object.keys(scales)])) {
    const match = key.includes("/")
      ? prims.filter((p) => `${p.component}/${p.name}` === key)
      : prims.filter((p) => p.component === key);
    if (match.length) pivots.set(key, componentPivot(match));
  }

  return prims.map((p) => {
    const partKey = `${p.component}/${p.name}`;
    const rotP = rotations[partKey];
    const sclP = scales[partKey];
    const rotC = rotations[p.component];
    const sclC = scales[p.component];
    const oc = offsets[p.component] ?? ZERO;
    const op = offsets[partKey] ?? ZERO;
    if (!rotP && !sclP && !rotC && !sclC && !isMoved(oc) && !isMoved(op)) return p;

    let st: Staged = { location: p.location, rotation: p.rotation, params: p.params };
    if (rotP || sclP) st = applyStage(st, pivots.get(partKey) ?? ZERO, rotP, sclP);
    if (rotC || sclC) st = applyStage(st, pivots.get(p.component) ?? ZERO, rotC, sclC);
    return {
      ...p,
      location: [
        st.location[0] + oc[0] + op[0],
        st.location[1] + oc[1] + op[1],
        st.location[2] + oc[2] + op[2],
      ] as Vec3,
      rotation: st.rotation,
      params: st.params,
    };
  });
}
