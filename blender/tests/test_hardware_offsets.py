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
        shafts = [p for p in hardware if p.name.endswith("_shaft")]
        assert heads and nuts and shafts
        # every through-bolt head/nut has a washer under it (set-screw heads
        # and carriage-bolt domes legitimately carry no head-side washer)
        names = {p.name for p in hardware}
        for h in heads:
            if "_bolt" in h.name:
                assert h.name.replace("_head", "_washer_h") in names
        for nu in nuts:
            if "_bolt" in nu.name:
                assert nu.name.replace("_nut", "_washer_n") in names
        assert all(p.params.get("segments") == 6 for p in heads + nuts), "hex heads/nuts"

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

    def test_bench_slats_get_carriage_bolts(self):
        """Wood seat slats on metal frame rails: the fastener a fabricator
        uses is a carriage bolt — smooth dome proud of the timber (no washer
        under it), washer + hex nut on the steel side below."""
        prims = compute_primitives(with_hardware(load("park_bench.json")))
        domes = [p for p in prims if p.name.endswith("_dome")]
        assert domes, "expected carriage bolts at the wood-on-metal slat joints"
        seat_height = 18 * 0.0254
        dome = next(d for d in domes if abs(d.location[2] - seat_height) < 0.1)
        joint = dome.name.split("_")[0]
        shaft = next(p for p in prims if p.name == dome.name.replace("_dome", "_shaft"))
        nut = next(p for p in prims if p.name == dome.name.replace("_dome", "_nut"))
        assert dome.location[2] > nut.location[2], "dome on the wood above, nut below"
        assert f"{joint}_" in dome.name
        # the shaft actually spans the joint plane at the seat surface
        top = shaft.location[2] + shaft.params["depth"] / 2
        bottom = shaft.location[2] - shaft.params["depth"] / 2
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


class TestLoadClassFasteners:
    """C6: bolt diameter scales with the joined members' load tier."""

    def _joint(self, size):
        from blender.builders.base import Primitive

        a = Primitive(kind="box", name="a", component="ca",
                      location=(0, 0, size / 2), params={"size": (size, size, size)})
        b = Primitive(kind="box", name="b", component="cb",
                      location=(0, 0, size + 0.02), params={"size": (0.05, 0.05, 0.06)})
        return [a, b]

    def test_heavier_members_get_bigger_bolts(self):
        from blender.builders.hardware import compute_hardware

        light = compute_hardware(self._joint(0.15))    # ~0.003 m3 -> light
        heavy = compute_hardware(self._joint(0.9))     # ~0.73 m3 -> heavy
        r_light = next(p for p in light if p.name.endswith("_shaft")).params["radius"]
        r_heavy = next(p for p in heavy if p.name.endswith("_shaft")).params["radius"]
        assert r_heavy > r_light
        assert r_heavy <= 0.014


class TestMaterialAwareHardware:
    """Fasteners must suit the asset: no industrial bolts on wood joinery."""

    def _two_part(self, mat_a, mat_b):
        # two overlapping boxes in different components, each a declared material
        return {
            "asset_type": "table", "name": "T", "units": "metric",
            "parameters": [], "primitives": [
                {"kind": "box", "name": "leg", "component": "legs",
                 "material_slot": "wood_a", "location": [0, 0, 0.35],
                 "params": {"size": [0.06, 0.06, 0.7]}},
                {"kind": "box", "name": "apron", "component": "apron",
                 "material_slot": "wood_b", "location": [0, 0, 0.66],
                 "params": {"size": [0.5, 0.06, 0.08]}},
            ],
            "materials": [
                {"slot": "wood_a", "preset": mat_a},
                {"slot": "wood_b", "preset": mat_b},
            ],
            "toggles": [
                {"id": "connection_hardware", "label": "Connection Hardware", "value": True},
            ],
        }

    def test_all_wood_joint_gets_no_bolts(self):
        prims = compute_primitives(self._two_part("wood_slat", "wood_slat"))
        assert not any(p.component == "hardware" for p in prims), \
            "a wood-to-wood joint should use concealed joinery, not metal bolts"

    def test_wood_to_metal_joint_keeps_bolts(self):
        prims = compute_primitives(self._two_part("wood_slat", "cast_iron"))
        assert any(p.component == "hardware" for p in prims), \
            "a wood slat bolted to a metal frame is a real carriage-bolt joint"

    def test_metal_to_metal_joint_keeps_bolts(self):
        prims = compute_primitives(self._two_part("galvanized_steel", "cast_iron"))
        assert any(p.component == "hardware" for p in prims)

    def test_street_light_flange_hardware_unaffected(self):
        prims = compute_primitives(with_hardware(load("street_light.json")))
        assert any(p.component == "hardware" for p in prims)

    def test_direct_call_without_spec_treats_all_as_metal(self):
        # existing direct compute_hardware(prims) callers keep their behavior
        from blender.builders.hardware import compute_hardware
        from blender.builders.base import Primitive
        a = Primitive(kind="box", name="a", component="ca",
                      location=(0, 0, 0.5), params={"size": (0.3, 0.3, 1.0)})
        b = Primitive(kind="box", name="b", component="cb",
                      location=(0, 0, 1.0), params={"size": (0.3, 0.3, 0.1)})
        assert compute_hardware([a, b])  # metal by default → hardware present
