"""Code-compliance validator for AssetSpec documents (T1.2, T1.3).

Pure functions only: no I/O besides the optional standards-file load, no
globals mutated, so both the FastAPI backend and the Blender worker can call
`validate_spec` and get identical results.

Behavior by ``code_mode`` (spec field, T1.3):
  * ``strict``   - out-of-range values are clamped in the returned spec and
                   reported as violations with ``corrected_value`` set.
  * ``advisory`` - violations are reported but nothing is changed
                   (movie/animation props that don't need code compliance).
"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

STANDARDS_PATH = Path(__file__).resolve().parent / "us_codes.json"

#: Length-unit conversion table (everything goes through meters).
UNIT_TO_METERS = {
    "m": 1.0,
    "cm": 0.01,
    "mm": 0.001,
    "ft": 0.3048,
    "in": 0.0254,
}


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """Convert a length between any two supported units."""
    if from_unit == to_unit:
        return float(value)
    try:
        return float(value) * UNIT_TO_METERS[from_unit] / UNIT_TO_METERS[to_unit]
    except KeyError as exc:
        raise ValueError(f"Unsupported unit: {exc.args[0]!r}") from exc


def load_standards(path: Optional[Path] = None) -> dict:
    """Load the US-code standards DB (keys starting with '_' are metadata)."""
    return json.loads(Path(path or STANDARDS_PATH).read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Violation:
    parameter_id: str
    value: float
    unit: str
    limit_type: str  # "min" | "max"
    limit_value: float
    limit_unit: str
    corrected_value: Optional[float]  # None in advisory mode
    code_ref: str
    source: str
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ValidationResult:
    spec: dict
    violations: list = field(default_factory=list)
    #: False when the asset_type has no entry in the standards DB, meaning
    #: nothing could be checked (distinct from "checked and clean").
    checked: bool = True

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict:
        return {
            "spec": self.spec,
            "violations": [v.to_dict() for v in self.violations],
            "checked": self.checked,
            "ok": self.ok,
        }


def validate_standards_db(db: dict) -> list:
    """Structural check for a (possibly AI-proposed) standards DB. Returns a
    list of human-readable problems; empty list means structurally sound."""
    errors = []
    if not isinstance(db, dict) or not any(k for k in db if not k.startswith("_")):
        return ["Standards DB must be an object with at least one asset type"]
    for asset_type, entry in db.items():
        if asset_type.startswith("_"):
            continue
        where = f"{asset_type}"
        if not isinstance(entry, dict) or not isinstance(entry.get("parameters"), dict) \
                or not entry["parameters"]:
            errors.append(f"{where}: needs a non-empty 'parameters' object")
            continue
        if not entry.get("source"):
            errors.append(f"{where}: missing 'source' citation")
        for pid, rule in entry["parameters"].items():
            w = f"{where}.{pid}"
            if not isinstance(rule, dict):
                errors.append(f"{w}: rule must be an object")
                continue
            if rule.get("unit") not in UNIT_TO_METERS:
                errors.append(f"{w}: bad unit {rule.get('unit')!r}")
            if not isinstance(rule.get("min"), (int, float)):
                errors.append(f"{w}: 'min' must be a number")
            maximum = rule.get("max")
            if maximum is not None:
                if not isinstance(maximum, (int, float)):
                    errors.append(f"{w}: 'max' must be a number or null")
                elif isinstance(rule.get("min"), (int, float)) and maximum < rule["min"]:
                    errors.append(f"{w}: max {maximum} < min {rule['min']}")
            default = rule.get("default")
            if not isinstance(default, (int, float)):
                errors.append(f"{w}: 'default' must be a number")
            elif isinstance(rule.get("min"), (int, float)):
                if default < rule["min"] or (
                    isinstance(maximum, (int, float)) and default > maximum
                ):
                    errors.append(f"{w}: default {default} outside [min, max]")
            if not rule.get("code_ref"):
                errors.append(f"{w}: missing 'code_ref'")
    return errors


def _default_unit(spec: dict) -> str:
    return "ft" if spec.get("units", "imperial") == "imperial" else "m"


def validate_spec(spec: dict, standards: Optional[dict] = None) -> ValidationResult:
    """Check every spec parameter against the standards DB.

    Returns a :class:`ValidationResult` whose ``spec`` is a deep copy; the
    input spec is never mutated. In strict mode the copy has out-of-range
    values clamped to the nearest legal limit.
    """
    if standards is None:
        standards = load_standards()

    out = copy.deepcopy(spec)
    strict = out.get("code_mode", "strict") == "strict"
    rules = standards.get(out.get("asset_type", ""))
    if not isinstance(rules, dict):
        return ValidationResult(spec=out, checked=False)

    param_rules: dict = rules.get("parameters", {})
    source: str = rules.get("source", "")
    violations: list[Violation] = []

    for param in out.get("parameters", []):
        rule = param_rules.get(param.get("id", ""))
        value = param.get("value")
        if rule is None or not isinstance(value, (int, float)):
            continue  # no rule for this parameter, or non-numeric (select)
        if param.get("unit") and param["unit"] not in UNIT_TO_METERS:
            continue  # dimensionless param (deg/W/x): no dimensional code limits

        unit = param.get("unit") or _default_unit(out)
        rule_unit = rule.get("unit", unit)
        value_in_rule_unit = convert(value, unit, rule_unit)

        for limit_type in ("min", "max"):
            limit = rule.get(limit_type)
            if limit is None:
                continue
            too_low = limit_type == "min" and value_in_rule_unit < limit
            too_high = limit_type == "max" and value_in_rule_unit > limit
            if not (too_low or too_high):
                continue

            corrected = round(convert(limit, rule_unit, unit), 6)
            violations.append(
                Violation(
                    parameter_id=param["id"],
                    value=value,
                    unit=unit,
                    limit_type=limit_type,
                    limit_value=limit,
                    limit_unit=rule_unit,
                    corrected_value=corrected if strict else None,
                    code_ref=rule.get("code_ref", ""),
                    source=source,
                    message=(
                        f"{param['id']} = {value} {unit} is "
                        f"{'below minimum' if too_low else 'above maximum'} "
                        f"{limit} {rule_unit} ({rule.get('code_ref', source)})"
                    ),
                )
            )
            if strict:
                param["value"] = corrected

    # C5: member-sizing ratio rules (load-plausible proportions, not FEA) —
    # e.g. a tapered pole's base:top diameter ratio must stay in a real
    # fabrication range; the "of" parameter is corrected in strict mode.
    by_id = {p.get("id"): p for p in out.get("parameters", [])}
    for rr in rules.get("sizing", {}).get("ratios", []):
        p_of = by_id.get(rr.get("of"))
        p_to = by_id.get(rr.get("to"))
        if (
            p_of is None or p_to is None
            or not isinstance(p_of.get("value"), (int, float))
            or not isinstance(p_to.get("value"), (int, float))
        ):
            continue
        unit_of = p_of.get("unit") or _default_unit(out)
        unit_to = p_to.get("unit") or _default_unit(out)
        v_of = convert(p_of["value"], unit_of, "m")
        v_to = convert(p_to["value"], unit_to, "m")
        if v_to <= 0:
            continue
        ratio = v_of / v_to
        for limit_type in ("min", "max"):
            limit = rr.get(limit_type)
            if limit is None:
                continue
            too_low = limit_type == "min" and ratio < limit
            too_high = limit_type == "max" and ratio > limit
            if not (too_low or too_high):
                continue
            corrected = round(convert(v_to * limit, "m", unit_of), 6)
            violations.append(
                Violation(
                    parameter_id=rr["of"],
                    value=round(ratio, 4),
                    unit="ratio",
                    limit_type=limit_type,
                    limit_value=limit,
                    limit_unit=f"× {rr['to']}",
                    corrected_value=corrected if strict else None,
                    code_ref=rr.get("code_ref", ""),
                    source=source,
                    message=(
                        f"{rr['of']} : {rr['to']} ratio {ratio:.2f} is "
                        f"{'below' if too_low else 'above'} the fabrication range "
                        f"{limit_type} {limit} ({rr.get('code_ref', source)})"
                    ),
                )
            )
            if strict:
                p_of["value"] = corrected

    return ValidationResult(spec=out, violations=violations, checked=True)
