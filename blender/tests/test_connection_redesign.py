"""Tests for the connections-generation redesign: one Euler convention
everywhere (Blender XYZ), gusset plates that sit flush instead of floating,
one joint per physical junction, welds for pipes on modeled bases, and
hardware that follows user edits."""
import json
import math
from pathlib import Path

import pytest

import blender.builders  # noqa: F401
from blender.builders.base import Primitive, compute_primitives
from blender.builders.connections import gusset_plate
from blender.builders.hardware import _cylinder_axis, _euler_xyz_matrix

REPO_ROOT = Path(__file__).resolve().parents[2]


def load(name):
    return json.loads((REPO_ROOT / "examples" / name).read_text())


def spec_of(prims, connections=None):
    return {
        "asset_type": "fixture", "name": "F", "units": "metric",
        "parameters": [], "primitives": prims,
        "materials": [{"slot": "m", "preset": "galvanized_steel"}],
        "connections": connections or [],
        "toggles": [{"id": "connection_hardware", "label": "CH", "value": True}],
    }


def mat_mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)]


def rot_x(t):
    c, s = math.cos(t), math.sin(t)
    return [[1, 0, 0], [0, c, -s], [0, s, c]]


def rot_y(t):
    c, s = math.cos(t), math.sin(t)
    return [[c, 0, s], [0, 1, 0], [-s, 0, c]]


def rot_z(t):
    c, s = math.cos(t), math.sin(t)
    return [[c, -s, 0], [s, c, 0], [0, 0, 1]]


class TestEulerConvention:
    """Primitive.rotation is a Blender XYZ Euler: R = Rz·Ry·Rx (X first,
    fixed axes) — what ops.py hands to obj.rotation_euler and the preview
    renders with Three's 'ZYX' order. The shared matrix must match."""

    @pytest.mark.parametrize("rot", [
        (0.3, 0.0, 0.0), (0.0, 1.2, 0.0), (0.0, 0.0, -0.7),
        (0.4, -0.9, 1.3), (0.0, math.pi / 2, math.pi / 4),
    ])
    def test_matrix_is_rz_ry_rx(self, rot):
        expected = mat_mul(rot_z(rot[2]), mat_mul(rot_y(rot[1]), rot_x(rot[0])))
        m = _euler_xyz_matrix(rot)
        for r1, r2 in zip(m, expected):
            for a, b in zip(r1, r2):
                assert math.isclose(a, b, abs_tol=1e-12)

    @pytest.mark.parametrize("rot", [
        (0.5, 0.0, 0.0), (0.0, 0.8, 0.0), (0.3, 0.6, -1.1),
    ])
    def test_cylinder_axis_agrees_with_matrix(self, rot):
        # the oriented-cylinder AABB and the corner AABB must share one
        # convention — this drifted apart before the redesign
        m = _euler_xyz_matrix(rot)
        axis = _cylinder_axis(rot)
        for k in range(3):
            assert math.isclose(axis[k], m[k][2], abs_tol=1e-12)


def corners_of(prim):
    """World positions of a gusset loft's four profile corners."""
    m = _euler_xyz_matrix(prim.rotation)
    d = prim.params["depth"]
    w0 = prim.params["profile_start"]["w"]
    w1 = prim.params["profile_end"]["w"]
    out = {}
    for tag, (x, z) in {
        "top_in": (-w0 / 2, -d / 2), "bot_in": (w0 / 2, -d / 2),
        "top_out": (-w1 / 2, d / 2), "bot_out": (w1 / 2, d / 2),
    }.items():
        v = (x, 0.0, z)
        w = [sum(m[i][k] * v[k] for k in range(3)) + prim.location[i]
             for i in range(3)]
        out[tag] = (math.hypot(w[0], w[1]), w[2])  # (radial, z)
    return out


class TestGussetPlate:
    """Gussets are flush plates, not floating arrowheads: one long edge lies
    flat, the raked tall edge is buried inside the member."""

    def test_base_gusset_sits_flat_on_the_flange(self):
        [g] = gusset_plate("g", "c", "s", (0.0, 0.0), math.pi / 4,
                           attach_r=0.15, reach_r=0.311, flush_z=0.052,
                           hug="bottom", height=0.165)
        c = corners_of(g)
        assert c["bot_in"][1] == pytest.approx(0.052, abs=1e-4)
        assert c["bot_out"][1] == pytest.approx(0.052, abs=1e-4)   # flat bottom
        assert c["top_in"][0] <= 0.15 + 1e-6                       # at/inside the pole
        assert c["top_out"][0] <= 0.311 + 1e-6                     # inside the rim
        assert c["top_in"][1] > c["top_out"][1]                    # hypotenuse down

    def test_knee_brace_hugs_the_arm_underside(self):
        [g] = gusset_plate("g", "c", "s", (0.0, 0.0), 0.0,
                           attach_r=0.09, reach_r=0.25, flush_z=8.0,
                           hug="top", height=0.16)
        c = corners_of(g)
        assert c["top_in"][1] == pytest.approx(8.0, abs=1e-4)
        assert c["top_out"][1] == pytest.approx(8.0, abs=1e-4)     # flat top
        assert c["bot_in"][0] <= 0.09 + 1e-6                       # at/inside the pole
        assert c["bot_in"][1] < c["bot_out"][1]                    # hypotenuse up

    def test_too_thin_annulus_emits_nothing(self):
        assert gusset_plate("g", "c", "s", (0.0, 0.0), 0.0,
                            attach_r=0.15, reach_r=0.16, flush_z=0.05) == []


POLE = {"kind": "cylinder", "name": "shaft", "component": "pole",
        "material_slot": "m", "location": [0, 0, 4.5],
        "params": {"radius": 0.08, "depth": 9}}


class TestJunctionMerge:
    """One physical junction -> one joint: a pole meeting its base plate's
    grout pad, flange, and gussets is ONE junction, while bench slats along
    a rail keep their separate bolts."""

    def test_pole_on_modeled_base_is_one_welded_joint(self):
        plate = {"kind": "cylinder", "name": "plate", "component": "base",
                 "material_slot": "m", "location": [0, 0, 0.015],
                 "params": {"radius": 0.25, "depth": 0.03}}
        pad = {"kind": "cylinder", "name": "pad", "component": "base",
               "material_slot": "m", "location": [0, 0, 0.045],
               "params": {"radius": 0.2, "depth": 0.03}}
        prims = compute_primitives(spec_of([POLE, plate, pad]))
        records = [p.meta["joint"] for p in prims if p.meta and "joint" in p.meta]
        pair = [r for r in records if {r["a"], r["b"]} == {"pole", "base"}]
        assert len(pair) == 1, "the pole/base junction must be a single joint"
        assert pair[0]["type"] == "weld", "a standing pipe is welded to its base"
        # the bead sits at the seam where the pole exits the base, not inside
        weld = next(p for p in prims if p.name.endswith("_weld"))
        assert weld.location[2] == pytest.approx(0.06, abs=1e-6)
        # and no bolt was driven through the pole
        assert not any(p.name.endswith("_shaft") and p.component == "hardware"
                       for p in prims)

    def test_street_light_has_exactly_three_joints(self):
        spec = load("street_light.json")
        spec["toggles"].append({"id": "connection_hardware", "label": "CH",
                                "value": True})
        prims = compute_primitives(spec)
        records = [p.meta["joint"] for p in prims if p.meta and "joint" in p.meta]
        assert sorted(r["type"] for r in records) == \
            ["band_clamp", "slip_fit", "through_bolt"]

    def test_separate_contact_regions_keep_their_joints(self):
        spec = load("park_bench.json")
        spec["toggles"].append({"id": "connection_hardware", "label": "CH",
                                "value": True})
        prims = compute_primitives(spec)
        records = [p.meta["joint"] for p in prims if p.meta and "joint" in p.meta]
        carriage = [r for r in records if r["type"] == "carriage_bolt"]
        assert len(carriage) == 6, "3 slats x 2 rails stay separate joints"


class TestHardwareFollowsEdits:
    def test_band_clamp_follows_a_moved_arm(self):
        spec = load("street_light.json")
        spec["toggles"].append({"id": "connection_hardware", "label": "CH",
                                "value": True})
        base_band = next(p for p in compute_primitives(spec)
                         if p.name.endswith("_band"))
        spec["offsets"] = {"arm": [0.0, 0.0, -0.5]}
        moved_band = next(p for p in compute_primitives(spec)
                          if p.name.endswith("_band"))
        assert moved_band.location[2] == pytest.approx(
            base_band.location[2] - 0.5, abs=1e-6)

    def test_hardware_group_offset_still_applies(self):
        spec = load("street_light.json")
        spec["toggles"].append({"id": "connection_hardware", "label": "CH",
                                "value": True})
        base = {p.name: p for p in compute_primitives(spec)
                if p.component == "hardware"}
        spec["offsets"] = {"hardware": [0.2, 0.0, 0.0]}
        moved = {p.name: p for p in compute_primitives(spec)
                 if p.component == "hardware"}
        for name, p in moved.items():
            assert p.location[0] == pytest.approx(base[name].location[0] + 0.2,
                                                  abs=1e-9)
