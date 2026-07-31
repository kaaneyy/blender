import { filletPath } from "../shapes";
/** Mirror of blender/builders/generic.py — realizes the spec's own
 * `primitives` array (the LLM "generate anything" path). */
import type { AssetSpec, LoftProfile, Primitive, Vec3 } from "../types";
import { evalExpr } from "../expr";
import { specParams, specToggles } from "./base";

type RawValue = number | string;

export function expressionEnv(spec: AssetSpec): Record<string, number> {
  const env: Record<string, number> = {};
  const toggles = specToggles(spec);
  for (const id of Object.keys(toggles)) env[id] = toggles[id] ? 1 : 0;
  Object.assign(env, specParams(spec));
  return env;
}

function vec3(raw: RawValue[] | undefined, env: Record<string, number>, fallback: Vec3): Vec3 {
  if (!raw) return fallback;
  return [evalExpr(raw[0], env), evalExpr(raw[1], env), evalExpr(raw[2], env)];
}

function evalParams(rp: NonNullable<AssetSpec["primitives"]>[number]["params"], env: Record<string, number>): Primitive["params"] {
  const params: Primitive["params"] = {};
  if (rp.radius !== undefined) params.radius = evalExpr(rp.radius, env);
  if (rp.depth !== undefined) params.depth = evalExpr(rp.depth, env);
  if (rp.radius_bottom !== undefined) params.radius_bottom = evalExpr(rp.radius_bottom, env);
  if (rp.radius_top !== undefined) params.radius_top = evalExpr(rp.radius_top, env);
  if (rp.size !== undefined) params.size = vec3(rp.size, env, [1, 1, 1]);
  if (rp.segments !== undefined) params.segments = evalExpr(rp.segments, env);
  if (rp.shell !== undefined) params.shell = evalExpr(rp.shell, env);
  if (rp.radius_end !== undefined) params.radius_end = evalExpr(rp.radius_end, env);
  if (rp.wall !== undefined) params.wall = evalExpr(rp.wall, env);
  // a named choice, never arithmetic — passed through untouched so the
  // expression evaluator never tries to resolve e.g. "square" as a variable
  if (rp.section !== undefined) params.section = rp.section;
  if (rp.profile !== undefined) {
    params.profile =
      typeof rp.profile === "string"
        ? rp.profile
        : rp.profile.map(([r, z]) => [evalExpr(r, env), evalExpr(z, env)] as [number, number]);
  }
  if (rp.path !== undefined) {
    params.path = rp.path.map((pt) => vec3(pt, env, [0, 0, 0]));
  }
  if (rp.bend_radius !== undefined) params.bend_radius = evalExpr(rp.bend_radius, env);
  for (const key of ["profile_start", "profile_end"] as const) {
    const v = rp[key];
    if (v !== undefined) {
      params[key] = {
        shape: v.shape as LoftProfile["shape"],
        w: evalExpr(v.w, env),
        h: evalExpr(v.h, env),
      };
    }
  }
  // A called-out bend is baked into the path HERE, once, so everything
  // downstream — the AABB, the preview, the Blender curve, the takeoff —
  // reads the same filleted polyline and can never disagree about it.
  if (params.path && typeof params.bend_radius === "number" && params.bend_radius > 0) {
    params.path = filletPath(params.path, params.bend_radius);
  }
  return params;
}

export function buildCustom(spec: AssetSpec): Primitive[] {
  const env = expressionEnv(spec);
  const prims: Primitive[] = [];
  const used = new Set<string>();

  (spec.primitives ?? []).forEach((raw, i) => {
    if (raw.visible_if !== undefined && evalExpr(raw.visible_if, env) === 0) return;

    const params = evalParams(raw.params ?? {}, env);
    const baseName = raw.name || `part_${i + 1}`;
    const location = vec3(raw.location, env, [0, 0, 0]);
    const rotation = vec3(raw.rotation, env, [0, 0, 0]);

    // B7: linear array — expand into evenly stepped copies
    let placements: Array<[string, Vec3]>;
    if (raw.array) {
      // half-up: floor(x+0.5), keep identical to the mirror. Math.round IS
      // floor(x+0.5) for all reals — this is the shared convention.
      const count = Math.max(1, Math.round(evalExpr(raw.array.count, env)));
      const step = vec3(raw.array.step, env, [0, 0, 0]);
      placements = Array.from({ length: count }, (_, n) => [
        count > 1 ? `${baseName}_${n + 1}` : baseName,
        [location[0] + step[0] * n, location[1] + step[1] * n, location[2] + step[2] * n] as Vec3,
      ]);
    } else {
      placements = [[baseName, location]];
    }

    for (const [rawName, loc] of placements) {
      let name = rawName;
      while (used.has(name)) name += "_";
      used.add(name);
      prims.push({
        kind: raw.kind,
        name,
        component: raw.component ?? "body",
        location: loc,
        rotation,
        materialSlot: raw.material_slot ?? "default",
        cut: Boolean(raw.cut),
        params: { ...params },
      });
    }
  });

  if (!prims.some((p) => !p.cut)) {
    throw new Error("Custom spec produced no visible primitives");
  }
  return prims;
}
