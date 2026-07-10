"""Tests for the deterministic connection auditor (audit.py) — the machine
behind the app's "Check connections" button. Parity target:
frontend/src/builders/audit.ts."""
import json
from pathlib import Path

import pytest

import blender.builders  # noqa: F401
from blender.builders.audit import apply_audit_fixes, audit_connections

REPO_ROOT = Path(__file__).resolve().parents[2]


def load(name):
    return json.loads((REPO_ROOT / "examples" / name).read_text())


def base_spec(prims, connections=None):
    return {
        "asset_type": "fixture", "name": "F", "units": "metric",
        "parameters": [], "primitives": prims,
        "materials": [{"slot": "m", "preset": "galvanized_steel"}],
        "connections": connections or [],
    }


BOX = {"kind": "box", "name": "post", "component": "post", "material_slot": "m",
       "location": [0, 0, 0.5], "params": {"size": [0.1, 0.1, 1.0]}}


class TestCleanAssets:
    @pytest.mark.parametrize("example", ["street_light.json", "bike_rack.json",
                                         "planter.json"])
    def test_bundled_examples_audit_clean(self, example):
        report = audit_connections(load(example))
        assert report["findings"] == []
        assert report["components"] >= 1

    def test_park_bench_reports_backrest_bolts_through_slat(self):
        # a genuine defect in the example: the frame<->backrest through-bolts
        # pass through the rear seat slat. Declared joints are report-only
        # (no auto-remove), so both findings arrive without a fix.
        report = audit_connections(load("park_bench.json"))
        kinds = {f["kind"] for f in report["findings"]}
        assert kinds == {"collision"}
        assert all(f["fix"] is None for f in report["findings"])


class TestFindingsAndFixes:
    def test_floating_component_gets_seating_fix(self):
        shelf = {"kind": "box", "name": "shelf", "component": "shelf",
                 "material_slot": "m", "location": [0.4, 0, 1.2],
                 "params": {"size": [0.5, 0.3, 0.04]}}
        spec = base_spec([BOX, shelf])
        report = audit_connections(spec)
        f = next(f for f in report["findings"] if f["kind"] == "floating")
        assert f["severity"] == "error"
        assert f["component"] == "shelf"
        assert f["fix"] and f["fix"]["ops"][0]["op"] == "nudge"
        fixed = apply_audit_fixes(spec, report["findings"])
        assert audit_connections(fixed)["findings"] == []

    def test_below_grade_raised_to_grade(self):
        sunk = dict(BOX, location=[0, 0, 0.42])  # 80mm below grade
        spec = base_spec([sunk])
        report = audit_connections(spec)
        f = next(f for f in report["findings"] if f["kind"] == "below_grade")
        assert f["fix"]["ops"][0]["delta"][2] == pytest.approx(0.08, abs=1e-9)
        fixed = apply_audit_fixes(spec, report["findings"])
        assert audit_connections(fixed)["findings"] == []

    def test_dead_declaration_removed(self):
        spec = base_spec([BOX], connections=[
            {"a": "ghost", "b": "post", "type": "weld"},
        ])
        report = audit_connections(spec)
        f = next(f for f in report["findings"] if f["kind"] == "dead_declaration")
        fixed = apply_audit_fixes(spec, [f])
        assert fixed["connections"] == []
        assert audit_connections(fixed)["findings"] == []

    def test_gap_declaration_and_one_nudge_per_component(self):
        shelf = {"kind": "box", "name": "shelf", "component": "shelf",
                 "material_slot": "m", "location": [0.4, 0, 1.2],
                 "params": {"size": [0.5, 0.3, 0.04]}}
        spec = base_spec([BOX, shelf], connections=[
            {"a": "post", "b": "shelf", "type": "through_bolt"},
        ])
        report = audit_connections(spec)
        kinds = [f["kind"] for f in report["findings"]]
        assert "floating" in kinds and "gap_declaration" in kinds
        # both findings would nudge 'shelf': only the first carries the fix,
        # so applying can never stack two moves computed from the same start
        fixes = [f for f in report["findings"] if f["fix"]]
        assert len(fixes) == 1
        fixed = apply_audit_fixes(spec, report["findings"])
        assert audit_connections(fixed)["findings"] == []

    def test_sliver_joint_seated_deeper(self):
        lid = {"kind": "box", "name": "lid", "component": "lid",
               "material_slot": "m", "location": [0, 0, 1.018],
               "params": {"size": [0.4, 0.4, 0.04]}}
        post = dict(BOX, params={"size": [0.3, 0.3, 1.0]})
        spec = base_spec([post, lid])
        report = audit_connections(spec)
        f = next(f for f in report["findings"] if f["kind"] == "sliver")
        assert "2mm" in f["title"]
        fixed = apply_audit_fixes(spec, report["findings"])
        assert audit_connections(fixed)["findings"] == []

    def test_apply_is_pure(self):
        shelf = {"kind": "box", "name": "shelf", "component": "shelf",
                 "material_slot": "m", "location": [0.4, 0, 1.2],
                 "params": {"size": [0.5, 0.3, 0.04]}}
        spec = base_spec([BOX, shelf])
        snapshot = json.dumps(spec, sort_keys=True)
        report = audit_connections(spec)
        apply_audit_fixes(spec, report["findings"])
        assert json.dumps(spec, sort_keys=True) == snapshot

    def test_report_counts(self):
        report = audit_connections(load("street_light.json"))
        assert report["joints"] == 3  # slip fit + band clamp + luminaire bolt
        assert report["components"] == 4
