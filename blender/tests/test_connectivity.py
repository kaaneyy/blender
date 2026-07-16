"""Tests for the buildability / load-path validator (connectivity.py):
contact graph, floating-part detection with nearest support + gap, below-
grade geometry, and dead declared connections; the deterministic
scale-sanity checker (check_scale_sanity); and the deterministic embedded-
part detector (check_embedded_parts)."""
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
    check_embedded_parts,
    check_scale_sanity,
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
