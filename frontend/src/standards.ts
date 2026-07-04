/** Client-side mirror of standards/validator.py, reading the SAME
 * standards/us_codes.json (single source of truth — the file is imported
 * across the package boundary at build time, never duplicated).
 * Used for the live code-violation UI (T4.5); the server/CLI re-validate
 * authoritatively before any export. */
import usCodes from "../../standards/us_codes.json";
import type { AssetSpec, SpecParameter, Unit } from "./types";
import { convert } from "./units";

interface Rule {
  min: number | null;
  max: number | null;
  default?: number;
  unit: Unit;
  code_ref?: string;
  note?: string;
}

export interface CodeViolation {
  parameterId: string;
  limitType: "min" | "max";
  limitValue: number;
  limitUnit: Unit;
  /** Nearest legal value, in the parameter's own unit. */
  correctedValue: number;
  codeRef: string;
  message: string;
}

const DB = usCodes as unknown as Record<
  string,
  { source?: string; parameters?: Record<string, Rule> }
>;

export function checkParam(
  spec: AssetSpec,
  param: SpecParameter,
): CodeViolation | null {
  if (typeof param.value !== "number") return null;
  const rule = DB[spec.asset_type]?.parameters?.[param.id];
  if (!rule) return null;

  const unit: Unit = param.unit ?? (spec.units === "imperial" ? "ft" : "m");
  const v = convert(param.value, unit, rule.unit);

  for (const limitType of ["min", "max"] as const) {
    const limit = rule[limitType];
    if (limit == null) continue;
    const broken = limitType === "min" ? v < limit : v > limit;
    if (!broken) continue;
    return {
      parameterId: param.id,
      limitType,
      limitValue: limit,
      limitUnit: rule.unit,
      correctedValue: Number(convert(limit, rule.unit, unit).toFixed(6)),
      codeRef: rule.code_ref ?? "",
      message:
        `${param.label} must be ${limitType === "min" ? "≥" : "≤"} ` +
        `${limit} ${rule.unit} — ${rule.code_ref ?? DB[spec.asset_type]?.source ?? "US code"}` +
        (rule.note ? `. ${rule.note}` : ""),
    };
  }
  return null;
}

interface RatioRule {
  of: string;
  to: string;
  min?: number | null;
  max?: number | null;
  code_ref?: string;
  note?: string;
}

/** C5 mirror: member-sizing ratio rules (e.g. pole base:top taper). */
function checkRatios(spec: AssetSpec): Record<string, CodeViolation> {
  const entry = DB[spec.asset_type] as
    | { sizing?: { ratios?: RatioRule[] }; source?: string }
    | undefined;
  const out: Record<string, CodeViolation> = {};
  const byId = new Map(spec.parameters.map((p) => [p.id, p]));
  for (const rr of entry?.sizing?.ratios ?? []) {
    const pOf = byId.get(rr.of);
    const pTo = byId.get(rr.to);
    if (!pOf || !pTo || typeof pOf.value !== "number" || typeof pTo.value !== "number") {
      continue;
    }
    const unitOf: Unit = pOf.unit ?? (spec.units === "imperial" ? "ft" : "m");
    const unitTo: Unit = pTo.unit ?? (spec.units === "imperial" ? "ft" : "m");
    const vOf = convert(pOf.value, unitOf, "m");
    const vTo = convert(pTo.value, unitTo, "m");
    if (vTo <= 0) continue;
    const ratio = vOf / vTo;
    for (const limitType of ["min", "max"] as const) {
      const limit = rr[limitType];
      if (limit == null) continue;
      const broken = limitType === "min" ? ratio < limit : ratio > limit;
      if (!broken) continue;
      out[rr.of] = {
        parameterId: rr.of,
        limitType,
        limitValue: limit,
        limitUnit: unitOf,
        correctedValue: Number(convert(vTo * limit, "m", unitOf).toFixed(6)),
        codeRef: rr.code_ref ?? "",
        message:
          `${rr.of} : ${rr.to} ratio ${ratio.toFixed(2)} is ` +
          `${limitType === "min" ? "below" : "above"} the fabrication range ` +
          `${limitType} ${limit} — ${rr.code_ref ?? entry?.source ?? "sizing rule"}` +
          (rr.note ? `. ${rr.note}` : ""),
      };
      break;
    }
  }
  return out;
}

/** All current violations keyed by parameter id. */
export function checkSpec(spec: AssetSpec): Record<string, CodeViolation> {
  const out: Record<string, CodeViolation> = {};
  for (const p of spec.parameters) {
    const v = checkParam(spec, p);
    if (v) out[p.id] = v;
  }
  // sizing ratios only fill slots without a direct min/max violation
  const ratios = checkRatios(spec);
  for (const [id, v] of Object.entries(ratios)) {
    if (!out[id]) out[id] = v;
  }
  return out;
}
