"""Pins the new accessible asset-type entries added to the standards DB
(drinking_fountain, ramp, accessible_table, trash_receptacle) — real ADA
dimensional limits for asset types the generic "generate anything" builder
path can target but that previously had no DB entry (validator returned
checked=False for them).

Separate from test_ada_codes.py (Brief 1's door/handrail/bench enrichment)
and test_validator.py (general validator behavior) — this file exists to
fail loudly if a specific, citable ADA number for one of these new asset
types regresses or is removed, and to fail if the change under test is
reverted (see TestEndToEndClamp below).
"""
import pytest

from standards.validator import load_standards, validate_spec, validate_standards_db


@pytest.fixture(scope="module")
def standards():
    return load_standards()


class TestRealDbStillSound:
    def test_new_entries_pass_structural_validation(self, standards):
        # Redundant with test_validator.py's whole-file check, but pins that
        # the new entries specifically don't break validate_standards_db.
        assert validate_standards_db(standards) == []


class TestDrinkingFountainEntry:
    def test_entry_has_source(self, standards):
        assert standards["drinking_fountain"]["source"]

    def test_spout_height_rule(self, standards):
        rule = standards["drinking_fountain"]["parameters"]["spout_height"]
        assert rule["min"] == 0
        assert rule["max"] == 36
        assert rule["unit"] == "in"
        assert 0 <= rule["default"] <= 36
        assert "602" in rule["code_ref"]

    def test_knee_clearance_height_rule(self, standards):
        rule = standards["drinking_fountain"]["parameters"]["knee_clearance_height"]
        assert rule["min"] == 27
        assert rule["max"] is None
        assert rule["unit"] == "in"
        assert "306" in rule["code_ref"]

    def test_spout_reach_rule(self, standards):
        rule = standards["drinking_fountain"]["parameters"]["spout_reach"]
        assert rule["min"] == 0
        assert rule["max"] == 5
        assert rule["unit"] == "in"
        assert "602" in rule["code_ref"]


class TestRampEntry:
    def test_entry_has_source(self, standards):
        assert standards["ramp"]["source"]

    def test_clear_width_rule(self, standards):
        rule = standards["ramp"]["parameters"]["clear_width"]
        assert rule["min"] == 36
        assert rule["max"] is None
        assert rule["unit"] == "in"
        assert "405.5" in rule["code_ref"]

    def test_max_rise_per_run_rule(self, standards):
        rule = standards["ramp"]["parameters"]["max_rise_per_run"]
        assert rule["min"] == 0
        assert rule["max"] == 30
        assert rule["unit"] == "in"
        assert "405.6" in rule["code_ref"]

    def test_landing_length_rule(self, standards):
        rule = standards["ramp"]["parameters"]["landing_length"]
        assert rule["min"] == 60
        assert rule["max"] is None
        assert rule["unit"] == "in"
        assert "405.7.3" in rule["code_ref"]

    def test_slope_is_not_encoded_as_a_length_parameter(self, standards):
        # A 1:12 slope is a ratio, not a length — it must never show up as a
        # 'parameters' rule with a length unit.
        params = standards["ramp"]["parameters"]
        assert "slope" not in params
        assert "max_slope" not in params


class TestAccessibleTableEntry:
    def test_entry_has_source(self, standards):
        assert standards["accessible_table"]["source"]

    def test_surface_height_rule(self, standards):
        rule = standards["accessible_table"]["parameters"]["surface_height"]
        assert rule["min"] == 28
        assert rule["max"] == 34
        assert rule["unit"] == "in"
        assert "902.3" in rule["code_ref"]

    def test_knee_clearance_height_rule(self, standards):
        rule = standards["accessible_table"]["parameters"]["knee_clearance_height"]
        assert rule["min"] == 27
        assert rule["max"] is None
        assert rule["unit"] == "in"
        assert "306" in rule["code_ref"]

    def test_toe_clearance_depth_rule(self, standards):
        rule = standards["accessible_table"]["parameters"]["toe_clearance_depth"]
        assert rule["min"] == 17
        assert rule["max"] is None
        assert rule["unit"] == "in"
        assert "306.2.3" in rule["code_ref"]


class TestTrashReceptacleEntry:
    def test_entry_has_source(self, standards):
        assert standards["trash_receptacle"]["source"]

    def test_operable_part_height_rule(self, standards):
        rule = standards["trash_receptacle"]["parameters"]["operable_part_height"]
        assert rule["min"] == 15
        assert rule["max"] == 48
        assert rule["unit"] == "in"
        assert "308" in rule["code_ref"]


def _spec(asset_type, param_id, value, unit="in", code_mode="strict"):
    return {
        "asset_type": asset_type,
        "name": "TestAsset",
        "units": "imperial",
        "code_mode": code_mode,
        "parameters": [
            {"id": param_id, "label": param_id, "type": "slider",
             "value": value, "unit": unit},
        ],
    }


class TestEndToEndClamp:
    """These new asset types previously had no DB entry at all, so
    validate_spec returned checked=False and never touched their values. If
    the drinking_fountain.spout_height rule regresses (wrong number, wrong
    unit, or removed entirely), this test fails: with no rule the validator
    would report checked=False / zero violations and leave the value at 40,
    and with a wrong max the corrected value would differ from 36."""

    def test_asset_type_is_now_checked(self):
        spec = _spec("drinking_fountain", "spout_height", 40)
        result = validate_spec(spec)
        assert result.checked is True

    def test_overreaching_spout_is_flagged_and_clamped_to_36(self):
        spec = _spec("drinking_fountain", "spout_height", 40)
        result = validate_spec(spec)
        assert not result.ok
        [v] = result.violations
        assert v.parameter_id == "spout_height"
        assert v.limit_type == "max"
        assert v.limit_value == 36
        assert v.corrected_value == 36
        assert result.spec["parameters"][0]["value"] == 36
        assert "602" in v.code_ref

    def test_compliant_spout_height_passes_untouched(self):
        spec = _spec("drinking_fountain", "spout_height", 34)
        result = validate_spec(spec)
        assert result.ok
        assert result.spec["parameters"][0]["value"] == 34

    def test_narrow_ramp_is_flagged_and_clamped_to_36(self):
        spec = _spec("ramp", "clear_width", 30)
        result = validate_spec(spec)
        assert not result.ok
        [v] = result.violations
        assert v.parameter_id == "clear_width"
        assert v.limit_type == "min"
        assert v.limit_value == 36
        assert v.corrected_value == 36
        assert result.spec["parameters"][0]["value"] == 36
        assert "405" in v.code_ref

    def test_advisory_mode_flags_without_clamping(self):
        spec = _spec("drinking_fountain", "spout_height", 40, code_mode="advisory")
        result = validate_spec(spec)
        assert not result.ok
        assert result.violations[0].corrected_value is None
        assert result.spec["parameters"][0]["value"] == 40
