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

/** All current violations keyed by parameter id. */
export function checkSpec(spec: AssetSpec): Record<string, CodeViolation> {
  const out: Record<string, CodeViolation> = {};
  for (const p of spec.parameters) {
    const v = checkParam(spec, p);
    if (v) out[p.id] = v;
  }
  return out;
}
