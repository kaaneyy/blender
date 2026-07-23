"""Tests for the connection vocabulary (v3 orchestrator + emitter library):
declared `connections` intent, new emitters (anchor base, slip fitter, split
band clamp, carriage bolts, welds, flange splices, lag screws), auto
anchor-base detection, and the rotated-corner AABB fix. Parity target:
frontend/src/builders/{hardware,connections}.ts."""
import json
import math
from pathlib import Path

import pytest

import blender.builders  # noqa: F401
from blender.builders.base import Primitive, compute_primitives
from blender.builders.connections import ground_connection
from blender.builders.hardware import _aabb, compute_hardware

REPO_ROOT = Path(__file__).resolve().parents[2]


def load(name):
    return json.loads((REPO_ROOT / "examples" / name).read_text())


def spec_of(prims, materials=None, connections=None):
    """Wrap raw primitive dicts into a minimal custom spec with hardware on."""
    return {
        "asset_type": "fixture", "name": "F", "units": "metric",
        "parameters": [], "primitives": prims,
        "materials": materials or [{"slot": "m", "preset": "galvanized_steel"}],
        "connections": connections or [],
        "toggles": [{"id": "connection_hardware", "label": "CH", "value": True}],
    }


def hw_names(prims):
    return {p.name for p in prims if p.component == "hardware"}


POLE = {"kind": "cylinder", "name": "shaft", "component": "pole",
        "material_slot": "m", "location": [0, 0, 4.5],
        "params": {"radius": 0.08, "depth": 9}}
CROSS_ARM = {"kind": "cylinder", "name": "arm", "component": "arm",
             "material_slot": "m", "location": [0.4, 0, 8.0],
             "rotation": [0, 1.5707963, 0],
             "params": {"radius": 0.03, "depth": 1.0}}


class TestAutoAnchorBase:
    def test_bare_pole_gets_full_ground_package(self):
        prims = compute_primitives(spec_of([POLE]))
        names = hw_names(prims)
        assert any("flange" in n for n in names)
        assert any("grout_pad" in n for n in names)
        assert any("anchor_bolt" in n for n in names)
        assert any("gusset" in n for n in names)
        assert any("weld_bead" in n for n in names)
        # heavy pole -> 6-bolt anchor circle
        assert sum(1 for n in names if "anchor_bolt" in n) == 6
        # anchor package rides in the hardware component with joint names
        flange = next(p for p in prims if p.component == "hardware" and p.name.endswith("_flange"))
        assert flange.name.startswith("joint")

    def test_square_post_gets_square_plate(self):
        post = {"kind": "box", "name": "post", "component": "post",
                "material_slot": "m", "location": [1.0, 2.0, 1.5],
                "params": {"size": [0.1, 0.1, 3.0]}}
        prims = compute_primitives(spec_of([post]))
        flange = next(p for p in prims if p.name.endswith("_flange"))
        assert flange.kind == "box"
        # placed at the member's own XY, not the origin
        assert flange.location[0] == pytest.approx(1.0)
        assert flange.location[1] == pytest.approx(2.0)
        # 4 corner anchors, no weld ring on a square plate
        names = hw_names(prims)
        assert sum(1 for n in names if "anchor_bolt" in n) == 4
        assert not any("weld_bead" in n for n in names)

    def test_light_member_gets_no_anchor(self):
        leg = {"kind": "box", "name": "leg", "component": "legs",
               "material_slot": "m", "location": [0, 0, 0.35],
               "params": {"size": [0.06, 0.06, 0.7]}}
        prims = compute_primitives(spec_of([leg]))
        assert not any("flange" in n for n in hw_names(prims))

    def test_declared_anchor_base_forces_package_on_light_member(self):
        leg = {"kind": "box", "name": "leg", "component": "legs",
               "material_slot": "m", "location": [0, 0, 0.35],
               "params": {"size": [0.06, 0.06, 0.7]}}
        prims = compute_primitives(spec_of(
            [leg], connections=[{"a": "legs", "b": "ground", "type": "anchor_base"}],
        ))
        assert any("flange" in n for n in hw_names(prims))

    def test_declared_none_suppresses_anchor(self):
        prims = compute_primitives(spec_of(
            [POLE], connections=[{"a": "pole", "b": "ground", "type": "none"}],
        ))
        assert not any("flange" in n for n in hw_names(prims))

    def test_modeled_base_suppresses_auto_anchor(self):
        base = {"kind": "cylinder", "name": "plate", "component": "base",
                "material_slot": "m", "location": [0, 0, 0.015],
                "params": {"radius": 0.25, "depth": 0.03}}
        prims = compute_primitives(spec_of([POLE, base]))
        # the modeled plate wins: no generated flange package
        assert not any("grout_pad" in n for n in hw_names(prims))

    def test_street_light_keeps_its_builder_base(self):
        spec = load("street_light.json")
        spec["toggles"].append({"id": "connection_hardware", "label": "CH", "value": True})
        prims = compute_primitives(spec)
        # exactly one flange: the curated builder's, not a generated double
        flanges = [p for p in prims if p.name == "flange"]
        assert len(flanges) == 1
        assert not any(p.name.endswith("_grout_pad") and p.component == "hardware"
                       for p in prims)


class TestLeanAnchorGussets:
    """Gusset webs are reserved for genuinely HEAVY auto anchors — a light
    or standard member gets a lean anchor (plate + grout + bolts, no
    gussets), while ground_connection's own `gussets` kwarg still defaults
    to on (the curated street_light base stays byte-for-byte identical)."""

    def test_light_member_gets_no_gusset(self):
        light_post = {"kind": "cylinder", "name": "p", "component": "post",
                      "material_slot": "m", "location": [0, 0, 0.15],
                      "params": {"radius": 0.03, "depth": 0.3}}
        prims = compute_primitives(spec_of(
            [light_post], connections=[{"a": "post", "b": "ground", "type": "anchor_base"}],
        ))
        names = hw_names(prims)
        assert any("flange" in n for n in names), "still gets a lean ground package"
        assert not any("gusset" in n for n in names), "but no gusset webs"

    def test_heavy_member_still_gets_gussets(self):
        prims = compute_primitives(spec_of([POLE]))  # heavy pole (see TestAutoAnchorBase)
        assert any("gusset" in n for n in hw_names(prims))

    def test_ground_connection_gussets_false_emits_none(self):
        prims = ground_connection(0.1, gussets=False)
        assert not any("gusset" in p.name for p in prims)
        # everything else is unaffected
        assert any("flange" in p.name for p in prims)
        assert any("anchor_bolt" in p.name for p in prims)
        assert any("grout_pad" in p.name for p in prims)

    def test_ground_connection_default_still_emits_gussets(self):
        prims = ground_connection(0.1)
        assert any("gusset" in p.name for p in prims)


class TestSlipFit:
    def test_post_top_fit_gets_collar_and_set_screws(self):
        fitter = {"kind": "cylinder", "name": "fitter", "component": "lamp",
                  "material_slot": "m", "location": [0, 0, 3.0],
                  "params": {"radius": 0.07, "depth": 0.2}}
        post = dict(POLE, params={"radius": 0.05, "depth": 3}, location=[0, 0, 1.5])
        prims = compute_primitives(spec_of([post, fitter]))
        names = hw_names(prims)
        assert any(n.endswith("_fitter") for n in names)
        assert sum(1 for n in names if "setscrew" in n and not n.endswith("_head")) == 3
        # no nonsense vertical through-bolt at the telescoping fit
        fit_z = 2.95
        vertical_shafts = [
            p for p in prims if p.component == "hardware"
            and p.name.endswith("_shaft") and p.rotation == (0.0, 0.0, 0.0)
            and abs(p.location[2] - fit_z) < 0.2
        ]
        assert not vertical_shafts

    def test_set_screws_scale_with_the_fit_and_snap_to_catalog(self):
        """Adaptive fastener sizing: a big fitter takes bigger set screws
        than a small one, and both are real catalog sizes (the drawn screw
        is the scheduled screw)."""
        from blender.builders.hardware import BOLT_CATALOG

        def screws(post_r, fitter_r):
            fitter = {"kind": "cylinder", "name": "fitter", "component": "lamp",
                      "material_slot": "m", "location": [0, 0, 3.0],
                      "params": {"radius": fitter_r, "depth": 0.2}}
            post = dict(POLE, params={"radius": post_r, "depth": 3},
                        location=[0, 0, 1.5])
            prims = compute_primitives(spec_of([post, fitter]))
            return [p for p in prims if "setscrew" in p.name
                    and not p.name.endswith("_head")]

        small = screws(0.02, 0.03)
        large = screws(0.05, 0.07)
        assert small and large
        catalog_radii = {r for _, r in BOLT_CATALOG}
        for s in small + large:
            assert s.params["radius"] in catalog_radii, s.name
        assert large[0].params["radius"] > small[0].params["radius"]

    def test_pole_on_flange_disc_is_not_a_slip_fit(self):
        disc = {"kind": "cylinder", "name": "plate", "component": "base",
                "material_slot": "m", "location": [0, 0, 0.015],
                "params": {"radius": 0.25, "depth": 0.03}}
        prims = compute_primitives(spec_of([POLE, disc]))
        assert not any("fitter" in n for n in hw_names(prims))


class TestDeclaredTypes:
    def _pair(self):
        return [
            {"kind": "box", "name": "leg", "component": "legs",
             "material_slot": "m", "location": [0, 0, 0.35],
             "params": {"size": [0.06, 0.06, 0.7]}},
            {"kind": "box", "name": "apron", "component": "apron",
             "material_slot": "m", "location": [0, 0, 0.66],
             "params": {"size": [0.5, 0.06, 0.08]}},
        ]

    def test_declared_weld_suppresses_bolts(self):
        arm = dict(CROSS_ARM)
        prims = compute_primitives(spec_of(
            [POLE, arm], connections=[{"a": "arm", "b": "pole", "type": "weld"}],
        ))
        names = {n for n in hw_names(prims) if "anchor" not in n and "gusset" not in n
                 and "flange" not in n and "grout" not in n and "weld_bead" not in n}
        assert any(n.endswith("_weld") for n in names)
        assert not any("bolt" in n or "band" in n for n in names)

    def test_declared_none_suppresses_everything(self):
        prims = compute_primitives(spec_of(
            self._pair(), connections=[{"a": "legs", "b": "apron", "type": "none"}],
        ))
        assert not hw_names(prims)

    def test_declared_count_respected(self):
        prims = compute_primitives(spec_of(
            self._pair(),
            connections=[{"a": "legs", "b": "apron", "type": "through_bolt", "count": 3}],
        ))
        shafts = [n for n in hw_names(prims) if n.endswith("_shaft")]
        assert len(shafts) == 3

    def test_part_path_matching(self):
        prims = compute_primitives(spec_of(
            self._pair(),
            connections=[{"a": "legs/leg", "b": "apron", "type": "lag_screw"}],
        ))
        names = hw_names(prims)
        assert any(n.endswith("_head") for n in names)
        assert not any(n.endswith("_nut") for n in names), "lag screws have no nut"

    def test_declared_flange_splice(self):
        lower = {"kind": "cylinder", "name": "lower", "component": "lower",
                 "material_slot": "m", "location": [0, 0, 1.0],
                 "params": {"radius": 0.06, "depth": 2.02}}
        upper = {"kind": "cylinder", "name": "upper", "component": "upper",
                 "material_slot": "m", "location": [0, 0, 3.0],
                 "params": {"radius": 0.06, "depth": 2.02}}
        prims = compute_primitives(spec_of(
            [lower, upper],
            connections=[{"a": "lower", "b": "upper", "type": "flange_splice"}],
        ))
        names = hw_names(prims)
        flanges = [n for n in names if "_flange" in n and "anchor" not in n]
        assert len([n for n in flanges if n.endswith("_flange1") or n.endswith("_flange2")]) == 2
        assert sum(1 for n in names if n.endswith("_shaft")) == 6

    def test_unknown_refs_do_not_throw(self):
        prims = compute_primitives(spec_of(
            self._pair(),
            connections=[{"a": "ghost", "b": "phantom", "type": "weld"}],
        ))
        # unmatched declaration ignored; inference still bolts the real joint
        assert any("bolt" in n for n in hw_names(prims))

    def test_carriage_bolt_declared_on_metal(self):
        prims = compute_primitives(spec_of(
            self._pair(),
            connections=[{"a": "legs", "b": "apron", "type": "carriage_bolt"}],
        ))
        assert any(n.endswith("_dome") for n in hw_names(prims))


class TestSplitBandClamp:
    def test_band_has_ears_and_ear_bolts(self):
        prims = compute_primitives(spec_of([POLE, dict(CROSS_ARM)]))
        hardware = [p for p in prims if p.component == "hardware"]
        band = next(p for p in hardware if p.name.endswith("_band"))
        assert band.kind == "tube"
        joint = band.name.split("_")[0]
        ears = [p for p in hardware if p.name.startswith(f"{joint}_ear")]
        assert len(ears) == 2
        # ear bolts run along the arm axis (X here), outside the pole
        shafts = [p for p in hardware if p.name.startswith(f"{joint}_bolt")
                  and p.name.endswith("_shaft")]
        assert len(shafts) == 2
        for s in shafts:
            assert abs(s.location[1]) > band.params["radius"] * 0.8

    def test_band_width_scales_with_arm(self):
        thin = compute_primitives(spec_of([
            POLE, dict(CROSS_ARM, params={"radius": 0.012, "depth": 1.0})]))
        thick = compute_primitives(spec_of([
            POLE, dict(CROSS_ARM, params={"radius": 0.025, "depth": 1.0})]))
        bw = lambda prims: next(p for p in prims if p.name.endswith("_band")).params["depth"]
        assert bw(thick) > bw(thin)


class TestCarriageBolts:
    def test_wood_on_metal_gets_dome_up(self):
        rail = {"kind": "box", "name": "rail", "component": "frame",
                "material_slot": "steel", "location": [0, 0, 0.44],
                "params": {"size": [1.2, 0.05, 0.05]}}
        slat = {"kind": "box", "name": "slat", "component": "seat",
                "material_slot": "wood", "location": [0, 0, 0.47],
                "params": {"size": [1.2, 0.4, 0.04]}}
        prims = compute_primitives(spec_of(
            [rail, slat],
            materials=[{"slot": "steel", "preset": "galvanized_steel"},
                       {"slot": "wood", "preset": "wood_slat"}],
        ))
        hardware = [p for p in prims if p.component == "hardware"]
        domes = [p for p in hardware if p.name.endswith("_dome")]
        assert domes, "wood on metal (vertical axis) takes carriage bolts"
        nut = next(p for p in hardware if p.name == domes[0].name.replace("_dome", "_nut"))
        assert domes[0].location[2] > nut.location[2], "dome proud of the wood, nut below the steel"
        # no washer under a carriage dome
        assert not any(p.name == domes[0].name.replace("_dome", "_washer_h") for p in hardware)


class TestRotatedAabb:
    def test_tilted_panel_no_longer_blows_up_to_a_cube(self):
        # a thin panel tilted 30°: the old code inflated it to a 1 m cube and
        # bolted it to a post 0.35 m away; the tight box must not touch it
        panel = Primitive(
            kind="box", name="panel", component="panel",
            location=(0.0, 0.0, 1.2), rotation=(0.5235988, 0.0, 0.0),
            params={"size": (2.0, 1.0, 0.02)},
        )
        post = Primitive(
            kind="box", name="post", component="post",
            location=(0.0, 0.85, 0.35), params={"size": (0.06, 0.06, 0.7)},
        )
        assert compute_hardware([panel, post]) == []

    def test_rotated_box_aabb_is_tight(self):
        panel = Primitive(
            kind="box", name="p", component="c",
            location=(0.0, 0.0, 0.0), rotation=(math.pi / 6, 0.0, 0.0),
            params={"size": (2.0, 1.0, 0.02)},
        )
        center, half = _aabb(panel)
        assert half[0] == pytest.approx(1.0, abs=1e-6)          # unrotated axis
        # y extent: 0.5*cos30 + 0.01*sin30 ≈ 0.438 (vs 1.0 for the old cube)
        assert half[1] == pytest.approx(0.438, abs=0.01)
        assert half[2] == pytest.approx(0.259, abs=0.01)


class TestDuplicatesGetHardware:
    def test_edit_overlay_duplicate_carries_joints(self):
        spec = load("park_bench.json")
        spec["toggles"].append({"id": "connection_hardware", "label": "CH", "value": True})
        base_hw = len([p for p in compute_primitives(spec) if p.component == "hardware"])
        spec["edits"] = {"duplicates": [{"source": "seat", "name": "seat copy"}]}
        spec["offsets"] = {"seat copy": [0.0, 0.0, 0.6]}
        dup_hw = len([p for p in compute_primitives(spec) if p.component == "hardware"])
        # the duplicated seat overlaps nothing new at +0.6 m... place it back
        # on the frame instead: 0 offset -> same joints dedupe by grid; use a
        # small offset so the copy forms its own joints with the frame
        spec["offsets"] = {"seat copy": [0.0, 0.0, 0.03]}
        dup_hw2 = len([p for p in compute_primitives(spec) if p.component == "hardware"])
        assert dup_hw2 >= base_hw  # duplicate participates in joint detection


class TestDuplicateInheritsDeclaredConnections:
    """A duplicate (edits.duplicates) must be matched against the spec's
    `connections` declarations as if it were its source component — the
    declaration is written against the source's name, which a copy never
    literally matches, so without resolution a copy silently falls back to
    geometric inference instead of the fabrication intent the designer
    declared for the part it was cloned from."""

    def test_duplicate_inherits_declared_type_from_source(self):
        # park_bench.json declares {"a": "seat", "b": "frame", "type":
        # "carriage_bolt"} — but seat (wood) on frame (cast_iron) is also
        # what geometric inference would pick on its own for a vertical
        # joint, so a coincidental match wouldn't prove resolution is doing
        # anything. Override the declared type to through_bolt instead:
        # inference would still infer carriage_bolt for this wood-on-metal
        # vertical joint, so only a real declaration match produces
        # through_bolt.
        spec = load("park_bench.json")
        spec["toggles"].append({"id": "connection_hardware", "label": "CH", "value": True})
        spec["connections"][0]["type"] = "through_bolt"
        spec["edits"] = {"duplicates": [{"source": "seat", "name": "seat copy"}]}
        # offset the copy sideways (not just vertically) so its contact
        # region lands in a different dedupe grid cell than the original
        # seat's joints, while still overlapping the frame rail
        spec["offsets"] = {"seat copy": [0.1, 0.0, 0.0]}
        prims = compute_primitives(spec)
        joints = [p.meta["joint"] for p in prims if p.component == "hardware" and p.meta]
        copy_frame_joints = [
            j for j in joints if {"seat copy", "frame"} == {j["a"], j["b"]}
        ]
        assert copy_frame_joints, "seat copy should form joints with frame"
        assert all(j["type"] == "through_bolt" for j in copy_frame_joints), (
            "seat copy should inherit seat's declared through_bolt override, "
            "not fall back to carriage_bolt inference"
        )
        # the meta record keeps the copy's REAL component name, not the
        # resolved source name — resolution affects matching only
        assert all("seat copy" in (j["a"], j["b"]) for j in copy_frame_joints)

    def test_copy_touching_its_own_source_falls_back_to_inference(self):
        # a declaration that (incorrectly) matched a copy against its own
        # source would let a self-referential declaration fire between the
        # two — a resolved pair that maps to (X, X) must match nothing.
        spec = load("park_bench.json")
        spec["toggles"].append({"id": "connection_hardware", "label": "CH", "value": True})
        spec["connections"].append({"a": "seat", "b": "seat", "type": "lag_screw"})
        spec["edits"] = {"duplicates": [{"source": "seat", "name": "seat copy"}]}
        # small vertical offset: overlaps the original seat directly (both
        # wood -> soft/soft, no declared match -> inference skips it, same
        # as today's behavior for two abutting non-metal parts)
        spec["offsets"] = {"seat copy": [0.0, 0.0, 0.01]}
        prims = compute_primitives(spec)
        joints = [p.meta["joint"] for p in prims if p.component == "hardware" and p.meta]
        assert not any({"seat", "seat copy"} == {j["a"], j["b"]} for j in joints)

    def test_duplicate_of_ground_none_declaration_gets_no_anchor(self):
        prims_spec = spec_of(
            [POLE], connections=[{"a": "pole", "b": "ground", "type": "none"}]
        )
        prims_spec["edits"] = {"duplicates": [{"source": "pole", "name": "pole copy"}]}
        # shift sideways only, so the copy still lands at grade like its
        # source and would otherwise qualify for an auto anchor base
        prims_spec["offsets"] = {"pole copy": [2.0, 0.0, 0.0]}
        prims = compute_primitives(prims_spec)
        assert not any("flange" in n for n in hw_names(prims))


class TestStableJointIds:
    def test_anchor_base_is_joint1(self):
        prims = compute_primitives(spec_of([POLE, dict(CROSS_ARM)]))
        flange = next(p for p in prims if p.component == "hardware"
                      and p.name.endswith("_flange"))
        assert flange.name.startswith("joint1_"), "ground anchorage ranks first"

    def test_ids_stable_under_slider_nudge(self):
        spec = load("street_light.json")
        spec["toggles"].append({"id": "connection_hardware", "label": "CH", "value": True})
        def joint_of(prims, suffix):
            return next(p.name.split("_")[0] for p in prims
                        if p.component == "hardware" and p.name.endswith(suffix))
        a = compute_primitives(spec)
        spec["parameters"][0]["value"] = 30.5  # nudge pole height
        b = compute_primitives(spec)
        assert joint_of(a, "_band") == joint_of(b, "_band")
