/** Mirror of blender/builders/generic.py — realizes the spec's own
 * `primitives` array (the LLM "generate anything" path). */
import type { AssetSpec, Primitive, Vec3 } from "../types";
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

export function buildCustom(spec: AssetSpec): Primitive[] {
  const env = expressionEnv(spec);
  const prims: Primitive[] = [];
  const used = new Set<string>();

  (spec.primitives ?? []).forEach((raw, i) => {
    if (raw.visible_if !== undefined && evalExpr(raw.visible_if, env) === 0) return;

    const params: Primitive["params"] = {};
    const rp = raw.params ?? {};
    if (rp.radius !== undefined) params.radius = evalExpr(rp.radius, env);
    if (rp.depth !== undefined) params.depth = evalExpr(rp.depth, env);
    if (rp.radius_bottom !== undefined) params.radius_bottom = evalExpr(rp.radius_bottom, env);
    if (rp.radius_top !== undefined) params.radius_top = evalExpr(rp.radius_top, env);
    if (rp.size !== undefined) params.size = vec3(rp.size, env, [1, 1, 1]);
    if (rp.segments !== undefined) params.segments = evalExpr(rp.segments, env);

    let name = raw.name || `part_${i + 1}`;
    while (used.has(name)) name += "_";
    used.add(name);

    prims.push({
      kind: raw.kind,
      name,
      component: raw.component ?? "body",
      location: vec3(raw.location, env, [0, 0, 0]),
      rotation: vec3(raw.rotation, env, [0, 0, 0]),
      materialSlot: raw.material_slot ?? "default",
      params,
    });
  });

  if (!prims.length) throw new Error("Custom spec produced no visible primitives");
  return prims;
}
