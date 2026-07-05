"""Tests for the buildability / load-path validator (connectivity.py):
contact graph, floating-part detection with nearest support + gap, below-
grade geometry, and dead declared connections."""
import json
from pathlib import Path

import blender.builders  # noqa: F401
from blender.builders.base import compute_primitives
from blender.builders.connectivity import buildability_errors, check_buildability

REPO_ROOT = Path(__file__).resolve().parents[2]


def load(name):
    return json.loads((REPO_ROOT / "examples" / name).read_text())


def findings_for(spec):
    return check_buildability(compute_primitives(spec), spec)


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
