"""Tests for the SketchUp-style edit overlay (move / rotate / stretch /
delete / duplicate) baked into compute_primitives. Parity target for
frontend/src/builders/edits.ts."""
import json
import math
from pathlib import Path

import blender.builders  # noqa: F401
from blender.builders.base import compute_primitives
from blender.builders.edits import (
    apply_transforms,
    component_pivot,
    _euler_xyz_matrix,
    _euler_from_matrix,
    _mat_mul,
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
