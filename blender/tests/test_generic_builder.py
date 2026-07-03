"""Tests for the expression evaluator and the generate-anything path."""
import json
import math
from pathlib import Path

import pytest

import blender.builders  # noqa: F401
from blender.builders.base import compute_primitives, resolve_material
from blender.builders.expr import ExprError, safe_eval

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_SPEC = json.loads((REPO_ROOT / "examples" / "park_bench.json").read_text())

IN = 0.0254
FT = 0.3048


class TestSafeEval:
    def test_arithmetic(self):
        env = {"a": 2.0, "b": 0.5}
        assert safe_eval("a * 3 + b", env) == 6.5
        assert safe_eval("-(a - b) / 2", env) == -0.75
        assert safe_eval("min(a, b, 10)", env) == 0.5
        assert safe_eval("max(a, 3)", env) == 3.0
        assert safe_eval("abs(-a)", env) == 2.0

    def test_plain_numbers_pass_through(self):
        assert safe_eval(4, {}) == 4.0
        assert safe_eval(0.25, {}) == 0.25

    def test_unknown_name_rejected(self):
        with pytest.raises(ExprError, match="Unknown name"):
            safe_eval("nope + 1", {"a": 1})

    @pytest.mark.parametrize("evil", [
        "__import__('os')",
        "a.__class__",
        "().__class__",
        "exec('x=1')",
        "[1][0]",
        "'a' + 'b'",
        "a ** 99",
        "lambda: 1",
    ])
    def test_code_execution_rejected(self, evil):
        with pytest.raises(ExprError):
            safe_eval(evil, {"a": 1.0})


class TestGenericBuilder:
    def test_bench_example_builds(self):
        prims = compute_primitives(BENCH_SPEC)
        names = [p.name for p in prims]
        assert len(names) == len(set(names))
        # backrest on, armrests off in the example
        assert any(p.component == "backrest" for p in prims)
        assert not any(p.component == "armrests" for p in prims)
        for p in prims:
            for v in list(p.location) + list(p.rotation):
                assert math.isfinite(v)

    def test_expressions_track_parameters(self):
        spec = json.loads(json.dumps(BENCH_SPEC))
        for p in spec["parameters"]:
            if p["id"] == "seat_height":
                p["value"] = 19  # in
        prims = compute_primitives(spec)
        leg = next(p for p in prims if p.name == "leg_fl")
        assert leg.params["size"][2] == pytest.approx(19 * IN)
        assert leg.location[2] == pytest.approx(19 * IN / 2)

    def test_visible_if_toggle(self):
        spec = json.loads(json.dumps(BENCH_SPEC))
        for t in spec["toggles"]:
            t["value"] = t["id"] == "armrests"  # armrests on, backrest off
        prims = compute_primitives(spec)
        components = {p.component for p in prims}
        assert "armrests" in components and "backrest" not in components

    def test_validator_clamps_custom_asset(self):
        from standards.validator import validate_spec

        spec = json.loads(json.dumps(BENCH_SPEC))
        for p in spec["parameters"]:
            if p["id"] == "seat_height":
                p["value"] = 30  # way above ADA 19 in max
        result = validate_spec(spec)
        [v] = result.violations
        assert v.code_ref == "ADA-903.5"
        leg = next(
            p for p in compute_primitives(result.spec) if p.name == "leg_fl"
        )
        assert leg.params["size"][2] == pytest.approx(19 * IN)

    def test_missing_builder_and_primitives_raises(self):
        with pytest.raises(ValueError, match="no 'primitives'"):
            compute_primitives({"asset_type": "warp_core", "parameters": []})


class TestMaterialOverrides:
    def test_preset_defaults(self):
        props = resolve_material(BENCH_SPEC, "slats")
        assert props["uv_scale"] == 2.5  # override from the example
        assert props["metallic"] == 0.0  # from wood_slat preset

    def test_color_override(self):
        props = resolve_material(BENCH_SPEC, "frame")
        r, g, b, a = props["base_color"]
        assert (round(r * 255), round(g * 255), round(b * 255)) == (0x2B, 0x2B, 0x30)
        assert props["roughness"] == 0.7

    def test_unknown_slot_falls_back(self):
        props = resolve_material(BENCH_SPEC, "mystery")
        assert props["metallic"] == 1.0  # galvanized_steel fallback
        assert props["uv_scale"] == 1.0 and props["emission"] == 0.0
