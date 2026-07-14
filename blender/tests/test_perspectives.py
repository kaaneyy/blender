"""Tests for perspectives.py: the four discipline evaluators behind the
Improve flow's professional-framing feature. Python-only module (no TS
mirror by design — see the module docstring), so these behavior tests are
the only guard.

Bucketing is the load-bearing behavior here: a floating-part finding must
land under civil and nowhere else, a collision/joint finding must land
under mechanical, a material-slot gap must land under design. If bucketing
were ever removed (e.g. every finding dumped into every perspective), these
tests catch it."""
import copy
import json
from pathlib import Path

import pytest

import blender.builders  # noqa: F401
from blender.builders.perspectives import (
    PERSPECTIVES,
    evaluate_all,
    evaluate_perspective,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def load(name):
    return json.loads((REPO_ROOT / "examples" / name).read_text())


def floating_spec():
    """A component ('orb') with no load path to the ground."""
    return {
        "asset_type": "sculpture", "name": "F", "units": "metric",
        "parameters": [],
        "primitives": [
            {"kind": "box", "name": "base", "component": "base",
             "location": [0, 0, 0.1], "params": {"size": [0.5, 0.5, 0.2]}},
            {"kind": "sphere", "name": "orb", "component": "orb",
             "location": [0, 0, 1.5], "params": {"radius": 0.2}},
        ],
    }


def bad_expression_spec():
    """A primitive expression referencing an id that doesn't exist — makes
    compute_primitives raise outright."""
    return {
        "asset_type": "table", "name": "T", "units": "metric",
        "parameters": [
            {"id": "seat_height", "label": "H", "type": "slider",
             "min": 0.3, "max": 1.0, "step": 0.05, "value": 0.45, "unit": "m"},
        ],
        "primitives": [
            {"kind": "box", "name": "top", "component": "top",
             "location": [0, 0, "seat_heigth + 0.1"],  # typo'd id
             "params": {"size": [1, 0.5, 0.04]}},
        ],
    }


def material_gap_spec():
    """A primitive's material_slot ('trim') isn't declared in materials[]."""
    return {
        "asset_type": "fixture", "name": "M", "units": "metric",
        "parameters": [],
        "materials": [{"slot": "frame", "preset": "galvanized_steel"}],
        "primitives": [
            {"kind": "box", "name": "post", "component": "post",
             "material_slot": "trim", "location": [0, 0, 0.5],
             "params": {"size": [0.1, 0.1, 1.0]}},
        ],
    }


class TestContract:
    def test_evaluate_all_returns_four_entries_in_contract_order(self):
        res = evaluate_all(load("park_bench.json"))
        assert len(res) == 4
        expected = [
            ("architecture", "Architecture", "\U0001F3DB️"),
            ("mechanical", "Mechanical engineering", "\U0001F529"),
            ("civil", "Civil / structural engineering", "\U0001F3D7️"),
            ("design", "Industrial design", "\U0001F3A8"),
        ]
        for entry, (pid, label, icon) in zip(res, expected):
            assert entry["id"] == pid
            assert entry["label"] == label
            assert entry["icon"] == icon
            assert set(entry) == {"id", "label", "icon", "metrics", "findings"}

    def test_perspectives_tuple_matches(self):
        assert [p["id"] for p in PERSPECTIVES] == [
            "architecture", "mechanical", "civil", "design",
        ]

    def test_unknown_perspective_raises(self):
        with pytest.raises(ValueError):
            evaluate_perspective("nope", load("park_bench.json"))

    def test_findings_shape(self):
        for entry in evaluate_all(load("park_bench.json")):
            for f in entry["findings"]:
                assert set(f) == {"severity", "kind", "message", "source"}
                assert f["severity"] in ("error", "warning", "info")
                assert f["source"] == "checks"

    def test_does_not_mutate_input_spec(self):
        spec = load("park_bench.json")
        before = copy.deepcopy(spec)
        evaluate_all(spec)
        assert spec == before


class TestBrokenSpec:
    def test_returns_four_build_failure_entries(self):
        res = evaluate_all(bad_expression_spec())
        assert len(res) == 4
        for entry in res:
            assert entry["metrics"] == {}
            assert len(entry["findings"]) == 1
            f = entry["findings"][0]
            assert f["severity"] == "error"
            assert f["kind"] == "build_failure"
            assert "seat_heigth" in f["message"]

    def test_evaluate_all_never_raises_for_broken_dict_input(self):
        # a spec missing everything meaningful still returns 4 entries
        res = evaluate_all({})
        assert len(res) == 4


class TestCivilBucketing:
    def test_floating_part_is_civil_only(self):
        res = {e["id"]: e for e in evaluate_all(floating_spec())}
        civil_kinds = {f["kind"] for f in res["civil"]["findings"]}
        assert "floating" in civil_kinds
        for pid in ("architecture", "mechanical", "design"):
            kinds = {f["kind"] for f in res[pid]["findings"]}
            assert "floating" not in kinds

    def test_civil_metrics_present(self):
        entry = evaluate_perspective("civil", load("street_light.json"))
        assert entry["metrics"]["height_m"] > 0
        assert entry["metrics"]["footprint_m2"] > 0
        assert entry["metrics"]["grounded_components"] >= 1

    def test_below_grade_bucketed_under_civil(self):
        spec = {
            "asset_type": "fixture", "name": "S", "units": "metric",
            "parameters": [],
            "materials": [{"slot": "m", "preset": "galvanized_steel"}],
            "primitives": [
                {"kind": "box", "name": "post", "component": "post",
                 "material_slot": "m", "location": [0, 0, 0.42],  # 80mm sunk
                 "params": {"size": [0.1, 0.1, 1.0]}},
            ],
        }
        entry = evaluate_perspective("civil", spec)
        kinds = {f["kind"] for f in entry["findings"]}
        assert "below_grade" in kinds


class TestMechanicalBucketing:
    def test_park_bench_collision_is_mechanical_only(self):
        # a known defect in the example fixture: the frame<->backrest
        # through-bolts pass through the rear seat slat, a real "collision"
        # audit finding.
        spec = load("park_bench.json")
        res = {e["id"]: e for e in evaluate_all(spec)}
        mech_kinds = {f["kind"] for f in res["mechanical"]["findings"]}
        assert "collision" in mech_kinds
        for pid in ("architecture", "civil", "design"):
            kinds = {f["kind"] for f in res[pid]["findings"]}
            assert "collision" not in kinds

    def test_mechanical_metrics_present(self):
        entry = evaluate_perspective("mechanical", load("park_bench.json"))
        assert entry["metrics"]["joint_count"] > 0
        assert entry["metrics"]["fastener_count"] > 0
        assert entry["metrics"]["connection_types"] >= 1

    def test_mechanical_metrics_do_not_mutate_toggles(self):
        spec = load("bike_rack.json")
        before_toggles = copy.deepcopy(spec.get("toggles"))
        evaluate_perspective("mechanical", spec)
        assert spec.get("toggles") == before_toggles


class TestArchitectureHeuristics:
    def test_tower_like_proportions_flagged(self):
        spec = {
            "asset_type": "fixture", "name": "Tower", "units": "metric",
            "parameters": [],
            "materials": [{"slot": "m", "preset": "galvanized_steel"}],
            "primitives": [
                {"kind": "box", "name": "mast", "component": "mast",
                 "material_slot": "m", "location": [0, 0, 5.0],
                 "params": {"size": [0.1, 0.1, 10.0]}},
            ],
        }
        entry = evaluate_perspective("architecture", spec)
        kinds = {f["kind"] for f in entry["findings"]}
        assert "tower_massing" in kinds
        assert entry["metrics"]["height_m"] == pytest.approx(10.0)

    def test_seat_height_surface_flagged_human_scale(self):
        entry = evaluate_perspective("architecture", load("park_bench.json"))
        kinds = {f["kind"] for f in entry["findings"]}
        assert "human_scale" in kinds

    def test_symmetric_asset_reports_no_asymmetry(self):
        # park bench is built symmetric about x=0
        entry = evaluate_perspective("architecture", load("park_bench.json"))
        kinds = {f["kind"] for f in entry["findings"]}
        assert "asymmetric" not in kinds

    def test_asymmetric_asset_flagged(self):
        spec = {
            "asset_type": "fixture", "name": "Lopsided", "units": "metric",
            "parameters": [],
            "materials": [{"slot": "m", "preset": "galvanized_steel"}],
            "primitives": [
                {"kind": "box", "name": "post", "component": "post",
                 "material_slot": "m", "location": [0.6, 0, 0.5],
                 "params": {"size": [0.1, 0.1, 1.0]}},
            ],
        }
        entry = evaluate_perspective("architecture", spec)
        kinds = {f["kind"] for f in entry["findings"]}
        assert "asymmetric" in kinds

    def test_architecture_metrics_present(self):
        entry = evaluate_perspective("architecture", load("street_light.json"))
        assert entry["metrics"]["component_count"] >= 1
        assert entry["metrics"]["width_m"] >= 0
        assert entry["metrics"]["depth_m"] >= 0


class TestDesignBucketing:
    def test_undeclared_material_slot_is_design_only(self):
        res = {e["id"]: e for e in evaluate_all(material_gap_spec())}
        design_kinds = {f["kind"] for f in res["design"]["findings"]}
        assert "missing_material" in design_kinds
        for pid in ("architecture", "mechanical", "civil"):
            kinds = {f["kind"] for f in res[pid]["findings"]}
            assert "missing_material" not in kinds

    def test_implicit_hardware_slot_not_flagged(self):
        # generated connection hardware uses material_slot "hardware" with a
        # documented silent fallback — never a "missing material" finding
        spec = load("bike_rack.json")
        toggles = spec.setdefault("toggles", [])
        for t in toggles:
            if t.get("id") == "connection_hardware":
                t["value"] = True
                break
        else:
            toggles.append({"id": "connection_hardware",
                            "label": "Connection Hardware", "value": True})
        entry = evaluate_perspective("design", spec)
        assert all(f["kind"] != "missing_material" for f in entry["findings"])

    def test_monochrome_palette_flagged(self):
        spec = {
            "asset_type": "fixture", "name": "Mono", "units": "metric",
            "parameters": [],
            "materials": [
                {"slot": "a", "preset": "powder_coat_black"},
                {"slot": "b", "preset": "galvanized_steel", "color": "#020202"},
            ],
            "primitives": [
                {"kind": "box", "name": "p1", "component": "p1",
                 "material_slot": "a", "location": [0, 0, 0.5],
                 "params": {"size": [0.1, 0.1, 1.0]}},
            ],
        }
        # force both slots to resolve to the identical color override
        spec["materials"][0]["color"] = "#020202"
        entry = evaluate_perspective("design", spec)
        kinds = {f["kind"] for f in entry["findings"]}
        assert "monochrome_palette" in kinds

    def test_design_metrics_present(self):
        entry = evaluate_perspective("design", load("park_bench.json"))
        assert entry["metrics"]["slot_count"] == 2
        assert entry["metrics"]["preset_coverage_fraction"] == 1.0
        assert entry["metrics"]["distinct_color_count"] >= 1


class TestCleanBundledExamples:
    @pytest.mark.parametrize("example", [
        "street_light.json", "bike_rack.json", "planter.json", "pergola.json",
    ])
    def test_evaluates_without_crashing(self, example):
        res = evaluate_all(load(example))
        assert len(res) == 4
        for entry in res:
            assert isinstance(entry["metrics"], dict)
            assert isinstance(entry["findings"], list)
