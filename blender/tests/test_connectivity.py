"""Tests for the buildability / load-path validator (connectivity.py):
contact graph, floating-part detection with nearest support + gap, below-
grade geometry, and dead declared connections; the deterministic
scale-sanity checker (check_scale_sanity); the deterministic embedded-
part detector (check_embedded_parts); the dead-CONTROL detector
(check_dead_controls) — a parameter/toggle a spec exposes but that provably
drives no geometry; and the toggle feature-completeness detector
(check_toggle_dependencies) — a toggle that, switched off, orphans a
still-visible part it wasn't co-gated with."""
import json
from pathlib import Path

import pytest

import blender.builders  # noqa: F401
from blender.builders.base import compute_primitives
from blender.builders.connectivity import (
    EMBED_OVERLAP_RATIO,
    PIERCE_AXIS_FRACTION,
    buildability_errors,
    check_buildability,
    check_dead_controls,
    check_embedded_parts,
    check_scale_sanity,
    check_toggle_dependencies,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_NAMES = sorted(p.name for p in (REPO_ROOT / "examples").glob("*.json"))


def load(name):
    return json.loads((REPO_ROOT / "examples" / name).read_text())


def findings_for(spec):
    return check_buildability(compute_primitives(spec), spec)


def scale_findings_for(spec):
    return check_scale_sanity(compute_primitives(spec), spec)


def embedded_findings_for(spec):
    return check_embedded_parts(compute_primitives(spec), spec)


class TestLoadPath:
    def test_grounded_examples_pass(self):
        for name in ("park_bench.json", "street_light.json"):
            findings = findings_for(load(name))
            assert buildability_errors(findings) == [], name

    def test_floating_component_reported_with_gap(self):
        spec = load("park_bench.json")
        # raise every seat slat well above frame AND backrest: the seat floats
        for p in spec["primitives"]:
            if p["component"] == "seat":
                p["location"][2] = "seat_height + 1.5"
        findings = findings_for(spec)
        errors = buildability_errors(findings)
        seat = next(f for f in errors if "'seat'" in f["message"])
        assert "floats" in seat["message"]
        assert "mm away" in seat["message"]
        assert seat["parameter_id"] == "__buildability__"
        assert seat["severity"] == "error"

    def test_barely_resting_on_backrest_is_not_floating(self):
        # geometric truth: a raised seat that still overlaps the backrest has
        # a (silly but real) load path — the graph must find it
        spec = load("park_bench.json")
        for p in spec["primitives"]:
            if p["component"] == "seat":
                p["location"][2] = "seat_height + 0.5"
        findings = findings_for(spec)
        assert not any(f["limit_type"] == "floating" for f in findings)

    def test_indirect_support_counts(self):
        # slats sit on rails which sit on legs: seat never touches the
        # ground directly but reaches it through the graph
        findings = findings_for(load("park_bench.json"))
        assert not any(f["limit_type"] == "floating" for f in findings)


class TestBelowGrade:
    def test_below_ground_geometry_is_flagged(self):
        spec = load("park_bench.json")
        spec["primitives"].append({
            "kind": "box", "name": "buried", "component": "frame",
            "material_slot": "frame", "location": [0, 0, -0.1],
            "params": {"size": [0.2, 0.2, 0.4]},
        })
        findings = findings_for(spec)
        below = [f for f in findings if f["limit_type"] == "below_ground"]
        assert below and below[0]["severity"] == "warning"
        assert "below grade" in below[0]["message"]


class TestDeclarations:
    def test_dead_declaration_reported(self):
        spec = load("park_bench.json")
        spec["connections"] = [
            {"a": "seat", "b": "ghost_component", "type": "through_bolt"},
        ]
        findings = findings_for(spec)
        dead = [f for f in findings if f["limit_type"] == "unmatched_declaration"]
        assert dead and "don't exist" in dead[0]["message"]

    def test_no_contact_declaration_reports_gap(self):
        spec = load("park_bench.json")
        # armrests are toggled OFF -> declared armrest connections in the
        # example are pruned with the geometry; instead declare a joint
        # between two parts that exist but never touch
        spec["connections"] = [
            {"a": "seat", "b": "backrest/back_slat_high", "type": "through_bolt"},
        ]
        findings = findings_for(spec)
        dead = [f for f in findings if f["limit_type"] == "unmatched_declaration"]
        assert dead and "mm apart" in dead[0]["message"]

    def test_ground_declarations_are_exempt(self):
        spec = load("park_bench.json")
        spec["connections"] = [{"a": "frame", "b": "ground", "type": "none"}]
        findings = findings_for(spec)
        assert not any(f["limit_type"] == "unmatched_declaration" for f in findings)


def _pergola_with_solar(size):
    """The WHY repro: pergola.json (default connection_hardware=true) plus a
    seated 'solar' box component of the given (w, d, h) resting on the front
    beam. Scale is the only thing wrong with it — it is fully supported and
    declares no connections, so check_buildability/audit are both silent."""
    spec = load("pergola.json")
    spec["primitives"].append({
        "kind": "box", "name": "panel", "component": "solar",
        "material_slot": "beams",
        "location": [0, "span_y/2", "post_height + beam_depth + 0.05"],
        "params": {"size": list(size)},
    })
    return spec


class TestScaleSanity:
    @pytest.mark.parametrize("name", EXAMPLE_NAMES)
    def test_bundled_examples_are_scale_clean(self, name):
        # false-positive gate: every shipped example must produce zero scale
        # findings — a neutered check that always returns [] would also pass
        # this alone, which is why the toy/giant/envelope tests below assert
        # real findings on purpose-built reproductions.
        findings = scale_findings_for(load(name))
        assert findings == [], (name, findings)

    def test_toy_panel_is_a_scale_outlier(self):
        # reproduces the bug report: a 0.3x0.2x0.05 m "solar panel" seated on
        # a ~3.7 m pergola is toy-scale relative to the structure.
        spec = _pergola_with_solar((0.3, 0.2, 0.05))
        findings = scale_findings_for(spec)
        outliers = [f for f in findings if f["kind"] == "scale_outlier"]
        assert len(outliers) == 1
        finding = outliers[0]
        assert finding["component"] == "solar"
        assert finding["severity"] == "warning"
        assert "solar" in finding["message"]
        assert "0.30 m" in finding["message"]
        # no other scale finding should fire alongside it
        assert findings == outliers

    def test_giant_panel_is_a_scale_giant(self):
        # the inverse bug: a 12x9x0.08 m "solar panel" dwarfs the same
        # pergola it's supposedly mounted on.
        spec = _pergola_with_solar((12, 9, 0.08))
        findings = scale_findings_for(spec)
        giants = [f for f in findings if f["kind"] == "scale_giant"]
        assert len(giants) == 1
        finding = giants[0]
        assert finding["component"] == "solar"
        assert finding["severity"] == "warning"
        assert "solar" in finding["message"]

    def test_oversized_envelope_is_flagged(self):
        spec = {
            "asset_type": "custom", "name": "Huge", "units": "metric",
            "code_mode": "advisory", "parameters": [], "toggles": [],
            "materials": [{"slot": "frame", "preset": "wood_slat"}],
            "components": ["frame"], "connections": [],
            "primitives": [
                {"kind": "box", "name": "slab", "component": "frame",
                 "material_slot": "frame", "location": [0, 0, 20],
                 "params": {"size": [40, 5, 5]}},
            ],
        }
        findings = scale_findings_for(spec)
        extreme = [f for f in findings if f["kind"] == "envelope_extreme"]
        assert len(extreme) == 1
        assert extreme[0]["component"] is None
        assert extreme[0]["severity"] == "warning"

    def test_hardware_and_cut_prims_never_contribute(self):
        # connection_hardware is already on for pergola.json (bolts/washers
        # get generated as tiny 'hardware'-component prims); a giant cut
        # prim (negative space) is added on top to prove neither kind
        # pollutes the envelope math.
        spec = load("pergola.json")
        spec["primitives"].append({
            "kind": "box", "name": "phantom_cut", "component": "posts",
            "material_slot": "posts", "cut": True,
            "location": [0, 0, 5], "params": {"size": [100, 100, 100]},
        })
        findings = scale_findings_for(spec)
        assert findings == []

    def test_never_raises_on_valid_primitives(self):
        # a spec with a single tiny grounded component and no siblings must
        # not raise even though most ratio guards divide by other quantities
        spec = {
            "asset_type": "custom", "name": "Lonely", "units": "metric",
            "code_mode": "advisory", "parameters": [], "toggles": [],
            "materials": [{"slot": "frame", "preset": "wood_slat"}],
            "components": ["frame"], "connections": [],
            "primitives": [
                {"kind": "box", "name": "cube", "component": "frame",
                 "material_slot": "frame", "location": [0, 0, 0.05],
                 "params": {"size": [0.1, 0.1, 0.1]}},
            ],
        }
        findings = check_scale_sanity(compute_primitives(spec), spec)
        assert isinstance(findings, list)

    def test_neutered_check_would_fail_this_suite(self):
        # guard against a no-op regression: at least one of the purpose-built
        # reproductions above must yield a non-empty result.
        spec = _pergola_with_solar((0.3, 0.2, 0.05))
        assert scale_findings_for(spec) != []


def _victorian_post_spec():
    """The WHY repro: a "15 ft victorian post" built on the generic
    primitives path as a base plate, a hairline ~20mm-diameter/4.57m-tall
    cylinder shaft, and a small finial. Passes buildability (the wire
    reaches grade) and every scale_outlier/scale_giant/envelope_extreme
    check above (its envelope is asset-scale, not toy- or giant-scale) —
    only a per-primitive cross-section-vs-span check catches it."""
    return {
        "asset_type": "custom", "name": "VictorianPost", "units": "metric",
        "code_mode": "advisory", "parameters": [], "toggles": [],
        "materials": [{"slot": "post", "preset": "wood_slat"}],
        "components": ["base", "shaft", "finial"], "connections": [],
        "primitives": [
            {"kind": "box", "name": "base_plate", "component": "base",
             "material_slot": "post", "location": [0, 0, 0.02],
             "params": {"size": [0.3, 0.3, 0.04]}},
            {"kind": "cylinder", "name": "shaft", "component": "shaft",
             "material_slot": "post", "location": [0, 0, 2.285],
             "params": {"radius": 0.01, "depth": 4.57}},
            {"kind": "sphere", "name": "finial", "component": "finial",
             "material_slot": "post", "location": [0, 0, 4.6],
             "params": {"radius": 0.05}},
        ],
    }


def _segmented_post_spec():
    """The segmentation dodge: a wire-thin post assembled from 8 stacked
    SHORT cylinder segments (each 0.6 m span, individually under
    SLIVER_MIN_SPAN=0.75 m so NONE trips the per-primitive check on its
    own), plus a base plate and a finial component at the top. The 'post'
    component's own union envelope (0-4.8 m span, 16mm cross-section) reads
    as exactly the same hairline rod the per-primitive check exists to
    catch — closing the dodge requires weighing the component's ENVELOPE,
    not just each segment individually. A finial component sits at the top
    (making the rest-of-asset union tall too, so scale_giant also stays
    quiet) — the WHY repro this brief is closing."""
    prims = [
        {"kind": "box", "name": "base_plate", "component": "base",
         "material_slot": "post", "location": [0, 0, 0.02],
         "params": {"size": [0.3, 0.3, 0.04]}},
    ]
    for i in range(8):
        prims.append({
            "kind": "cylinder", "name": f"segment_{i}", "component": "post",
            "material_slot": "post", "location": [0, 0, 0.3 + i * 0.6],
            "params": {"radius": 0.008, "depth": 0.6},
        })
    prims.append({
        "kind": "sphere", "name": "finial", "component": "finial",
        "material_slot": "post", "location": [0, 0, 4.85],
        "params": {"radius": 0.05},
    })
    return {
        "asset_type": "custom", "name": "SegmentedPost", "units": "metric",
        "code_mode": "advisory", "parameters": [], "toggles": [],
        "materials": [{"slot": "post", "preset": "wood_slat"}],
        "components": ["base", "post", "finial"], "connections": [],
        "primitives": prims,
    }


def _wide_arc_spec():
    """A hoop/arch built from short, thin segments distributed along a WIDE
    curve: 6 cylinders (radius 0.015 m, depth 0.3 m — each individually
    nowhere near SLIVER_MIN_SPAN) positioned along a 1.5 m-wide, ~0.55 m
    tall arc. No single segment is a rod, and — unlike _segmented_post_spec
    — the component's own union envelope isn't one either: its rise (~0.55
    m) is far too large relative to its 1.53 m span to read as a thin
    cross-section, so the component-level rod check must also stay quiet."""
    xs = [-0.75, -0.45, -0.15, 0.15, 0.45, 0.75]
    zs = [0.15, 0.30, 0.40, 0.40, 0.30, 0.15]
    prims = [
        {"kind": "cylinder", "name": f"seg_{i}", "component": "arch",
         "material_slot": "arch", "location": [x, 0, z],
         "params": {"radius": 0.015, "depth": 0.3}}
        for i, (x, z) in enumerate(zip(xs, zs))
    ]
    return {
        "asset_type": "custom", "name": "Arch", "units": "metric",
        "code_mode": "advisory", "parameters": [], "toggles": [],
        "materials": [{"slot": "arch", "preset": "galvanized_steel"}],
        "components": ["arch"], "connections": [],
        "primitives": prims,
    }


class TestSliverMembers:
    @pytest.mark.parametrize("name", EXAMPLE_NAMES)
    def test_bundled_examples_are_sliver_clean(self, name):
        # false-positive gate: every shipped example (mast arm, park_bench
        # slats, bike_rack hoops included) must produce zero sliver_member
        # findings as shipped — a neutered check that always returns []
        # would also pass this alone, which is why the repro/sheet/flagpole
        # tests below assert real findings on purpose-built reproductions.
        findings = [f for f in scale_findings_for(load(name))
                    if f["kind"] == "sliver_member"]
        assert findings == [], (name, findings)

    def test_street_light_banner_bracket_and_double_arm_stay_clean(self):
        # banner_bracket/double_arm default OFF in the bundled example, so
        # the parametrized loop above never builds them. The bracket's
        # 32mm-diameter, 0.9m pin (~28:1 aspect) is exactly the kind of
        # genuinely-slender hardware this check must not confuse with the
        # WHY repro's ~230:1-aspect post.
        spec = load("street_light.json")
        for t in spec.setdefault("toggles", []):
            if t["id"] in ("banner_bracket", "double_arm"):
                t["value"] = True
        findings = [f for f in scale_findings_for(spec) if f["kind"] == "sliver_member"]
        assert findings == []

    def test_wire_thin_post_is_flagged(self):
        # the WHY repro itself: exactly the shaft is flagged, not the base
        # plate (short, wide) or the finial (short, roughly cubic).
        spec = _victorian_post_spec()
        findings = scale_findings_for(spec)
        slivers = [f for f in findings if f["kind"] == "sliver_member"]
        assert len(slivers) == 1
        finding = slivers[0]
        assert finding["component"] == "shaft"
        assert finding["severity"] == "warning"
        assert "shaft/shaft" in finding["message"]
        assert "4.57 m" in finding["message"]
        assert "0.020" in finding["message"]
        # no other scale finding fires alongside it
        assert findings == slivers

    def test_thin_sheet_is_not_flagged(self):
        # a sign face: one tiny dim (thickness) and one wide one (the face
        # itself) — a sheet, not a rod. Must never be flagged: this is the
        # exact shape the "both cross dims tiny" requirement exists for.
        spec = {
            "asset_type": "custom", "name": "Sign", "units": "metric",
            "code_mode": "advisory", "parameters": [], "toggles": [],
            "materials": [{"slot": "sign", "preset": "wood_slat"}],
            "components": ["face"], "connections": [],
            "primitives": [
                {"kind": "box", "name": "panel", "component": "face",
                 "material_slot": "sign", "location": [0, 0, 1.0],
                 "params": {"size": [2.0, 1.0, 0.003]}},
            ],
        }
        findings = scale_findings_for(spec)
        assert not any(f["kind"] == "sliver_member" for f in findings)

    def test_slender_real_flagpole_is_not_flagged(self):
        # a genuinely slender 50mm-diameter, 6m flagpole (~120:1 aspect,
        # steeper than the banner-bracket pin) must not be confused with
        # the WHY repro's ~20mm hairline shaft.
        spec = {
            "asset_type": "custom", "name": "Flag", "units": "metric",
            "code_mode": "advisory", "parameters": [], "toggles": [],
            "materials": [{"slot": "pole", "preset": "galvanized_steel"}],
            "components": ["pole"], "connections": [],
            "primitives": [
                {"kind": "cylinder", "name": "mast", "component": "pole",
                 "material_slot": "pole", "location": [0, 0, 3.0],
                 "params": {"radius": 0.025, "depth": 6.0}},
            ],
        }
        findings = scale_findings_for(spec)
        assert not any(f["kind"] == "sliver_member" for f in findings)

    def test_hardware_and_cut_prims_never_contribute(self):
        # a hairline box declared "cut" (negative space) and a hairline
        # cylinder declared component "hardware" would both be flagged on
        # their raw geometry alone — neither may contribute.
        spec = _victorian_post_spec()
        spec["primitives"].append({
            "kind": "box", "name": "phantom_wire", "component": "shaft",
            "material_slot": "post", "cut": True,
            "location": [0, 0, 2.285], "params": {"size": [0.01, 0.01, 4.57]},
        })
        spec["primitives"].append({
            "kind": "cylinder", "name": "ghost_rod", "component": "hardware",
            "material_slot": "post",
            "location": [0, 0, 2.285], "params": {"radius": 0.01, "depth": 4.57},
        })
        findings = [f for f in scale_findings_for(spec) if f["kind"] == "sliver_member"]
        # only the ORIGINAL shaft trips it, not the cut/hardware copies
        assert len(findings) == 1
        assert findings[0]["component"] == "shaft"

    def test_segmented_post_is_flagged_at_component_level(self):
        # the segmentation dodge: no single 0.6 m segment trips the
        # per-primitive check, but the 'post' component's own stacked-
        # segment envelope (0-4.8 m, 16mm cross-section) reads as exactly
        # the same hairline rod.
        spec = _segmented_post_spec()
        findings = scale_findings_for(spec)
        slivers = [f for f in findings if f["kind"] == "sliver_member"]
        assert len(slivers) == 1
        finding = slivers[0]
        assert finding["component"] == "post"
        assert finding["severity"] == "warning"
        assert "4.80 m" in finding["message"]
        assert "0.016" in finding["message"]
        assert "several segments" in finding["message"]
        # the finial and base plate are not flagged — only the wire-thin
        # stack of segments is
        assert not any(f["component"] == "base" for f in findings)
        assert not any(f["component"] == "finial" for f in findings)
        # no other scale finding fires alongside it
        assert findings == slivers

    def test_single_prim_wire_is_not_double_counted(self):
        # dedupe: the victorian post's 'shaft' component is built from a
        # SINGLE primitive, so its own box and its component's union
        # envelope are identical — the component-level pass must not also
        # emit a second finding for the same member.
        spec = _victorian_post_spec()
        findings = scale_findings_for(spec)
        slivers = [f for f in findings if f["kind"] == "sliver_member"]
        assert len(slivers) == 1
        assert slivers[0]["component"] == "shaft"

    def test_wide_arc_of_short_segments_is_not_flagged(self):
        # a hoop/arch built from short, thin segments distributed along a
        # WIDE curve: neither the per-primitive check (no segment is a rod
        # on its own) nor the new component-level check (the component's
        # own envelope is wide, not a rod) may flag it.
        findings = scale_findings_for(_wide_arc_spec())
        assert not any(f["kind"] == "sliver_member" for f in findings)

    def test_never_raises_on_valid_primitives(self):
        spec = _victorian_post_spec()
        findings = check_scale_sanity(compute_primitives(spec), spec)
        assert isinstance(findings, list)

    def test_never_raises_on_segmented_or_arc_primitives(self):
        for spec in (_segmented_post_spec(), _wide_arc_spec()):
            findings = check_scale_sanity(compute_primitives(spec), spec)
            assert isinstance(findings, list)

    def test_neutered_check_would_fail_this_suite(self):
        # guard against a no-op regression: the repro above must yield a
        # non-empty result.
        assert scale_findings_for(_victorian_post_spec()) != []

    def test_neutered_component_check_would_fail_this_suite(self):
        # guard against a no-op regression on the component-level rule
        # specifically: the segmentation repro above must yield a
        # non-empty result.
        assert scale_findings_for(_segmented_post_spec()) != []


def _pergola_with_lantern():
    """The WHY repro for check_embedded_parts: a 'lantern' box component
    centered INSIDE pergola.json's front beam (beams component), so ~100%
    of its volume overlaps the beam instead of sitting on a face. Sized
    1.0 x 0.06 x 0.15 m — comfortably above SCALE_OUTLIER_RATIO's
    asset_max/10 cutoff (~0.38 m here) so it does NOT also trip
    check_scale_sanity's scale_outlier on size alone, isolating the defect
    this check exists to catch (embedding/position) from a different
    signal (size). This is THE gap check_buildability/check_scale_sanity/
    audit's buried-hardware check all miss: the lantern is fully supported
    (buried inside a beam is about as "grounded" as it gets by AABB
    contact), it is not toy-scale, it declares no connection, and it is
    not a hardware prim — nothing existing catches "rammed through
    geometry" specifically."""
    spec = load("pergola.json")
    spec["primitives"].append({
        "kind": "box", "name": "housing", "component": "lantern",
        "material_slot": "beams",
        # dead center of beam_front's own box: post_height + beam_depth/2 - 0.02
        "location": [0, "span_y/2", "post_height + beam_depth/2 - 0.02"],
        "params": {"size": [1.0, 0.06, 0.15]},
    })
    return spec


def _pergola_with_seated_cap():
    """A properly seated part: a small 'cap' component embedded 20mm (the
    repo's own fabrication-seating convention) into the OUTER face of the
    front-right post, well clear of the beam/rafter joint above it — must
    NOT be flagged."""
    spec = load("pergola.json")
    spec["primitives"].append({
        "kind": "box", "name": "sign", "component": "cap",
        "material_slot": "posts",
        # post's outer (+x) face is at span_x/2 + post_size/2; embed 20mm
        # into it by centering the cap's inner face 0.02m inside that face,
        # at mid-post height, far from the beam/rafter intersection
        "location": ["span_x/2 + post_size/2 + 0.03", "span_y/2",
                      "post_height/2"],
        "params": {"size": [0.1, 0.06, 0.1]},
    })
    return spec


class TestEmbeddedParts:
    def test_repro_gap_other_checks_stay_silent(self):
        # prove the gap FIRST: the lantern-in-beam repro sails through both
        # existing checks untouched.
        spec = _pergola_with_lantern()
        prims = compute_primitives(spec)
        assert buildability_errors(check_buildability(prims, spec)) == []
        assert check_scale_sanity(prims, spec) == []

    def test_lantern_inside_beam_is_flagged(self):
        spec = _pergola_with_lantern()
        findings = embedded_findings_for(spec)
        embedded = [f for f in findings if f["kind"] == "embedded_part"]
        assert len(embedded) == 1
        finding = embedded[0]
        assert finding["severity"] == "warning"
        assert finding["component"] == "lantern"
        assert "lantern" in finding["message"]
        assert "beams" in finding["message"]
        # fully enclosed -> ~100%
        assert "100%" in finding["message"] or "99%" in finding["message"]

    def test_properly_seated_part_is_not_flagged(self):
        spec = _pergola_with_seated_cap()
        findings = embedded_findings_for(spec)
        assert not any(f["component"] == "cap" for f in findings)
        assert findings == []

    @pytest.mark.parametrize("name", EXAMPLE_NAMES)
    def test_bundled_examples_are_embed_clean(self, name):
        # false-positive gate: every shipped example (including pergola/
        # park_bench, whose own joints rely on 10-20mm embeds) must produce
        # zero embedded_part findings.
        findings = embedded_findings_for(load(name))
        assert findings == [], (name, findings)

    def test_hardware_and_cut_prims_never_contribute(self):
        # connection_hardware is on for pergola.json, generating small
        # 'hardware'-component prims seated inside beams/posts by design; a
        # giant cut prim (negative space) is added on top. Neither should
        # be able to trigger (as component C) or feed (as an 'other' box)
        # an embedded_part finding.
        spec = load("pergola.json")
        spec["primitives"].append({
            "kind": "box", "name": "phantom_cut", "component": "posts",
            "material_slot": "posts", "cut": True,
            "location": [0, 0, 5], "params": {"size": [100, 100, 100]},
        })
        findings = embedded_findings_for(spec)
        assert findings == []

    def test_never_raises_on_valid_primitives(self):
        # single-component spec: nothing to embed into, must not raise and
        # must return no findings.
        spec = {
            "asset_type": "custom", "name": "Lonely", "units": "metric",
            "code_mode": "advisory", "parameters": [], "toggles": [],
            "materials": [{"slot": "frame", "preset": "wood_slat"}],
            "components": ["frame"], "connections": [],
            "primitives": [
                {"kind": "box", "name": "cube", "component": "frame",
                 "material_slot": "frame", "location": [0, 0, 0.05],
                 "params": {"size": [0.1, 0.1, 0.1]}},
            ],
        }
        findings = check_embedded_parts(compute_primitives(spec), spec)
        assert isinstance(findings, list)
        assert findings == []

    def test_threshold_constant_is_conservative(self):
        # sanity-check the frozen contract's named constant exists and sits
        # well above the fraction a legitimate seated joint reaches.
        assert EMBED_OVERLAP_RATIO == 0.6
        seated_findings = embedded_findings_for(_pergola_with_seated_cap())
        assert seated_findings == []

    def test_neutered_check_would_fail_this_suite(self):
        # guard against a no-op regression: the lantern repro above must
        # yield a non-empty result.
        spec = _pergola_with_lantern()
        assert embedded_findings_for(spec) != []


def _pergola_with_pierced_lantern():
    """Instructor's repro: a 'lantern' box sized [0.25, 0.25, 0.3] centered
    on the front beam pierces clean through it (poking out both sides in
    the beam's thin cross-section axes) while its self-volume-inside-beam
    fraction (~0.19) stays well under EMBED_OVERLAP_RATIO — the embed
    signature alone misses this, which is exactly why pierced_part
    exists."""
    spec = load("pergola.json")
    spec["primitives"].append({
        "kind": "box", "name": "housing", "component": "lantern",
        "material_slot": "beams",
        "location": [0, "span_y/2", "post_height + beam_depth/2 - 0.02"],
        "params": {"size": [0.25, 0.25, 0.3]},
    })
    return spec


class TestPiercedParts:
    def test_pierced_lantern_self_volume_fraction_stays_low(self):
        # prove the gap the instructor's finding describes: the embed
        # signature alone (fraction of C's own volume inside the host)
        # stays comfortably under EMBED_OVERLAP_RATIO for this repro, even
        # though the lantern visibly exits the beam on both sides.
        spec = _pergola_with_pierced_lantern()
        findings = embedded_findings_for(spec)
        embedded = [f for f in findings if f["kind"] == "embedded_part"]
        assert embedded == []

    def test_lantern_pierces_beam_is_flagged(self):
        spec = _pergola_with_pierced_lantern()
        findings = embedded_findings_for(spec)
        pierced = [f for f in findings if f["kind"] == "pierced_part"]
        assert len(pierced) == 1
        finding = pierced[0]
        assert finding["severity"] == "warning"
        assert finding["component"] == "lantern"
        assert "lantern" in finding["message"]
        assert "beams" in finding["message"]
        # both pierced axes named (the beam's thin cross-section: y, z)
        assert "y" in finding["message"] and "z" in finding["message"]

    def test_post_beam_joint_is_explicitly_clean(self):
        # the pergola's own 20mm-embed post<->beam joint: pierces only 1
        # host axis (ratio 1.0) in either direction, never 2, so it must
        # never trigger pierced_part regardless of how tight the fraction
        # is tuned.
        findings = embedded_findings_for(load("pergola.json"))
        assert [f for f in findings if f["kind"] == "pierced_part"] == []

    @pytest.mark.parametrize("name", EXAMPLE_NAMES)
    def test_bundled_examples_are_pierce_clean(self, name):
        # false-positive gate, re-confirmed for pierced_part specifically:
        # includes bike_rack.json (hoops/base) and street_light.json
        # (pole/arm) which round-trip through non-box AABBs (sweep, tube,
        # cone, lathe, loft) and would false-positive if the box-only
        # restriction in _box_prims_by_component were ever dropped.
        findings = embedded_findings_for(load(name))
        pierced = [f for f in findings if f["kind"] == "pierced_part"]
        assert pierced == [], (name, pierced)

    def test_never_raises_on_valid_primitives(self):
        spec = {
            "asset_type": "custom", "name": "Lonely", "units": "metric",
            "code_mode": "advisory", "parameters": [], "toggles": [],
            "materials": [{"slot": "frame", "preset": "wood_slat"}],
            "components": ["frame"], "connections": [],
            "primitives": [
                {"kind": "box", "name": "cube", "component": "frame",
                 "material_slot": "frame", "location": [0, 0, 0.05],
                 "params": {"size": [0.1, 0.1, 0.1]}},
            ],
        }
        findings = check_embedded_parts(compute_primitives(spec), spec)
        assert isinstance(findings, list)
        assert findings == []

    def test_threshold_constant_is_conservative(self):
        assert PIERCE_AXIS_FRACTION == 0.95

    def test_neutered_pierce_check_would_fail_this_suite(self):
        # guard against a no-op regression on the pierce branch
        # specifically: the repro above must yield a pierced_part finding.
        spec = _pergola_with_pierced_lantern()
        pierced = [f for f in embedded_findings_for(spec) if f["kind"] == "pierced_part"]
        assert pierced != []


# ---------------------------------------------------------------------------
# Dead-control detection (check_dead_controls)
# ---------------------------------------------------------------------------

def _street_light_with_invented_controls():
    """The WHY repro: an AI-generated street_light spec with a slider and a
    toggle that sound plausible but that street_light.py never reads."""
    spec = load("street_light.json")
    spec["parameters"].append(
        {"id": "lantern_height", "label": "Lantern Height", "type": "slider",
         "min": 0.1, "max": 1.0, "step": 0.05, "value": 0.4, "unit": "m"}
    )
    spec["toggles"].append(
        {"id": "solar_panel", "label": "Solar Panel", "value": True}
    )
    return spec


def _generic_widget_spec():
    """A minimal generic-path spec where each parameter is referenced by
    exactly ONE kind of expression site: visible_if, array.count (and its
    step), and a raw lathe profile point — plus a toggle referenced nowhere
    and the universal connection_hardware toggle."""
    return {
        "asset_type": "widget", "name": "W", "units": "metric",
        "parameters": [
            {"id": "gate_open", "label": "Gate Open", "type": "slider",
             "min": 0, "max": 1, "step": 1, "value": 1, "unit": "x"},
            {"id": "rung_count", "label": "Rung Count", "type": "slider",
             "min": 1, "max": 5, "step": 1, "value": 3, "unit": "x"},
            {"id": "vase_radius", "label": "Vase Radius", "type": "slider",
             "min": 0.01, "max": 0.5, "step": 0.01, "value": 0.1, "unit": "m"},
        ],
        "toggles": [
            {"id": "dark_green_finish", "label": "Dark Green Finish", "value": True},
            {"id": "connection_hardware", "label": "Connection Hardware", "value": True},
        ],
        "materials": [{"slot": "m", "preset": "galvanized_steel"}],
        "components": ["body"],
        "connections": [],
        "primitives": [
            {"kind": "box", "name": "gate", "component": "body", "material_slot": "m",
             "location": [0, 0, 0.5], "params": {"size": [0.2, 0.2, 1.0]},
             "visible_if": "gate_open"},
            {"kind": "box", "name": "rung", "component": "body", "material_slot": "m",
             "location": [0, 0, 0.1], "params": {"size": [0.3, 0.02, 0.02]},
             "array": {"count": "rung_count", "step": [0, 0, "0.1 * rung_count"]}},
            {"kind": "lathe", "name": "vase", "component": "body", "material_slot": "m",
             "location": [1, 0, 0], "params": {"profile": [
                 ["vase_radius", 0], ["vase_radius * 1.2", 0.5], [0.01, 1.0],
             ]}},
        ],
    }


class TestDeadControls:
    def test_untouched_street_light_example_has_no_dead_controls(self):
        assert check_dead_controls(load("street_light.json")) == []

    def test_invented_controls_on_curated_builder_are_flagged(self):
        findings = check_dead_controls(_street_light_with_invented_controls())
        assert len(findings) == 2
        assert all(f["severity"] == "error" for f in findings)
        messages = " ".join(f["message"] for f in findings)
        assert "lantern_height" in messages
        assert "solar_panel" in messages

    def test_generic_path_ids_referenced_only_via_visible_if_array_or_profile_are_clean(self):
        findings = check_dead_controls(_generic_widget_spec())
        # exactly one finding: the truly-unreferenced dark_green_finish
        # toggle. gate_open (visible_if), rung_count (array.count/step),
        # and vase_radius (lathe profile point) are all clean.
        assert len(findings) == 1
        assert "dark_green_finish" in findings[0]["message"]

    def test_connection_hardware_is_never_flagged(self):
        spec = _generic_widget_spec()
        for f in check_dead_controls(spec):
            assert "connection_hardware" not in f["message"]

    @pytest.mark.parametrize("name", EXAMPLE_NAMES)
    def test_bundled_examples_have_no_dead_controls(self, name):
        assert check_dead_controls(load(name)) == [], name

    def test_no_primitives_and_no_curated_builder_returns_empty(self):
        assert check_dead_controls({"asset_type": "unknown_thing", "parameters": [],
                                    "toggles": []}) == []

    def test_never_raises_on_malformed_input(self):
        assert check_dead_controls(None) == []
        assert check_dead_controls({}) == []


# ---------------------------------------------------------------------------
# Toggle feature-completeness (check_toggle_dependencies)
# ---------------------------------------------------------------------------

def _toggle_orphan_repro(bulb_gated: bool = False):
    """The WHY repro (round 6 brief 10): "sometimes when an option is added
    like 'double the arm' it doesn't double the light element on top of
    it." A post + an arm gated by toggle 'second_arm' + a light resting on
    the arm's tip. With `bulb_gated` False (the defect), the light shares
    NO visible_if with the arm it rests on: the spec passes every check at
    the toggle's own default (True, arm present, light supported), but
    switching the toggle off removes the arm and leaves the light floating
    with nothing under it — exactly the bug report. With `bulb_gated` True
    (the fix), the light shares the arm's visible_if, so the whole feature
    adds/removes together and nothing is ever orphaned."""
    bulb = {
        "kind": "sphere", "name": "bulb", "component": "light",
        "material_slot": "m", "location": [0.6, 0, 3.0],
        "params": {"radius": 0.08},
    }
    if bulb_gated:
        bulb["visible_if"] = "second_arm"
    return {
        "asset_type": "custom", "name": "ToggleOrphanRepro", "units": "metric",
        "code_mode": "advisory",
        "parameters": [],
        "toggles": [{"id": "second_arm", "label": "Second Arm", "value": True}],
        "materials": [{"slot": "m", "preset": "galvanized_steel"}],
        "components": ["post", "arm", "light"], "connections": [],
        "primitives": [
            {"kind": "cylinder", "name": "post", "component": "post",
             "material_slot": "m", "location": [0, 0, 1.5],
             "params": {"radius": 0.05, "depth": 3.0}},
            {"kind": "box", "name": "arm", "component": "arm",
             "material_slot": "m", "location": [0.3, 0, 3.0],
             "params": {"size": [0.6, 0.05, 0.05]},
             "visible_if": "second_arm"},
            bulb,
        ],
    }


class TestToggleDependencies:
    def test_ungated_dependent_part_is_flagged(self):
        # the broken repro: exactly one finding, naming both the orphaned
        # component ('light') and the toggle that orphans it ('second_arm').
        findings = check_toggle_dependencies(_toggle_orphan_repro(bulb_gated=False))
        assert len(findings) == 1
        finding = findings[0]
        assert finding["limit_type"] == "toggle_orphan"
        assert finding["severity"] == "error"
        assert "light" in finding["message"]
        assert "second_arm" in finding["message"]

    def test_co_gated_dependent_part_is_clean(self):
        # the corrected spec: the light shares the arm's visible_if, so
        # switching second_arm off removes both together — no orphan.
        findings = check_toggle_dependencies(_toggle_orphan_repro(bulb_gated=True))
        assert findings == []

    def test_baseline_floater_is_not_reattributed_to_a_toggle(self):
        # a part that is ALREADY floating at the spec's own default state
        # (a pre-existing buildability defect, unrelated to any toggle)
        # must never be reported here — that diagnosis belongs to
        # check_buildability, not this check. Moving the light far enough
        # away that it never touches the arm even at default proves the
        # diff-against-baseline design: the light is a floater in BOTH the
        # baseline and the second_arm-off variant, so it is not a NEW
        # floater and must not be flagged.
        spec = _toggle_orphan_repro(bulb_gated=False)
        for p in spec["primitives"]:
            if p["name"] == "bulb":
                p["location"] = [5.0, 0, 3.0]
        assert check_toggle_dependencies(spec) == []

    def test_connection_hardware_toggle_is_exempt(self):
        # connection_hardware is skipped entirely (never flipped off by
        # this check, exactly like check_dead_controls exempts it) — the
        # real second_arm orphan is still reported, but nothing ever names
        # connection_hardware as the culprit toggle.
        spec = _toggle_orphan_repro(bulb_gated=False)
        spec["toggles"].append(
            {"id": "connection_hardware", "label": "Connection Hardware", "value": True}
        )
        findings = check_toggle_dependencies(spec)
        assert len(findings) == 1
        assert "second_arm" in findings[0]["message"]
        assert "connection_hardware" not in findings[0]["message"]

    @pytest.mark.parametrize("name", EXAMPLE_NAMES)
    def test_bundled_examples_have_no_toggle_orphans(self, name):
        # false-positive gate: every shipped example's toggle groups are
        # already fully co-gated (park_bench's backrest/armrests gate every
        # post/slat of that feature together; street_light's double_arm
        # mirrors the arm AND its luminaire as one unit; bike_rack's
        # base_plates and planter's planting are self-contained) — a
        # neutered check that always returns [] would also pass this alone,
        # which is why the repro tests above assert a real finding on a
        # purpose-built reproduction.
        assert check_toggle_dependencies(load(name)) == [], name

    def test_no_toggles_returns_empty(self):
        assert check_toggle_dependencies(
            {"asset_type": "custom", "parameters": [], "toggles": []}
        ) == []

    def test_never_raises_on_malformed_input(self):
        assert check_toggle_dependencies(None) == []
        assert check_toggle_dependencies({}) == []
        assert check_toggle_dependencies({"toggles": "not-a-list"}) == []
        assert check_toggle_dependencies({"toggles": [1, 2, "bad"]}) == []

    def test_neutered_check_would_fail_this_suite(self):
        # guard against a no-op regression: the repro above must yield a
        # non-empty result.
        assert check_toggle_dependencies(_toggle_orphan_repro(bulb_gated=False)) != []
