"""Builder tests at min/mid/max parameters (T7.1 style) — pure layer only,
no bpy required. Guards: finite geometry, correct overall dimensions,
component naming, toggle behavior. Updated for the Part B rebuild: swept
mast arm, lofted cobra head, lathed pole cap."""
import math

import pytest

import blender.builders  # noqa: F401  registers builders
from blender.builders.base import compute_primitives
from blender.builders.hardware import _aabb
from standards.validator import validate_spec

FT = 0.3048


def make_spec(pole_height=30, arm_length=8, mounting="flange", **toggle_overrides):
    toggles = {"double_arm": False, "banner_bracket": False}
    toggles.update(toggle_overrides)
    return {
        "asset_type": "street_light",
        "name": "TestLight",
        "units": "imperial",
        "code_mode": "strict",
        "parameters": [
            {"id": "pole_height", "label": "Pole Height", "type": "slider",
             "min": 20, "max": 40, "step": 0.5, "value": pole_height, "unit": "ft"},
            {"id": "arm_length", "label": "Arm Length", "type": "slider",
             "min": 4, "max": 15, "step": 0.5, "value": arm_length, "unit": "ft"},
            {"id": "mounting", "label": "Mounting", "type": "select",
             "value": mounting, "options": ["flange", "burial", "embedded"]},
        ],
        "toggles": [{"id": k, "label": k, "value": v} for k, v in toggles.items()],
        "materials": [{"slot": "pole", "preset": "galvanized_steel"}],
        "seed": 42,
    }


def _flat(value):
    if isinstance(value, (tuple, list)):
        for v in value:
            yield from _flat(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _flat(v)
    elif isinstance(value, (int, float)):
        yield float(value)


def top_z(prim):
    center, half = _aabb(prim)
    return center[2] + half[2]


@pytest.mark.parametrize("pole_height,arm_length", [
    (20, 4),   # code minimums
    (30, 8),   # typical
    (40, 15),  # code maximums
])
def test_geometry_is_finite_at_param_extremes(pole_height, arm_length):
    prims = compute_primitives(make_spec(pole_height, arm_length))
    assert prims
    for p in prims:
        values = list(p.location) + list(p.rotation) + list(_flat(p.params))
        assert all(math.isfinite(v) for v in values), f"non-finite value in {p.name}"


@pytest.mark.parametrize("pole_height", [20, 30, 40])
def test_pole_height_matches_spec(pole_height):
    prims = compute_primitives(make_spec(pole_height=pole_height))
    shaft = next(p for p in prims if p.name == "shaft")
    assert shaft.params["depth"] == pytest.approx(pole_height * FT)
    assert top_z(shaft) == pytest.approx(pole_height * FT)
    # nothing except the pole cap / luminaire pokes far above the pole
    assert max(top_z(p) for p in prims) < pole_height * FT + 0.5


@pytest.mark.parametrize("arm_length", [4, 8, 15])
def test_arm_is_a_tapered_sweep_reaching_arm_length(arm_length):
    prims = compute_primitives(make_spec(arm_length=arm_length))
    [arm] = [p for p in prims if p.component == "arm" and p.kind == "sweep"]
    # the swept path spans from the pole face to the full arm length
    xs = [pt[0] for pt in arm.params["path"]]
    assert min(xs) == pytest.approx(0.0)
    assert max(xs) == pytest.approx(arm_length * FT)
    # real mast arms taper toward the tip
    assert arm.params["radius_end"] < arm.params["radius"]
    # cobra head is a loft centered near the arm tip
    head = next(p for p in prims if p.name == "head")
    assert head.kind == "loft"
    assert head.location[0] == pytest.approx(arm_length * FT, abs=0.5)
    assert head.params["profile_start"]["shape"] == "rect"
    assert head.params["profile_end"]["shape"] == "ellipse"
    assert head.params["shell"] == pytest.approx(0.003)  # hollow housing (A4)


def test_pole_cap_is_a_lathe_dome():
    prims = compute_primitives(make_spec())
    cap = next(p for p in prims if p.name == "cap")
    assert cap.kind == "lathe"
    assert cap.params["profile"] == "dome"


def test_component_naming_convention():
    prims = compute_primitives(make_spec())
    components = {p.component for p in prims}
    assert {"base_plate", "pole", "arm", "luminaire"} <= components
    names = [p.name for p in prims]
    assert len(names) == len(set(names)), "primitive names must be unique"


def test_double_arm_toggle_mirrors_arm_and_luminaire():
    single = compute_primitives(make_spec())
    double = compute_primitives(make_spec(double_arm=True))
    arm_and_head = [p for p in single if p.component in ("arm", "luminaire")]
    assert len(double) == len(single) + len(arm_and_head)
    mirrored = [p for p in double if p.name.endswith("_b")]
    assert len(mirrored) == len(arm_and_head)
    # the mirrored sweep's path runs toward -X (paths carry their own coords)
    mirrored_arm = next(p for p in mirrored if p.kind == "sweep")
    original_arm = next(p for p in double if p.name == "mast_arm")
    assert min(pt[0] for pt in mirrored_arm.params["path"]) == pytest.approx(
        -max(pt[0] for pt in original_arm.params["path"])
    )
    # mirrored head sits at negated X
    m_head = next(p for p in mirrored if p.name == "head_b")
    o_head = next(p for p in double if p.name == "head")
    assert m_head.location[0] == pytest.approx(-o_head.location[0])


def test_banner_toggle():
    with_banner = compute_primitives(make_spec(banner_bracket=True))
    brackets = [p for p in with_banner if p.component == "banner_bracket"]
    assert len(brackets) == 2
    assert all(top_z(b) < 30 * FT for b in brackets)


class TestGroundConnection:
    """C1/C7: the pole meets the ground like an engineered installation."""

    def test_flange_mount_details(self):
        prims = {p.name: p for p in compute_primitives(make_spec())
                 if p.component == "base_plate"}
        # grout pad + round flange + weld bead (C3)
        assert {"grout_pad", "flange", "weld_bead"} <= set(prims)
        # anchor-bolt circle: 4 bolts with washers and hex nuts on a BCD
        bolts = [p for n, p in prims.items() if n.startswith("anchor_bolt_")]
        nuts = [p for n, p in prims.items() if n.startswith("anchor_nut_")]
        washers = [p for n, p in prims.items() if n.startswith("anchor_washer_")]
        assert len(bolts) == len(nuts) == len(washers) == 4
        assert all(p.params.get("segments") == 6 for p in nuts)
        # bolts sit ON a circle between the pole and the flange edge
        flange_r = prims["flange"].params["radius"]
        pole_r = (8 * 0.0254) / 2
        for b in bolts:
            r = math.hypot(b.location[0], b.location[1])
            assert pole_r < r < flange_r
        # gusset webs between the bolts, tall at the pole, thin at the rim
        gussets = [p for n, p in prims.items() if n.startswith("gusset_")]
        assert len(gussets) == 4
        assert all(g.kind == "loft" for g in gussets)
        g = gussets[0]
        assert g.params["profile_start"]["w"] > g.params["profile_end"]["w"]

    def test_burial_and_embedded_variants(self):
        burial = [p for p in compute_primitives(make_spec(mounting="burial"))
                  if p.component == "base_plate"]
        assert [p.name for p in burial] == ["backfill_collar"]
        assert burial[0].kind == "lathe" and burial[0].params["profile"] == "flared_base"

        embedded = {p.name for p in compute_primitives(make_spec(mounting="embedded"))
                    if p.component == "base_plate"}
        assert "concrete_pier" in embedded
        # no anchor bolts in either non-flange variant
        assert not any(n.startswith("anchor_bolt") for n in embedded)


class TestArmConnection:
    """C2/C4: the mast arm mounts with a slip-fitter and a gusset."""

    def test_slipfitter_wraps_pole_at_attach_height(self):
        prims = {p.name: p for p in compute_primitives(make_spec())}
        collar = prims["slipfitter"]
        assert collar.kind == "tube"
        pole_top_r = (4 * 0.0254) / 2
        pole_base_r = (8 * 0.0254) / 2
        assert pole_top_r < collar.params["radius"] < pole_base_r + 0.02
        # near the top of the pole where the arm attaches
        assert 30 * FT - 1.5 < collar.location[2] < 30 * FT

    def test_arm_gusset_under_cantilever(self):
        prims = {p.name: p for p in compute_primitives(make_spec())}
        gusset = prims["arm_gusset"]
        assert gusset.kind == "loft"
        assert gusset.params["profile_start"]["w"] > gusset.params["profile_end"]["w"]


def test_unknown_asset_type_raises():
    with pytest.raises(ValueError, match="No builder"):
        compute_primitives({"asset_type": "warp_core", "parameters": []})


def test_validated_spec_builds_after_clamping():
    """End-to-end: out-of-code spec -> validator clamps -> builder consumes."""
    spec = make_spec(pole_height=90)  # way above the 40 ft max
    result = validate_spec(spec)
    assert not result.ok
    prims = compute_primitives(result.spec)
    shaft = next(p for p in prims if p.name == "shaft")
    assert shaft.params["depth"] == pytest.approx(40 * FT)
