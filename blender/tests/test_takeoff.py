"""Fabrication detail: hollow sections, drilled plates, and what it weighs.

The reference is an ordinary shop drawing of a square-tube bike rack — 2.0
SQUARE x 0.188 wall stock, base plates with a 4x drilled hole pattern, and
"APPROXIMATE WEIGHT: 34.6 LB". These tests pin the three capabilities that
make a generated asset carry that much detail.
"""
import json
import math
from pathlib import Path

import pytest

from blender.builders.base import Primitive, compute_primitives
from blender.builders.takeoff import (
    KG_PER_LB,
    compute_takeoff,
    material_family,
    solid_volume,
    stock_callout,
)

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

IN = 0.0254  # metres per inch


def load(name):
    return json.loads((EXAMPLES / name).read_text())


def tube(radius, depth, wall, section=None, slot="pole"):
    params = {"radius": radius, "depth": depth, "wall": wall}
    if section:
        params["section"] = section
    return Primitive(kind="tube", name="member", component="frame",
                     location=(0.0, 0.0, depth / 2), material_slot=slot,
                     params=params)


class TestHollowVolume:
    def test_round_tube_is_an_annulus_not_a_billet(self):
        r, d, w = 0.05, 2.0, 0.005
        expected = math.pi * d * (r ** 2 - (r - w) ** 2)
        assert solid_volume(tube(r, d, w)) == pytest.approx(expected)

    def test_square_tube_is_the_square_annulus(self):
        """The drawing's stock: 2.0 in square, 0.188 in wall."""
        r, d, w = 1.0 * IN, 0.889, 0.188 * IN  # half-width across flats
        expected = d * (4 * r * r - 4 * (r - w) ** 2)
        assert solid_volume(tube(r, d, w, "square")) == pytest.approx(expected)

    def test_square_tube_outweighs_the_round_tube_it_encloses(self):
        """Same across-flats and wall: the square section has more metal."""
        r, d, w = 0.0254, 1.0, 0.0048
        assert solid_volume(tube(r, d, w, "square")) > solid_volume(tube(r, d, w))

    def test_a_wall_of_zero_is_solid(self):
        r, d = 0.05, 2.0
        assert solid_volume(tube(r, d, 0.0)) == pytest.approx(math.pi * r * r * d)

    def test_hollow_is_a_fraction_of_solid(self):
        """A 3/16 wall on a 4 in tube is a small share of the solid billet —
        the whole reason a takeoff needs the wall."""
        hollow = solid_volume(tube(0.0508, 3.0, 0.0048))
        solid = solid_volume(tube(0.0508, 3.0, 0.0))
        assert 0.05 < hollow / solid < 0.25

    def test_shell_hollows_a_cone_by_its_own_geometry(self):
        """A shelled cone's core is a shrunken cone, not a scaled sphere —
        the ratio differs per kind, so the core uses the cone formula."""
        solid = Primitive(kind="cone", name="shaft", component="pole",
                          location=(0, 0, 4.5), material_slot="pole",
                          params={"radius_bottom": 0.1, "radius_top": 0.05,
                                  "depth": 9.0})
        shelled = Primitive(kind="cone", name="shaft", component="pole",
                            location=(0, 0, 4.5), material_slot="pole",
                            params={"radius_bottom": 0.1, "radius_top": 0.05,
                                    "depth": 9.0, "shell": 0.0048})
        assert solid_volume(shelled) < solid_volume(solid) * 0.2
        assert solid_volume(shelled) > 0


class TestStockCallout:
    def test_square_tube_reads_as_shop_stock(self):
        callout = stock_callout(tube(1.0 * IN, 0.889, 0.188 * IN, "square"))
        assert "SQ" in callout and "2" in callout and "0.188" in callout

    def test_round_tube_reads_as_pipe(self):
        assert "OD" in stock_callout(tube(0.05, 1.0, 0.005))
        assert "pipe" in stock_callout(tube(0.05, 1.0, 0.005))

    def test_non_stock_kinds_have_no_callout(self):
        box = Primitive(kind="box", name="plate", component="base",
                        location=(0, 0, 0), material_slot="base",
                        params={"size": (0.15, 0.11, 0.01)})
        assert stock_callout(box) is None


class TestMaterialFamily:
    @pytest.mark.parametrize("preset,family", [
        ("galvanized_steel", "metal"),
        ("cast_iron", "metal"),
        ("wood_slat", "wood"),
        ("concrete", "concrete"),
        ("lamp_lens", "glass"),
        (None, "metal"),            # unknown reads as the heavier assumption
        ("weathered_oak", "wood"),  # token fallback
    ])
    def test_family(self, preset, family):
        assert material_family(preset) == family


class TestTakeoff:
    def test_street_light_weighs_what_a_street_light_weighs(self):
        """A 30 ft cobra-head assembly is a few hundred pounds, not tons —
        the check that caught the pole being modelled as a solid billet."""
        spec = load("street_light.json")
        out = compute_takeoff(compute_primitives(spec), spec)
        assert 300 < out["total_lb"] < 1200
        assert out["total_kg"] == pytest.approx(out["total_lb"] * KG_PER_LB)

    def test_tapered_pole_is_in_the_real_range(self):
        spec = load("street_light.json")
        out = compute_takeoff(compute_primitives(spec), spec)
        pole = next(c for c in out["by_component"] if c["component"] == "pole")
        assert 200 < pole["lb"] < 600

    def test_components_sum_to_the_total(self):
        spec = load("street_light.json")
        out = compute_takeoff(compute_primitives(spec), spec)
        assert sum(c["kg"] for c in out["by_component"]) == pytest.approx(out["total_kg"])

    def test_drilled_holes_are_negative_space_not_mass(self):
        """`cut` primitives are subtracted in Blender — they must never be
        counted as material."""
        plate = Primitive(kind="box", name="plate", component="base",
                          location=(0, 0, 0.01), material_slot="base",
                          params={"size": (0.15, 0.11, 0.01)})
        hole = Primitive(kind="cylinder", name="hole", component="base",
                         location=(0, 0, 0.01), material_slot="base", cut=True,
                         params={"radius": 0.007, "depth": 0.05})
        with_hole = compute_takeoff([plate, hole])
        without = compute_takeoff([plate])
        assert with_hole["total_kg"] == pytest.approx(without["total_kg"])
        assert all(p["name"] != "hole" for p in with_hole["parts"])

    def test_wood_is_lighter_than_steel_for_the_same_shape(self):
        spec = load("pergola.json")
        out = compute_takeoff(compute_primitives(spec), spec)
        assert out["total_lb"] > 0
        families = {p["family"] for p in out["parts"]}
        assert "wood" in families

    def test_every_example_produces_a_sane_weight(self):
        for path in sorted(EXAMPLES.glob("*.json")):
            spec = json.loads(path.read_text())
            out = compute_takeoff(compute_primitives(spec), spec)
            assert out["total_lb"] > 0, f"{path.name} weighs nothing"
            assert out["total_lb"] < 50_000, f"{path.name} weighs absurdly much"


class TestSquareTubeJoints:
    def test_square_stock_is_not_treated_as_round(self):
        """Nothing round-section wraps square stock: no band clamp, no slip
        fitter, and its anchor plate is square."""
        from blender.builders.hardware import _is_round_section, _is_square_tube

        square = tube(0.0254, 0.9, 0.0048, "square")
        round_ = tube(0.0254, 0.9, 0.0048)
        assert _is_square_tube(square) and not _is_square_tube(round_)
        assert _is_round_section(round_)
        assert not _is_round_section(square)

    def test_square_stock_anchors_on_a_square_plate(self):
        """A square-tube post anchors on a square plate with corner bolts,
        the same as a box post — never a round flange."""
        from blender.builders.connections import ground_connection

        square = ground_connection(0.0254, shape="square", name_prefix="j1_")
        round_ = ground_connection(0.0254, shape="round", name_prefix="j1_")
        assert next(p for p in square if p.name.endswith("flange")).kind == "box"
        assert next(p for p in round_ if p.name.endswith("flange")).kind == "cylinder"

    def test_the_plate_is_actually_drilled(self):
        """FOOT DETAIL A: the plate carries a real hole per anchor bolt,
        emitted as negative space rather than merely implied by a stud."""
        spec = load("street_light.json")
        spec["toggles"].append({"id": "connection_hardware", "label": "CH",
                                "value": True})
        prims = compute_primitives(spec)
        holes = [p for p in prims if "anchor_hole" in p.name]
        bolts = [p for p in prims if "anchor_bolt" in p.name]
        assert holes, "the base plate is drilled"
        assert len(holes) == len(bolts), "one hole per anchor bolt"
        assert all(p.cut for p in holes), "holes are negative space"
        # every hole is a clearance fit over its bolt, never a press fit
        for hole, bolt in zip(holes, bolts):
            assert hole.params["radius"] > bolt.params["radius"]
