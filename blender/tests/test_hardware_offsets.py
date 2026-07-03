"""Tests for connection-hardware generation and per-part position offsets."""
import json
from pathlib import Path

import pytest

import blender.builders  # noqa: F401
from blender.builders.base import compute_primitives

REPO_ROOT = Path(__file__).resolve().parents[2]


def load(name):
    return json.loads((REPO_ROOT / "examples" / name).read_text())


def with_hardware(spec):
    spec = json.loads(json.dumps(spec))
    spec.setdefault("toggles", []).append(
        {"id": "connection_hardware", "label": "Connection Hardware", "value": True}
    )
    return spec


def joint_numbers(prims):
    import re

    out = set()
    for p in prims:
        if p.component == "hardware":
            m = re.match(r"^joint(\d+)_", p.name)
            if m:
                out.add(int(m.group(1)))
    return out


class TestHardware:
    def test_off_by_default(self):
        prims = compute_primitives(load("street_light.json"))
        assert not any(p.component == "hardware" for p in prims)

    @pytest.mark.parametrize("example", ["street_light.json", "park_bench.json"])
    def test_generates_engineered_joints(self, example):
        base = compute_primitives(load(example))
        prims = compute_primitives(with_hardware(load(example)))
        hardware = [p for p in prims if p.component == "hardware"]
        assert hardware, "expected hardware at component joints"
        assert len(prims) == len(base) + len(hardware)
        heads = [p for p in hardware if p.name.endswith("_head")]
        nuts = [p for p in hardware if p.name.endswith("_nut")]
        washers = [p for p in hardware if "_washer_" in p.name]
        shafts = [p for p in hardware if p.name.endswith("_shaft")]
        assert heads and nuts and shafts
        assert len(washers) == len(heads) + len(nuts), "washer under every head and nut"
        assert all(p.params.get("segments") == 6 for p in heads + nuts), "hex heads/nuts"
        assert all(p.material_slot == "hardware" for p in hardware)

    def test_joint_count_bounded(self):
        prims = compute_primitives(with_hardware(load("street_light.json")))
        joints = joint_numbers(prims)
        assert 1 <= len(joints) <= 24

    def test_street_light_arm_gets_band_clamp(self):
        """A horizontal round mast arm meeting the upright pole is clamped
        with a saddle band (+2 side bolts), like real pole fittings."""
        prims = compute_primitives(with_hardware(load("street_light.json")))
        bands = [p for p in prims if p.name.endswith("_band")]
        assert bands, "expected a band clamp at the pole/arm joint"
        band = bands[0]
        # band wraps the pole: centered on the pole axis, near the arm height
        assert band.location[0] == pytest.approx(0.0)
        assert band.location[1] == pytest.approx(0.0)
        pole_height = 30 * 0.3048
        assert pole_height - 1.5 < band.location[2] < pole_height
        # band radius follows the pole taper (top radius 2in=0.0508 + gap)
        assert 0.05 < band.params["radius"] < 0.08

    def test_bench_slats_bolt_vertically_through_rails(self):
        """Seat slats sit on frame rails with real overlap; the generator
        must produce vertical through-bolts whose head is above the slat
        and nut below, spanning the actual joint."""
        prims = compute_primitives(with_hardware(load("park_bench.json")))
        vertical_shafts = [
            p for p in prims
            if p.name.endswith("_shaft") and p.rotation == (0.0, 0.0, 0.0)
        ]
        assert vertical_shafts, "expected vertical bolts at the slat/rail joints"
        seat_height = 18 * 0.0254
        s = vertical_shafts[0]
        joint = s.name.split("_")[0]
        head = next(p for p in prims if p.name == f"{joint}_bolt1_head")
        nut = next(p for p in prims if p.name == f"{joint}_bolt1_nut")
        assert head.location[2] > nut.location[2], "head above, nut below"
        # the shaft actually spans the joint plane at the seat surface
        top = s.location[2] + s.params["depth"] / 2
        bottom = s.location[2] - s.params["depth"] / 2
        assert bottom < seat_height < top

    def test_no_bolts_between_non_touching_parts(self):
        """Oriented bounding boxes: a rotated horizontal cylinder far from a
        box must not generate hardware (the old conservative AABB would)."""
        from blender.builders.hardware import compute_hardware
        from blender.builders.base import Primitive

        long_arm = Primitive(
            kind="cylinder", name="arm", component="a",
            location=(0.0, 0.0, 1.0), rotation=(0.0, 1.5707963, 0.0),
            params={"radius": 0.02, "depth": 2.0},
        )  # lies along X at z=1
        box = Primitive(
            kind="box", name="pad", component="b",
            location=(0.0, 0.0, 0.2), params={"size": (0.3, 0.3, 0.3)},
        )  # well below the arm
        assert compute_hardware([long_arm, box]) == []

    def test_oriented_extents_for_rotated_cylinder(self):
        from blender.builders.hardware import _half_extents
        from blender.builders.base import Primitive

        bracket = Primitive(
            kind="cylinder", name="b", component="c",
            location=(0, 0, 0), rotation=(1.5707963, 0.0, 0.0),  # along Y
            params={"radius": 0.016, "depth": 0.9},
        )
        hx, hy, hz = _half_extents(bracket)
        assert hx == pytest.approx(0.016, abs=1e-3)
        assert hy == pytest.approx(0.45, abs=1e-3)
        assert hz == pytest.approx(0.016, abs=1e-3)


class TestOffsets:
    def test_component_offset_moves_every_part(self):
        spec = load("street_light.json")
        spec["offsets"] = {"luminaire": [0.5, 0.0, -0.25]}
        base = {p.name: p for p in compute_primitives(load("street_light.json"))}
        moved = {p.name: p for p in compute_primitives(spec)}
        for name, p in moved.items():
            b = base[name]
            if p.component == "luminaire":
                assert p.location[0] == pytest.approx(b.location[0] + 0.5)
                assert p.location[2] == pytest.approx(b.location[2] - 0.25)
            else:
                assert p.location == b.location

    def test_part_offset_stacks_with_component_offset(self):
        spec = load("street_light.json")
        spec["offsets"] = {"pole": [0.1, 0, 0], "pole/shaft": [0.2, 0, 0]}
        prims = {p.name: p for p in compute_primitives(spec)}
        base = {p.name: p for p in compute_primitives(load("street_light.json"))}
        assert prims["shaft"].location[0] == pytest.approx(base["shaft"].location[0] + 0.3)
        assert prims["cap"].location[0] == pytest.approx(base["cap"].location[0] + 0.1)

    def test_offsets_apply_to_custom_assets(self):
        spec = load("park_bench.json")
        spec["offsets"] = {"seat/slat_mid": [0, 0, 0.05]}
        prims = {p.name: p for p in compute_primitives(spec)}
        base = {p.name: p for p in compute_primitives(load("park_bench.json"))}
        assert prims["slat_mid"].location[2] == pytest.approx(base["slat_mid"].location[2] + 0.05)
        assert prims["slat_front"].location == base["slat_front"].location
