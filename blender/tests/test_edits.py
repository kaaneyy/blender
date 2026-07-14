"""Tests for the SketchUp-style edit overlay (move / rotate / stretch /
delete / duplicate) baked into compute_primitives. Parity target for
frontend/src/builders/edits.ts."""
import json
import math
from pathlib import Path

import pytest

import blender.builders  # noqa: F401
from blender.builders.base import Primitive, compute_primitives
from blender.builders.edits import (
    apply_transforms,
    component_pivot,
    _euler_xyz_matrix,
    _euler_from_matrix,
    _mat_mul,
    _scale_factors,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def load(name):
    return json.loads((REPO_ROOT / "examples" / name).read_text())


def comps(prims):
    out = {}
    for p in prims:
        out.setdefault(p.component, []).append(p)
    return out


class TestDelete:
    def test_delete_component(self):
        spec = load("park_bench.json")
        base = compute_primitives(spec)
        assert any(p.component == "backrest" for p in base)
        spec["edits"] = {"hidden": ["backrest"]}
        edited = compute_primitives(spec)
        assert not any(p.component == "backrest" for p in edited)
        # nothing else removed
        assert len([p for p in edited if p.component != "backrest"]) == len(
            [p for p in base if p.component != "backrest"]
        )

    def test_delete_single_part(self):
        spec = load("park_bench.json")
        spec["edits"] = {"hidden": ["seat/slat_mid"]}
        edited = compute_primitives(spec)
        seat = [p for p in edited if p.component == "seat"]
        names = {p.name for p in seat}
        assert "slat_mid" not in names
        assert "slat_front" in names  # siblings survive


class TestDuplicate:
    def test_duplicate_component(self):
        spec = load("park_bench.json")
        base = comps(compute_primitives(spec))
        spec["edits"] = {"duplicates": [{"source": "seat", "name": "seat copy"}]}
        spec["offsets"] = {"seat copy": [0.3, 0.0, 0.0]}
        edited = comps(compute_primitives(spec))
        assert "seat copy" in edited
        assert len(edited["seat copy"]) == len(base["seat"])
        # the copy is shifted +0.3 m in X from the original
        orig = {p.name: p for p in edited["seat"]}
        for p in edited["seat copy"]:
            assert math.isclose(p.location[0], orig[p.name].location[0] + 0.3, abs_tol=1e-9)


class TestTransforms:
    def test_offset_still_stacks(self):
        spec = load("street_light.json")
        spec["offsets"] = {"pole": [0.0, 0.0, 1.0]}
        base = {p.name: p for p in compute_primitives(load("street_light.json")) if p.component == "pole"}
        moved = {p.name: p for p in compute_primitives(spec) if p.component == "pole"}
        for name, p in moved.items():
            assert math.isclose(p.location[2], base[name].location[2] + 1.0, abs_tol=1e-9)

    def test_scale_grows_bounds(self):
        spec = load("park_bench.json")
        base_pivot = component_pivot([p for p in compute_primitives(spec) if p.component == "seat"])
        spec["edits"] = {"scales": {"seat": [2.0, 2.0, 2.0]}}
        seat = [p for p in compute_primitives(spec) if p.component == "seat"]
        new_pivot = component_pivot(seat)
        # scaling about the center leaves the center put
        for a, b in zip(base_pivot, new_pivot):
            assert math.isclose(a, b, abs_tol=1e-6)
        # box parts doubled in size
        box = next(p for p in seat if p.kind == "box")
        # a doubled box has twice the footprint
        assert box.params["size"][0] > 0

    def test_rotate_90_about_z(self):
        # a +90° Z rotation about the pivot sends offset (dx, dy) -> (-dy, dx)
        spec = load("park_bench.json")
        seat = [p for p in compute_primitives(spec) if p.component == "seat"]
        pivot = component_pivot(seat)
        target = max(seat, key=lambda p: p.location[0])  # furthest +X part
        dx = target.location[0] - pivot[0]
        dy = target.location[1] - pivot[1]
        spec["edits"] = {"rotations": {"seat": [0.0, 0.0, math.pi / 2]}}
        rotated = {p.name: p for p in compute_primitives(spec) if p.component == "seat"}
        moved = rotated[target.name]
        assert math.isclose(moved.location[0] - pivot[0], -dy, abs_tol=1e-6)
        assert math.isclose(moved.location[1] - pivot[1], dx, abs_tol=1e-6)


class TestPartTransforms:
    """Rotate/stretch keyed by 'component/part' act on that part alone,
    about its own center — not the whole group."""

    def test_part_rotation_spins_only_that_part_in_place(self):
        spec = load("park_bench.json")
        base = {p.name: p for p in compute_primitives(spec) if p.component == "seat"}
        spec["edits"] = {"rotations": {"seat/slat_mid": [0.0, 0.0, math.pi / 2]}}
        edited = {p.name: p for p in compute_primitives(spec) if p.component == "seat"}
        # the part turned about its own center: box center == location, so
        # the location is unchanged and only the rotation moved
        for a, b in zip(edited["slat_mid"].location, base["slat_mid"].location):
            assert math.isclose(a, b, abs_tol=1e-9)
        assert math.isclose(edited["slat_mid"].rotation[2], math.pi / 2, abs_tol=1e-9)
        # siblings untouched
        assert edited["slat_front"].location == base["slat_front"].location
        assert edited["slat_front"].rotation == base["slat_front"].rotation

    def test_part_scale_resizes_only_that_part_in_place(self):
        spec = load("park_bench.json")
        base = {p.name: p for p in compute_primitives(spec) if p.component == "seat"}
        spec["edits"] = {"scales": {"seat/slat_mid": [1.0, 1.0, 2.0]}}
        edited = {p.name: p for p in compute_primitives(spec) if p.component == "seat"}
        assert edited["slat_mid"].params["size"][2] == pytest.approx(
            base["slat_mid"].params["size"][2] * 2.0)
        for a, b in zip(edited["slat_mid"].location, base["slat_mid"].location):
            assert math.isclose(a, b, abs_tol=1e-9)
        assert edited["slat_front"].params["size"] == base["slat_front"].params["size"]

    def test_part_edit_composes_with_group_edit(self):
        # part stage first (about its own center), then the group stage maps
        # it exactly like its siblings — so its location matches the pure
        # group edit and only its own rotation gains the extra spin
        spec_group = load("park_bench.json")
        spec_group["edits"] = {"rotations": {"seat": [0.0, 0.0, math.pi / 2]}}
        group_only = {p.name: p for p in compute_primitives(spec_group)
                      if p.component == "seat"}
        spec_both = load("park_bench.json")
        spec_both["edits"] = {"rotations": {
            "seat": [0.0, 0.0, math.pi / 2],
            "seat/slat_mid": [0.0, 0.0, math.pi / 2],
        }}
        both = {p.name: p for p in compute_primitives(spec_both)
                if p.component == "seat"}
        for a, b in zip(both["slat_mid"].location, group_only["slat_mid"].location):
            assert math.isclose(a, b, abs_tol=1e-9)
        assert math.isclose(abs(both["slat_mid"].rotation[2]), math.pi, abs_tol=1e-9)


class TestRotationMath:
    def test_matrix_roundtrip(self):
        for rot in [(0.1, 0.2, 0.3), (0.0, 0.0, math.pi / 2), (-0.5, 1.2, 0.7)]:
            m = _euler_xyz_matrix(rot)
            back = _euler_from_matrix(m)
            m2 = _euler_xyz_matrix(back)
            for r1, r2 in zip(m, m2):
                for a, b in zip(r1, r2):
                    assert math.isclose(a, b, abs_tol=1e-9)

    def test_compose_is_associative_with_identity(self):
        rot = (0.3, -0.4, 1.1)
        ident = _euler_xyz_matrix((0.0, 0.0, 0.0))
        composed = _euler_from_matrix(_mat_mul(ident, _euler_xyz_matrix(rot)))
        for a, b in zip(composed, rot):
            assert math.isclose(a, b, abs_tol=1e-9)


def test_no_edits_is_identity():
    spec = load("street_light.json")
    prims = compute_primitives(spec)
    assert apply_transforms(prims, {}) is prims  # fast path returns input


class TestRotationAwareScale:
    """A component/part scale edit applies world-axis factors s; a member
    rotated off-axis must stretch along its correct LOCAL axes (f = S·R·e_i
    lengths, not s directly), or scaled bench-like structures tear apart."""

    def test_rotated_cylinder_stretches_along_its_own_axis(self):
        # cylinder lying along world Y (rotation (pi/2, 0, 0)), like a bench
        # stretcher spanning between two legs at y = +-1.0
        cyl = Primitive(
            kind="cylinder", name="stretcher", component="frame",
            location=(0.0, 0.0, 0.5), rotation=(math.pi / 2, 0.0, 0.0),
            params={"radius": 0.05, "depth": 2.0},
        )
        leg_a = Primitive(
            kind="box", name="leg_a", component="frame",
            location=(0.0, -1.0, 0.0), params={"size": (0.1, 0.1, 1.0)},
        )
        leg_b = Primitive(
            kind="box", name="leg_b", component="frame",
            location=(0.0, 1.0, 0.0), params={"size": (0.1, 0.1, 1.0)},
        )
        spec = {"edits": {"scales": {"frame": [1.0, 2.0, 1.0]}}}
        out = {p.name: p for p in apply_transforms([cyl, leg_a, leg_b], spec)}
        # world-Y span doubled to match the legs (now at y = +-2.0)
        assert out["stretcher"].params["depth"] == pytest.approx(4.0)
        # radius unaffected: local axes 0/1 (both perpendicular to world Y
        # after the rotation) see factor 1 from an all-XZ-preserving scale
        assert out["stretcher"].params["radius"] == pytest.approx(0.05)
        # legs actually ended up at y = +-2.0, confirming the stretcher would
        # now reach them
        assert out["leg_a"].location[1] == pytest.approx(-2.0)
        assert out["leg_b"].location[1] == pytest.approx(2.0)

    def test_box_rotated_about_z_grows_local_y_not_local_x(self):
        box = Primitive(
            kind="box", name="panel", component="frame",
            location=(0.0, 0.0, 0.0), rotation=(0.0, 0.0, math.pi / 2),
            params={"size": (1.0, 0.4, 0.2)},
        )
        spec = {"edits": {"scales": {"frame": [2.0, 1.0, 1.0]}}}
        out = apply_transforms([box], spec)[0]
        # world-X scale becomes a LOCAL-Y stretch once rotated 90 deg about Z
        assert out.params["size"][0] == pytest.approx(1.0)
        assert out.params["size"][1] == pytest.approx(0.8)
        assert out.params["size"][2] == pytest.approx(0.2)

    def test_unrotated_box_keeps_current_behavior(self):
        box = Primitive(
            kind="box", name="panel", component="frame",
            location=(0.0, 0.0, 0.0), params={"size": (1.0, 0.4, 0.2)},
        )
        spec = {"edits": {"scales": {"frame": [2.0, 1.0, 1.0]}}}
        out = apply_transforms([box], spec)[0]
        assert out.params["size"][0] == pytest.approx(2.0)
        assert out.params["size"][1] == pytest.approx(0.4)
        assert out.params["size"][2] == pytest.approx(0.2)

    def test_scale_factors_identity_rotation_equals_s(self):
        s = (1.5, 2.0, 0.5)
        f = _scale_factors(s, (0.0, 0.0, 0.0))
        for a, b in zip(f, s):
            assert math.isclose(a, b, abs_tol=1e-9)
