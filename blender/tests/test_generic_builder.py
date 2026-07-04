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


class TestTiltedGeometry:
    def test_dimensionless_angle_param_drives_rotation(self):
        """Parameters without a unit (angles in degrees, counts) pass into
        expressions unchanged — no bogus length conversion — so tilted
        panels stay adjustable."""
        spec = {
            "asset_type": "solar_roof",
            "name": "TiltTest",
            "units": "imperial",
            "parameters": [
                {"id": "panel_tilt", "label": "Panel Tilt", "type": "slider",
                 "min": 0, "max": 60, "step": 1, "value": 30},
                {"id": "roof_width", "label": "Roof Width", "type": "slider",
                 "min": 3, "max": 8, "step": 0.5, "value": 5, "unit": "ft"},
            ],
            "toggles": [],
            "primitives": [
                {"kind": "box", "name": "roof", "component": "roof",
                 "location": [0, 0, 1.0],
                 "params": {"size": ["roof_width", 1.2, 0.05]}},
                {"kind": "box", "name": "panel", "component": "panels",
                 "location": [0, 0, 1.15],
                 "rotation": [0, "panel_tilt * 0.01745", 0],
                 "params": {"size": ["roof_width - 0.2", 1.0, 0.03]}},
            ],
        }
        prims = {p.name: p for p in compute_primitives(spec)}
        # angle stays 30 degrees -> 0.5235 rad (NOT 30 ft -> 9.14 "meters")
        assert prims["panel"].rotation[1] == pytest.approx(30 * 0.01745, abs=1e-4)
        # length param still converts: 5 ft -> 1.524 m
        assert prims["roof"].params["size"][0] == pytest.approx(1.524)
        # slider change tilts the panel
        spec["parameters"][0]["value"] = 45
        prims = {p.name: p for p in compute_primitives(spec)}
        assert prims["panel"].rotation[1] == pytest.approx(45 * 0.01745, abs=1e-4)


class TestFabricationKinds:
    """Part B vocabulary in the generate-anything path."""

    def _spec(self, prims):
        return {
            "asset_type": "prop", "name": "T", "units": "metric",
            "parameters": [
                {"id": "h", "label": "H", "type": "slider",
                 "min": 0.5, "max": 3, "step": 0.1, "value": 2, "unit": "m"},
            ],
            "toggles": [],
            "primitives": prims,
        }

    def test_lathe_named_profile(self):
        prims = compute_primitives(self._spec([
            {"kind": "lathe", "name": "globe", "component": "top",
             "location": [0, 0, "h"],
             "params": {"profile": "acorn", "radius": 0.18, "depth": 0.4}},
        ]))
        [globe] = prims
        assert globe.kind == "lathe" and globe.params["profile"] == "acorn"
        from blender.builders.hardware import _aabb
        center, half = _aabb(globe)
        assert center[2] == pytest.approx(2 + 0.2)  # profile spans z 0..0.4
        assert half[0] == pytest.approx(0.85 * 0.18)

    def test_lathe_raw_profile_with_expressions(self):
        prims = compute_primitives(self._spec([
            {"kind": "lathe", "name": "vase", "component": "body",
             "params": {"profile": [[0.1, 0], ["h/10", "h/4"], [0.05, "h/2"]]}},
        ]))
        assert prims[0].params["profile"][1] == (0.2, 0.5)

    def test_sweep_path_and_taper(self):
        prims = compute_primitives(self._spec([
            {"kind": "sweep", "name": "rail", "component": "rail",
             "params": {"path": [[0, 0, "h"], [1, 0, "h"], [2, 0, "h - 0.5"]],
                        "radius": 0.04, "radius_end": 0.02}},
        ]))
        [rail] = prims
        assert rail.params["path"][2] == (2.0, 0.0, 1.5)
        from blender.builders.hardware import _aabb
        center, half = _aabb(rail)
        assert center[0] == pytest.approx(1.0)  # path bbox center, not location
        assert half[0] == pytest.approx(1.0 + 0.04)

    def test_tube_and_loft(self):
        prims = compute_primitives(self._spec([
            {"kind": "tube", "name": "post", "component": "post",
             "location": [0, 0, "h/2"],
             "params": {"radius": 0.06, "wall": 0.004, "depth": "h"}},
            {"kind": "loft", "name": "hood", "component": "hood",
             "location": [0, 0, "h + 0.1"],
             "params": {"depth": 0.2,
                        "profile_start": {"shape": "rect", "w": 0.3, "h": 0.2},
                        "profile_end": {"shape": "ellipse", "w": "h/10", "h": 0.08}}},
        ]))
        post, hood = prims
        assert post.params["wall"] == 0.004
        assert hood.params["profile_end"]["w"] == pytest.approx(0.2)

    def test_array_expansion(self):
        prims = compute_primitives(self._spec([
            {"kind": "box", "name": "picket", "component": "fence",
             "location": [0, 0, 0.5],
             "array": {"count": 5, "step": ["h/8", 0, 0]},
             "params": {"size": [0.04, 0.04, 1.0]}},
        ]))
        assert len(prims) == 5
        assert [p.name for p in prims] == [f"picket_{i}" for i in range(1, 6)]
        assert prims[3].location[0] == pytest.approx(3 * 0.25)

    def test_cut_marks_negative_space(self):
        prims = compute_primitives(self._spec([
            {"kind": "box", "name": "plate", "component": "base",
             "params": {"size": [0.4, 0.4, 0.02]}},
            {"kind": "cylinder", "name": "hole", "component": "base", "cut": True,
             "params": {"radius": 0.02, "depth": 0.1}},
        ]))
        hole = next(p for p in prims if p.name == "hole")
        assert hole.cut is True
        # cut prims never receive hardware
        from blender.builders.hardware import compute_hardware
        assert all("hole" not in p.name for p in compute_hardware(prims))

    def test_all_cut_spec_rejected(self):
        with pytest.raises(ValueError, match="no visible"):
            compute_primitives(self._spec([
                {"kind": "box", "name": "x", "component": "a", "cut": True,
                 "params": {"size": [1, 1, 1]}},
            ]))
