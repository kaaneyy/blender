/** Pure derivation of which feature toggle "owns" a parameter, for the
 * Options panel (components/ControlsPanel.tsx). Display-only: nothing here
 * changes what geometry gets built — it only decides where a control
 * renders and whether it's dimmed. There is no Python mirror for this file
 * because no geometry constant/formula/ordering is involved; the token-scan
 * rules below port (verbatim, for TS) the read-only analysis in
 * blender/builders/connectivity.py's `_referenced_ids` / `_expr_tokens` /
 * `check_dead_controls`, which already proved this exact definition of
 * "referenced" and "gated" against the generic builder's own expression
 * sites.
 *
 * Ownership rule: a parameter P is owned by toggle T when EVERY primitive
 * that references P (as an expression-site identifier token — see
 * builders/generic.ts's evalParams/buildCustom for the exact sites scanned)
 * is itself gated by T (T's id appears as a token in that primitive's
 * `visible_if`), and at least one primitive references P. If the
 * referencing primitives don't share a single common gating toggle, P has
 * no owner and stays a core "Parameters" control — conservative by
 * construction. A spec with no `primitives` (curated builders like
 * street_light) yields no ownership at all, so those specs render exactly
 * like today's flat list. */
import type { AssetSpec, SpecPrimitive } from "./types";

/** Call names in the expression grammar (expr.ts's FUNCS) — identifier-
 * shaped tokens that are never parameter/toggle ids. Mirrors
 * connectivity.py::_EXPR_FUNCTION_NAMES. */
const EXPR_FUNCTION_NAMES = new Set(["min", "max", "abs"]);
const IDENT_RE = /[A-Za-z_][A-Za-z0-9_]*/g;

/** The one toggle every build consumes directly (base.ts/base.py), never
 * eligible to "own" a parameter. Mirrors connectivity.py::_UNIVERSAL_TOGGLES. */
const UNIVERSAL_TOGGLE = "connection_hardware";

export interface OptionDeps {
  /** paramId -> the single toggle id that exclusively owns it. A param with
   * no entry here is "core" — always relevant regardless of any toggle. */
  paramOwner: Record<string, string>;
  /** toggleId -> distinct count of primitives gated by that toggle's
   * visible_if. No key for a toggle that gates nothing (or is unknown). */
  gatedCount: Record<string, number>;
}

function exprTokens(value: unknown): Set<string> {
  const out = new Set<string>();
  if (typeof value !== "string") return out;
  for (const m of value.matchAll(IDENT_RE)) {
    if (!EXPR_FUNCTION_NAMES.has(m[0])) out.add(m[0]);
  }
  return out;
}

function unionInto(target: Set<string>, src: Set<string>): void {
  for (const t of src) target.add(t);
}

function intersect(a: Set<string>, b: Set<string>): Set<string> {
  const out = new Set<string>();
  for (const t of a) if (b.has(t)) out.add(t);
  return out;
}

/** Every parameter/toggle id token referenced by ONE primitive's expression
 * sites — mirrors generic.ts's evalParams/buildCustom dispatch exactly:
 * visible_if, location, rotation, array.count/array.step, and every params
 * entry (scalars, size/vec triples, the lathe profile's raw point list — a
 * named profile string is not an expression, the sweep path's point list,
 * and profile_start/profile_end's w/h). A malformed/partial primitive
 * contributes nothing rather than throwing. */
function referencedIdsForPrimitive(raw: unknown): Set<string> {
  const referenced = new Set<string>();
  if (!raw || typeof raw !== "object") return referenced;
  const prim = raw as SpecPrimitive;

  unionInto(referenced, exprTokens(prim.visible_if));
  for (const v of prim.location ?? []) unionInto(referenced, exprTokens(v));
  for (const v of prim.rotation ?? []) unionInto(referenced, exprTokens(v));

  const array = prim.array;
  if (array && typeof array === "object") {
    unionInto(referenced, exprTokens(array.count));
    for (const v of array.step ?? []) unionInto(referenced, exprTokens(v));
  }

  const params = prim.params;
  if (params && typeof params === "object") {
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined) continue;
      if (key === "profile") {
        if (typeof value === "string") continue; // named profile (e.g. "dome") — not an expression
        for (const pair of value as unknown as Array<unknown>) {
          if (Array.isArray(pair)) for (const v of pair) unionInto(referenced, exprTokens(v));
        }
      } else if (key === "path") {
        for (const point of value as unknown as Array<unknown>) {
          if (Array.isArray(point)) for (const v of point) unionInto(referenced, exprTokens(v));
        }
      } else if (key === "profile_start" || key === "profile_end") {
        if (value && typeof value === "object") {
          const v = value as { w?: unknown; h?: unknown };
          unionInto(referenced, exprTokens(v.w));
          unionInto(referenced, exprTokens(v.h));
        }
      } else if (Array.isArray(value)) {
        for (const v of value) unionInto(referenced, exprTokens(v));
      } else {
        unionInto(referenced, exprTokens(value));
      }
    }
  }
  return referenced;
}

/** Toggle ids (excluding the universal connection_hardware) whose id token
 * appears in this primitive's own visible_if — i.e. the toggles that gate
 * it. */
function gatingToggles(raw: unknown, toggleIds: string[]): Set<string> {
  const prim = raw as SpecPrimitive | undefined | null;
  const tokens = exprTokens(prim?.visible_if);
  const out = new Set<string>();
  for (const id of toggleIds) if (tokens.has(id)) out.add(id);
  return out;
}

export function computeOptionDeps(spec: AssetSpec | null | undefined): OptionDeps {
  const paramOwner: Record<string, string> = {};
  const gatedCount: Record<string, number> = {};
  const primitives = spec?.primitives;
  if (!Array.isArray(primitives) || primitives.length === 0) {
    // Curated builders (street_light, ...) declare no `primitives` — no
    // ownership is derivable, so every param stays a flat "core" control.
    return { paramOwner, gatedCount };
  }

  const toggleIds = (spec?.toggles ?? [])
    .map((t) => t?.id)
    .filter((id): id is string => typeof id === "string" && id !== UNIVERSAL_TOGGLE);

  const gatingByPrimitive = primitives.map((raw) => gatingToggles(raw, toggleIds));

  for (const tid of toggleIds) {
    let count = 0;
    for (const gating of gatingByPrimitive) if (gating.has(tid)) count += 1;
    if (count > 0) gatedCount[tid] = count;
  }

  const paramIds = (spec?.parameters ?? [])
    .map((p) => p?.id)
    .filter((id): id is string => typeof id === "string");

  for (const pid of paramIds) {
    let intersection: Set<string> | null = null;
    let referencedByAny = false;
    for (let i = 0; i < primitives.length; i++) {
      const refs = referencedIdsForPrimitive(primitives[i]);
      if (!refs.has(pid)) continue;
      referencedByAny = true;
      intersection =
        intersection === null ? new Set(gatingByPrimitive[i]) : intersect(intersection, gatingByPrimitive[i]);
      if (intersection.size === 0) break; // no possible single common owner left
    }
    if (referencedByAny && intersection && intersection.size === 1) {
      const [only] = intersection;
      paramOwner[pid] = only;
    }
  }

  return { paramOwner, gatedCount };
}
