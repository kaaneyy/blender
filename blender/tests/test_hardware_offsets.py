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


class TestHardware:
    def test_off_by_default(self):
        prims = compute_primitives(load("street_light.json"))
        assert not any(p.component == "hardware" for p in prims)

    @pytest.mark.parametrize("example", ["street_light.json", "park_bench.json"])
    def test_generates_bolts_at_joints(self, example):
        base = compute_primitives(load(example))
        prims = compute_primitives(with_hardware(load(example)))
        bolts = [p for p in prims if p.component == "hardware"]
        assert bolts, "expected hardware at component joints"
        assert len(bolts) % 3 == 0  # head + shaft + nut per connection
        assert len(prims) == len(base) + len(bolts)
        heads = [p for p in bolts if p.name.endswith("_head")]
        assert all(p.params.get("segments") == 6 for p in heads), "hex heads"
        assert all(p.material_slot == "hardware" for p in bolts)

    def test_bolt_count_bounded(self):
        prims = compute_primitives(with_hardware(load("street_light.json")))
        connections = len([p for p in prims if p.name.endswith("_shaft")])
        assert 1 <= connections <= 24


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
