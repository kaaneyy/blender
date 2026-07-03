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

    return ValidationResult(spec=out, violations=violations, checked=True)
