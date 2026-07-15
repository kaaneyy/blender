"""Tests for the buildability / load-path validator (connectivity.py):
contact graph, floating-part detection with nearest support + gap, below-
grade geometry, and dead declared connections; plus the deterministic
scale-sanity checker (check_scale_sanity)."""
import json
from pathlib import Path

import pytest

import blender.builders  # noqa: F401
from blender.builders.base import compute_primitives
from blender.builders.connectivity import (
    buildability_errors,
    check_buildability,
    check_scale_sanity,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_NAMES = sorted(p.name for p in (REPO_ROOT / "examples").glob("*.json"))


def load(name):
    return json.loads((REPO_ROOT / "examples" / name).read_text())


def findings_for(spec):
    return check_buildability(compute_primitives(spec), spec)


def scale_findings_for(spec):
    return check_scale_sanity(compute_primitives(spec), spec)


class TestLoadPath:
    def test_grounded_examples_pass(self):
        for name in ("park_bench.json", "street_light.json"):
            findings = findings_for(load(name))
            assert buildability_errors(findings) == [], name

    def test_floating_component_reported_with_gap(self):
        spec = load("park_bench.json")
        # raise every seat slat well above frame AND backrest: the seat floats
        for p in spec["primitives"]:
            if p["component"] == "seat":
                p["location"][2] = "seat_height + 1.5"
        findings = findings_for(spec)
        errors = buildability_errors(findings)
        seat = next(f for f in errors if "'seat'" in f["message"])
        assert "floats" in seat["message"]
        assert "mm away" in seat["message"]
        assert seat["parameter_id"] == "__buildability__"
        assert seat["severity"] == "error"

    def test_barely_resting_on_backrest_is_not_floating(self):
        # geometric truth: a raised seat that still overlaps the backrest has
        # a (silly but real) load path — the graph must find it
        spec = load("park_bench.json")
        for p in spec["primitives"]:
            if p["component"] == "seat":
                p["location"][2] = "seat_height + 0.5"
        findings = findings_for(spec)
        assert not any(f["limit_type"] == "floating" for f in findings)

    def test_indirect_support_counts(self):
        # slats sit on rails which sit on legs: seat never touches the
        # ground directly but reaches it through the graph
        findings = findings_for(load("park_bench.json"))
        assert not any(f["limit_type"] == "floating" for f in findings)


class TestBelowGrade:
    def test_below_ground_geometry_is_flagged(self):
        spec = load("park_bench.json")
        spec["primitives"].append({
            "kind": "box", "name": "buried", "component": "frame",
            "material_slot": "frame", "location": [0, 0, -0.1],
            "params": {"size": [0.2, 0.2, 0.4]},
        })
        findings = findings_for(spec)
        below = [f for f in findings if f["limit_type"] == "below_ground"]
        assert below and below[0]["severity"] == "warning"
        assert "below grade" in below[0]["message"]


class TestDeclarations:
    def test_dead_declaration_reported(self):
        spec = load("park_bench.json")
        spec["connections"] = [
            {"a": "seat", "b": "ghost_component", "type": "through_bolt"},
        ]
        findings = findings_for(spec)
        dead = [f for f in findings if f["limit_type"] == "unmatched_declaration"]
        assert dead and "don't exist" in dead[0]["message"]

    def test_no_contact_declaration_reports_gap(self):
        spec = load("park_bench.json")
        # armrests are toggled OFF -> declared armrest connections in the
        # example are pruned with the geometry; instead declare a joint
        # between two parts that exist but never touch
        spec["connections"] = [
            {"a": "seat", "b": "backrest/back_slat_high", "type": "through_bolt"},
        ]
        findings = findings_for(spec)
        dead = [f for f in findings if f["limit_type"] == "unmatched_declaration"]
        assert dead and "mm apart" in dead[0]["message"]

    def test_ground_declarations_are_exempt(self):
        spec = load("park_bench.json")
        spec["connections"] = [{"a": "frame", "b": "ground", "type": "none"}]
        findings = findings_for(spec)
        assert not any(f["limit_type"] == "unmatched_declaration" for f in findings)


def _pergola_with_solar(size):
    """The WHY repro: pergola.json (default connection_hardware=true) plus a
    seated 'solar' box component of the given (w, d, h) resting on the front
    beam. Scale is the only thing wrong with it — it is fully supported and
    declares no connections, so check_buildability/audit are both silent."""
    spec = load("pergola.json")
    spec["primitives"].append({
        "kind": "box", "name": "panel", "component": "solar",
        "material_slot": "beams",
        "location": [0, "span_y/2", "post_height + beam_depth + 0.05"],
        "params": {"size": list(size)},
    })
    return spec


class TestScaleSanity:
    @pytest.mark.parametrize("name", EXAMPLE_NAMES)
    def test_bundled_examples_are_scale_clean(self, name):
        # false-positive gate: every shipped example must produce zero scale
        # findings — a neutered check that always returns [] would also pass
        # this alone, which is why the toy/giant/envelope tests below assert
        # real findings on purpose-built reproductions.
        findings = scale_findings_for(load(name))
        assert findings == [], (name, findings)

    def test_toy_panel_is_a_scale_outlier(self):
        # reproduces the bug report: a 0.3x0.2x0.05 m "solar panel" seated on
        # a ~3.7 m pergola is toy-scale relative to the structure.
        spec = _pergola_with_solar((0.3, 0.2, 0.05))
        findings = scale_findings_for(spec)
        outliers = [f for f in findings if f["kind"] == "scale_outlier"]
        assert len(outliers) == 1
        finding = outliers[0]
        assert finding["component"] == "solar"
        assert finding["severity"] == "warning"
        assert "solar" in finding["message"]
        assert "0.30 m" in finding["message"]
        # no other scale finding should fire alongside it
        assert findings == outliers

    def test_giant_panel_is_a_scale_giant(self):
        # the inverse bug: a 12x9x0.08 m "solar panel" dwarfs the same
        # pergola it's supposedly mounted on.
        spec = _pergola_with_solar((12, 9, 0.08))
        findings = scale_findings_for(spec)
        giants = [f for f in findings if f["kind"] == "scale_giant"]
        assert len(giants) == 1
        finding = giants[0]
        assert finding["component"] == "solar"
        assert finding["severity"] == "warning"
        assert "solar" in finding["message"]

    def test_oversized_envelope_is_flagged(self):
        spec = {
            "asset_type": "custom", "name": "Huge", "units": "metric",
            "code_mode": "advisory", "parameters": [], "toggles": [],
            "materials": [{"slot": "frame", "preset": "wood_slat"}],
            "components": ["frame"], "connections": [],
            "primitives": [
                {"kind": "box", "name": "slab", "component": "frame",
                 "material_slot": "frame", "location": [0, 0, 20],
                 "params": {"size": [40, 5, 5]}},
            ],
        }
        findings = scale_findings_for(spec)
        extreme = [f for f in findings if f["kind"] == "envelope_extreme"]
        assert len(extreme) == 1
        assert extreme[0]["component"] is None
        assert extreme[0]["severity"] == "warning"

    def test_hardware_and_cut_prims_never_contribute(self):
        # connection_hardware is already on for pergola.json (bolts/washers
        # get generated as tiny 'hardware'-component prims); a giant cut
        # prim (negative space) is added on top to prove neither kind
        # pollutes the envelope math.
        spec = load("pergola.json")
        spec["primitives"].append({
            "kind": "box", "name": "phantom_cut", "component": "posts",
            "material_slot": "posts", "cut": True,
            "location": [0, 0, 5], "params": {"size": [100, 100, 100]},
        })
        findings = scale_findings_for(spec)
        assert findings == []

    def test_never_raises_on_valid_primitives(self):
        # a spec with a single tiny grounded component and no siblings must
        # not raise even though most ratio guards divide by other quantities
        spec = {
            "asset_type": "custom", "name": "Lonely", "units": "metric",
            "code_mode": "advisory", "parameters": [], "toggles": [],
            "materials": [{"slot": "frame", "preset": "wood_slat"}],
            "components": ["frame"], "connections": [],
            "primitives": [
                {"kind": "box", "name": "cube", "component": "frame",
                 "material_slot": "frame", "location": [0, 0, 0.05],
                 "params": {"size": [0.1, 0.1, 0.1]}},
            ],
        }
        findings = check_scale_sanity(compute_primitives(spec), spec)
        assert isinstance(findings, list)

    def test_neutered_check_would_fail_this_suite(self):
        # guard against a no-op regression: at least one of the purpose-built
        # reproductions above must yield a non-empty result.
        spec = _pergola_with_solar((0.3, 0.2, 0.05))
        assert scale_findings_for(spec) != []
