"""Tests for Phase 3: the bolt catalog (and its sync with us_codes.json),
the moment-proxy load class, joint-record metadata, and the joint schedule
that grounds the install guide."""
import json
from pathlib import Path

import blender.builders  # noqa: F401
from blender.builders.base import Primitive, compute_primitives
from blender.builders.hardware import (
    BOLT_CATALOG,
    _load_class_from_moment,
    _moment,
    _snap_bolt,
    compute_hardware,
)
from blender.builders.schedule import joint_schedule

REPO_ROOT = Path(__file__).resolve().parents[2]


def load(name):
    return json.loads((REPO_ROOT / "examples" / name).read_text())


class TestCatalog:
    def test_snap_to_nearest_real_size(self):
        assert _snap_bolt(0.0093) == ("M20", 0.010)
        assert _snap_bolt(0.0082) == ("M16", 0.008)
        assert _snap_bolt(0.001) == ("M6", 0.003)
        assert _snap_bolt(0.05) == ("M24", 0.012)

    def test_catalog_matches_standards_db(self):
        """The code constant and the human-facing table must not drift."""
        db = json.loads((REPO_ROOT / "standards" / "us_codes.json").read_text())
        table = {row["name"]: row["shaft_mm"] for row in db["_connections"]["bolt_catalog"]}
        assert {name: round(r * 2000) for name, r in BOLT_CATALOG} == table

    def test_anchor_tiers_match_emitter_constants(self):
        from blender.builders.connections import BOLT_COUNT, BOLT_R

        db = json.loads((REPO_ROOT / "standards" / "us_codes.json").read_text())
        tiers = db["_connections"]["anchor_tiers"]
        for tier, entry in tiers.items():
            assert BOLT_COUNT[tier] == entry["count"]
            assert round(BOLT_R[tier] * 2000) == entry["dia_mm"]

    def test_generated_bolts_are_catalog_sizes(self):
        spec = load("park_bench.json")
        spec["toggles"].append({"id": "connection_hardware", "label": "CH", "value": True})
        radii = {round(r, 6) for _, r in BOLT_CATALOG}
        for p in compute_primitives(spec):
            if p.component == "hardware" and p.name.endswith("_shaft"):
                assert round(p.params["radius"], 6) in radii, p.name


class TestMomentProxy:
    def _fixture(self, arm_center_x):
        post = Primitive(kind="box", name="post", component="post",
                         location=(0, 0, 1.0), params={"size": (0.1, 0.1, 2.0)})
        arm = Primitive(kind="box", name="arm", component="arm",
                        location=(arm_center_x, 0, 1.95),
                        params={"size": (1.0, 0.08, 0.08)})
        return post, arm

    def test_cantilever_outranks_compact(self):
        # same arm volume; centered vs cantilevered off the post
        post_a, arm_center = self._fixture(0.0)
        post_b, arm_cant = self._fixture(0.5)
        joint = (0.0, 0.0, 1.95)
        m_center = _moment(post_a, arm_center, joint, None)
        m_cant = _moment(post_b, arm_cant, joint, None)
        assert m_cant > m_center

    def test_load_class_thresholds(self):
        assert _load_class_from_moment(0.001) == "light"
        assert _load_class_from_moment(0.05) == "standard"
        assert _load_class_from_moment(0.5) == "heavy"


class TestJointRecords:
    def test_every_joint_carries_one_record(self):
        spec = load("street_light.json")
        spec["toggles"].append({"id": "connection_hardware", "label": "CH", "value": True})
        prims = compute_primitives(spec)
        hardware = [p for p in prims if p.component == "hardware"]
        joints = {p.name.split("_")[0] for p in hardware}
        records = [p.meta["joint"] for p in hardware if p.meta]
        assert len(records) == len(joints)
        for r in records:
            assert r["type"] and r["fastener"] and r["count"] >= 1
            assert r["a"] and r["b"]

    def test_records_survive_offsets_and_edits(self):
        spec = load("street_light.json")
        spec["toggles"].append({"id": "connection_hardware", "label": "CH", "value": True})
        spec["offsets"] = {"hardware": [0.1, 0.0, 0.0]}
        prims = compute_primitives(spec)
        assert any(p.meta and "joint" in p.meta for p in prims)


class TestJointSchedule:
    def test_bench_schedule_names_real_fasteners(self):
        schedule = joint_schedule(load("park_bench.json"))
        assert schedule, "the bench has joints"
        types = {r["type"] for r in schedule}
        assert "carriage_bolt" in types  # declared: slats on frame
        for r in schedule:
            assert r["id"] >= 1
            assert r["fastener"]
        # ids are sequential from 1
        assert [r["id"] for r in schedule] == list(range(1, len(schedule) + 1))

    def test_street_light_schedule_covers_anchor_and_clamp(self):
        schedule = joint_schedule(load("street_light.json"))
        types = {r["type"] for r in schedule}
        assert "band_clamp" in types
        assert "slip_fit" in types
        # anchor comes from the curated builder's modeled base (no generated
        # anchor_base) — but the declared band clamp must cite an ear bolt
        band = next(r for r in schedule if r["type"] == "band_clamp")
        assert "ear bolt" in band["fastener"]
        assert band["torque_nm"] > 0

    def test_schedule_forces_hardware_toggle(self):
        spec = load("park_bench.json")  # no connection_hardware toggle at all
        assert joint_schedule(spec), "schedule computes hardware even when the preview toggle is off"

    def test_unbuildable_spec_gives_empty_schedule(self):
        assert joint_schedule({"asset_type": "nope", "name": "x", "units": "metric",
                               "parameters": []}) == []


class TestInstallGuidePrompt:
    def test_user_message_embeds_schedule(self):
        import sys
        sys.path.insert(0, str(REPO_ROOT))
        from backend.app.spec_ai import _install_guide_prompts

        system, user, schedule = _install_guide_prompts(load("park_bench.json"))
        assert "Joint schedule" in user
        assert "## Joint schedule" in system
        assert schedule and json.dumps(schedule[0]["fastener"])[1:-1] in user
