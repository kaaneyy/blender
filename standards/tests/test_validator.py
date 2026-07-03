"""Unit tests for the pure code-compliance validator (T1.2, part of T7.1)."""
import math

import pytest

from standards.validator import convert, load_standards, validate_spec


def make_spec(**overrides) -> dict:
    spec = {
        "asset_type": "street_light",
        "name": "TestLight",
        "units": "imperial",
        "code_mode": "strict",
        "parameters": [
            {"id": "pole_height", "label": "Pole Height", "type": "slider",
             "min": 20, "max": 40, "step": 0.5, "value": 30, "unit": "ft",
             "code_ref": "AASHTO-RDG-lighting"},
            {"id": "arm_length", "label": "Arm Length", "type": "slider",
             "min": 4, "max": 15, "step": 0.5, "value": 8, "unit": "ft",
             "code_ref": "AASHTO-RDG-lighting"},
        ],
        "toggles": [],
    }
    spec.update(overrides)
    return spec


def set_param(spec, param_id, value, unit=None):
    for p in spec["parameters"]:
        if p["id"] == param_id:
            p["value"] = value
            if unit:
                p["unit"] = unit
            return
    raise KeyError(param_id)


class TestConvert:
    def test_identity(self):
        assert convert(5, "ft", "ft") == 5.0

    def test_ft_to_m(self):
        assert convert(10, "ft", "m") == pytest.approx(3.048)

    def test_in_to_ft(self):
        assert convert(36, "in", "ft") == pytest.approx(3.0)

    def test_roundtrip(self):
        assert convert(convert(7.25, "ft", "mm"), "mm", "ft") == pytest.approx(7.25)

    def test_unknown_unit_raises(self):
        with pytest.raises(ValueError):
            convert(1, "furlong", "m")


class TestStrictClamping:
    def test_in_range_is_clean(self):
        result = validate_spec(make_spec())
        assert result.ok and result.checked
        assert result.spec["parameters"][0]["value"] == 30

    def test_below_min_is_clamped(self):
        spec = make_spec()
        set_param(spec, "pole_height", 12)
        result = validate_spec(spec)
        assert not result.ok
        v = result.violations[0]
        assert v.parameter_id == "pole_height"
        assert v.limit_type == "min"
        assert v.corrected_value == 20
        assert result.spec["parameters"][0]["value"] == 20
        # input spec must never be mutated
        assert spec["parameters"][0]["value"] == 12

    def test_above_max_is_clamped(self):
        spec = make_spec()
        set_param(spec, "arm_length", 25)
        result = validate_spec(spec)
        [v] = result.violations
        assert v.limit_type == "max"
        assert result.spec["parameters"][1]["value"] == 15

    def test_violation_carries_code_ref(self):
        spec = make_spec()
        set_param(spec, "pole_height", 5)
        [v] = validate_spec(spec).violations
        assert v.code_ref == "AASHTO-RDG-lighting"
        assert "AASHTO" in v.source or "AASHTO" in v.code_ref


class TestAdvisoryMode:
    def test_warns_without_clamping(self):
        spec = make_spec(code_mode="advisory")
        set_param(spec, "pole_height", 55)
        result = validate_spec(spec)
        assert not result.ok
        assert result.violations[0].corrected_value is None
        assert result.spec["parameters"][0]["value"] == 55


class TestUnitConversion:
    def test_metric_value_checked_against_imperial_rule(self):
        spec = make_spec(units="metric")
        # 3 m ~ 9.8 ft, below the 20 ft minimum -> clamp to 20 ft = 6.096 m
        set_param(spec, "pole_height", 3, unit="m")
        result = validate_spec(spec)
        [v] = result.violations
        assert v.limit_type == "min"
        assert result.spec["parameters"][0]["value"] == pytest.approx(6.096)

    def test_metric_value_in_range_passes(self):
        spec = make_spec(units="metric")
        set_param(spec, "pole_height", 9.0, unit="m")  # ~29.5 ft
        assert validate_spec(spec).ok


class TestEdgeCases:
    def test_unknown_asset_type_is_unchecked(self):
        result = validate_spec(make_spec(asset_type="warp_core"))
        assert result.ok
        assert result.checked is False

    def test_parameter_without_rule_passes(self):
        spec = make_spec()
        spec["parameters"].append(
            {"id": "fluting_count", "label": "Fluting", "type": "number", "value": 12}
        )
        assert validate_spec(spec).ok

    def test_min_only_rule(self):
        spec = {
            "asset_type": "traffic_sign",
            "name": "Stop",
            "units": "imperial",
            "parameters": [
                {"id": "mount_height", "label": "Mount Height", "type": "slider",
                 "value": 5, "unit": "ft", "code_ref": "MUTCD-2A.18"},
            ],
        }
        result = validate_spec(spec)
        [v] = result.violations
        assert v.limit_type == "min" and v.limit_value == 7
        assert result.spec["parameters"][0]["value"] == 7
        # no max on this rule: a tall mount is fine
        spec["parameters"][0]["value"] = 30
        assert validate_spec(spec).ok

    def test_result_serializes(self):
        spec = make_spec()
        set_param(spec, "pole_height", 5)
        d = validate_spec(spec).to_dict()
        assert d["ok"] is False and d["checked"] is True
        assert d["violations"][0]["parameter_id"] == "pole_height"

    def test_every_seeded_asset_type_has_rules(self):
        standards = load_standards()
        expected = {"street_light", "pedestrian_lamp", "traffic_sign", "bollard",
                    "bench", "handrail", "door", "fire_hydrant"}
        assert expected.issubset(set(standards))
        for asset_type in expected:
            for rule in standards[asset_type]["parameters"].values():
                assert rule.get("min") is not None
                assert rule["unit"] in {"ft", "in", "m", "cm", "mm"}
                if rule.get("max") is not None:
                    assert rule["max"] >= rule["min"]
                default = rule.get("default")
                assert default is not None and math.isfinite(default)
                assert default >= rule["min"]
                if rule.get("max") is not None:
                    assert default <= rule["max"]


class TestStandardsDbValidator:
    def test_real_db_is_sound(self):
        from standards.validator import validate_standards_db

        assert validate_standards_db(load_standards()) == []

    def test_catches_structural_problems(self):
        from standards.validator import validate_standards_db

        bad = {
            "widget": {
                "parameters": {
                    "height": {"min": 40, "max": 30, "default": 99, "unit": "furlong"},
                },
            },
        }
        errors = " ".join(validate_standards_db(bad))
        assert "max 30 < min 40" in errors
        assert "bad unit" in errors
        assert "code_ref" in errors
        assert "source" in errors
