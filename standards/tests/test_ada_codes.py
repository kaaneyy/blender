"""Pins the ADA/IBC dimensional enrichment added to the 8 seeded standards
entries (bench/handrail/door) plus the _meta.code_authorities registry.

This file is intentionally separate from test_validator.py (which guards the
validator's general behavior) — it exists to fail loudly if a specific,
citable ADA number regresses or is removed, and to fail if the change under
test is reverted (see TestEndToEndClamp below).
"""
import pytest

from standards.validator import load_standards, validate_spec


@pytest.fixture(scope="module")
def standards():
    return load_standards()


class TestCodeAuthoritiesRegistry:
    def test_meta_has_code_authorities(self, standards):
        authorities = standards["_meta"]["code_authorities"]
        assert "ADA" in authorities
        assert "AIA" in authorities

    def test_required_authority_keys_present(self, standards):
        authorities = standards["_meta"]["code_authorities"]
        expected = {"ADA", "AIA", "IBC", "ANSI_A117.1", "AASHTO", "MUTCD", "PROWAG", "AWWA"}
        assert expected.issubset(set(authorities))

    def test_ada_authority_name_is_real(self, standards):
        assert "Americans with Disabilities Act" in standards["_meta"]["code_authorities"]["ADA"]

    def test_aia_is_honestly_described_not_a_numbered_code(self, standards):
        aia = standards["_meta"]["code_authorities"]["AIA"]
        assert "American Institute of Architects" in aia
        # AIA must never be fabricated as a numbered dimensional-limit code_ref
        # anywhere in the DB (e.g. "AIA-903.4") — it is a professional body,
        # not a code with citable dimensional sections.
        for asset_type, entry in standards.items():
            if asset_type.startswith("_") or not isinstance(entry, dict):
                continue
            for rule in entry.get("parameters", {}).values():
                code_ref = rule.get("code_ref", "")
                assert not code_ref.upper().startswith("AIA"), (
                    f"{asset_type}: fabricated AIA-numbered code_ref {code_ref!r}"
                )


class TestDoorEnrichment:
    def test_clear_width_rule(self, standards):
        rule = standards["door"]["parameters"]["clear_width"]
        assert rule["min"] == 32
        assert rule["max"] is None
        assert rule["unit"] == "in"
        assert 32 <= rule["default"] <= 36
        assert "404" in rule["code_ref"]

    def test_threshold_height_rule(self, standards):
        rule = standards["door"]["parameters"]["threshold_height"]
        assert rule["min"] == 0
        assert rule["max"] == 0.5
        assert rule["unit"] == "in"
        assert rule["default"] == 0
        assert "404" in rule["code_ref"]

    def test_clear_height_rule_untouched(self, standards):
        # sanity: enrichment must not have clobbered the existing rule
        rule = standards["door"]["parameters"]["clear_height"]
        assert rule["min"] == 80


class TestHandrailEnrichment:
    def test_grip_diameter_rule(self, standards):
        rule = standards["handrail"]["parameters"]["grip_diameter"]
        assert rule["min"] == 1.25
        assert rule["max"] == 2.0
        assert rule["unit"] == "in"
        assert "505" in rule["code_ref"]

    def test_wall_clearance_rule(self, standards):
        rule = standards["handrail"]["parameters"]["wall_clearance"]
        assert rule["min"] == 1.5
        assert rule["max"] is None
        assert rule["unit"] == "in"
        assert "505" in rule["code_ref"]

    def test_height_rule_untouched(self, standards):
        rule = standards["handrail"]["parameters"]["height"]
        assert rule["min"] == 34 and rule["max"] == 38


class TestBenchEnrichment:
    def test_seat_length_rule(self, standards):
        rule = standards["bench"]["parameters"]["seat_length"]
        assert rule["min"] == 42
        assert rule["max"] is None
        assert rule["unit"] == "in"
        # 42 in is the ADA 903.3 "Size" clause, not 903.4 "Back Support" —
        # pin the exact section so a mis-citation regresses loudly.
        assert rule["code_ref"] == "ADA-903.3"

    def test_back_height_rule(self, standards):
        rule = standards["bench"]["parameters"]["back_height"]
        assert rule["min"] == 18
        assert rule["max"] is None
        assert rule["unit"] == "in"
        assert "903" in rule["code_ref"]

    def test_seat_height_and_depth_untouched(self, standards):
        seat_height = standards["bench"]["parameters"]["seat_height"]
        seat_depth = standards["bench"]["parameters"]["seat_depth"]
        assert seat_height["min"] == 17 and seat_height["max"] == 19
        assert seat_depth["min"] == 20 and seat_depth["max"] == 24


def _door_spec(clear_width_value, code_mode="strict"):
    return {
        "asset_type": "door",
        "name": "TestDoor",
        "units": "imperial",
        "code_mode": code_mode,
        "parameters": [
            {"id": "clear_width", "label": "Clear Width", "type": "slider",
             "value": clear_width_value, "unit": "in"},
        ],
    }


class TestEndToEndClamp:
    """A door spec's clear_width is validated against the real ADA-404.2.3
    32 in minimum. If the clear_width rule regresses (wrong number, wrong
    unit, or removed entirely), this test fails: with no rule at all the
    validator would report zero violations and leave the value at 30, and
    with a wrong min the corrected value would differ from 32."""

    def test_narrow_door_is_flagged_and_clamped_to_32(self):
        spec = _door_spec(30)
        result = validate_spec(spec)
        assert not result.ok
        [v] = result.violations
        assert v.parameter_id == "clear_width"
        assert v.limit_type == "min"
        assert v.limit_value == 32
        assert v.corrected_value == 32
        assert result.spec["parameters"][0]["value"] == 32
        assert "404" in v.code_ref

    def test_compliant_door_passes_untouched(self):
        spec = _door_spec(36)
        result = validate_spec(spec)
        assert result.ok
        assert result.spec["parameters"][0]["value"] == 36

    def test_advisory_mode_flags_without_clamping(self):
        spec = _door_spec(28, code_mode="advisory")
        result = validate_spec(spec)
        assert not result.ok
        assert result.violations[0].corrected_value is None
        assert result.spec["parameters"][0]["value"] == 28
