/** Mirror of standards/validator.py unit handling. */
import type { LengthUnit, Unit, UnitSystem } from "./types";

export const UNIT_TO_METERS: Record<LengthUnit, number> = {
  m: 1,
  cm: 0.01,
  mm: 0.001,
  ft: 0.3048,
  in: 0.0254,
};

/** True for the length units that convert to meters (ft/in/m/cm/mm); false
 * for the dimensionless display units (deg/W/x) and undefined. */
export function isLengthUnit(unit: Unit | undefined): unit is LengthUnit {
  return unit !== undefined && unit in UNIT_TO_METERS;
}

/** Display symbol for a unit: the pretty glyph for dimensionless units, the
 * unit itself for lengths. */
export function unitSymbol(unit: Unit | undefined): string {
  if (unit === "deg") return "°";
  if (unit === "x") return "×";
  return unit ?? "";
}

export function convert(value: number, from: LengthUnit, to: LengthUnit): number {
  if (from === to) return value;
  return (value * UNIT_TO_METERS[from]) / UNIT_TO_METERS[to];
}

const trim = (v: number) =>
  Number(v.toFixed(2)).toLocaleString("en-US", { maximumFractionDigits: 2 });

/** Format a length (given in meters) for display in either unit system. */
export function formatLength(meters: number, system: UnitSystem): string {
  return system === "imperial"
    ? `${trim(convert(meters, "m", "ft"))} ft`
    : `${trim(meters)} m`;
}

/** The "other system" equivalent shown next to a raw length value (only
 * length units have a counterpart; dimensionless units don't). */
export function counterpart(value: number, unit: LengthUnit): string {
  const imperial = unit === "ft" || unit === "in";
  return imperial
    ? `${trim(convert(value, unit, "m"))} m`
    : `${trim(convert(value, unit, "ft"))} ft`;
}
