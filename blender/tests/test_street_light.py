"""Builder tests at min/mid/max parameters (T7.1 style) — pure layer only,
no bpy required. Guards: finite geometry, correct overall dimensions,
component naming, toggle behavior."""
import math

import pytest

import blender.builders  # noqa: F401  registers builders
from blender.builders.base import PRIMITIVE_KINDS, compute_primitives
from standards.validator import validate_spec

FT = 0.3048


def make_spec(pole_height=30, arm_length=8, **toggle_overrides):
    toggles = {"double_arm": False, "banner_bracket": False, "anchor_bolts": True}
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
        ],
        "toggles": [{"id": k, "label": k, "value": v} for k, v in toggles.items()],
        "materials": [{"slot": "pole", "preset": "galvanized_steel"}],
        "seed": 42,
    }


def top_z(prim):
    """Highest Z any part of an axis-aligned-ish primitive can reach."""
    z = prim.location[2]
    if prim.kind in ("cylinder", "cone"):
        return z + prim.params["depth"] / 2
    if prim.kind == "box":
        return z + prim.params["size"][2] / 2
    return z + prim.params["radius"]


@pytest.mark.parametrize("pole_height,arm_length", [
    (20, 4),   # code minimums
    (30, 8),   # typical
    (40, 15),  # code maximums
])
def test_geometry_is_finite_at_param_extremes(pole_height, arm_length):
    prims = compute_primitives(make_spec(pole_height, arm_length))
    assert prims
    for p in prims:
        values = list(p.location) + list(p.rotation)
        for key in PRIMITIVE_KINDS[p.kind]:
            v = p.params[key]
            values.extend(v if isinstance(v, (tuple, list)) else [v])
        assert all(math.isfinite(v) for v in values), f"non-finite value in {p.name}"
        # every dimension must be strictly positive
        for key in PRIMITIVE_KINDS[p.kind]:
            v = p.params[key]
            dims = v if isinstance(v, (tuple, list)) else [v]
            assert all(d > 0 for d in dims), f"non-positive dim in {p.name}"


@pytest.mark.parametrize("pole_height", [20, 30, 40])
def test_pole_height_matches_spec(pole_height):
    prims = compute_primitives(make_spec(pole_height=pole_height))
    shaft = next(p for p in prims if p.name == "shaft")
    assert shaft.params["depth"] == pytest.approx(pole_height * FT)
    assert top_z(shaft) == pytest.approx(pole_height * FT)
    # nothing except the pole cap / luminaire pokes far above the pole
    assert max(top_z(p) for p in prims) < pole_height * FT + 0.5


@pytest.mark.parametrize("arm_length", [4, 8, 15])
def test_arm_reach_tracks_arm_length(arm_length):
    prims = compute_primitives(make_spec(arm_length=arm_length))
    head = next(p for p in prims if p.name == "head")
    # luminaire head is centered near the arm tip
    assert head.location[0] == pytest.approx(arm_length * FT, abs=0.5)
    arm_segs = [p for p in prims if p.component == "arm"]
    assert len(arm_segs) == 6
    max_reach = max(p.location[0] for p in arm_segs)
    assert max_reach < arm_length * FT <= max_reach + 1.0


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
    # mirrored copies sit at negated X
    for m in mirrored:
        original = next(p for p in double if p.name == m.name[:-2])
        assert m.location[0] == pytest.approx(-original.location[0])
        assert m.location[2] == pytest.approx(original.location[2])


def test_anchor_bolt_and_banner_toggles():
    base_count = len(compute_primitives(make_spec()))
    without_bolts = compute_primitives(make_spec(anchor_bolts=False))
    assert len(without_bolts) == base_count - 4
    with_banner = compute_primitives(make_spec(banner_bracket=True))
    brackets = [p for p in with_banner if p.component == "banner_bracket"]
    assert len(brackets) == 2
    assert all(top_z(b) < 30 * FT for b in brackets)


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
