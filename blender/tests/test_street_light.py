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
    [arm] = [p for p in prims if p.component == "arm"]
    assert arm.kind == "sweep"
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
    m_head = next(p for p in mirrored if p.kind == "loft")
    o_head = next(p for p in double if p.name == "head")
    assert m_head.location[0] == pytest.approx(-o_head.location[0])


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
