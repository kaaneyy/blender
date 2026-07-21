"""Accessible table builder tests (pure primitive layer only, no bpy
required). Guards: finite geometry, surface-height fidelity within the ADA
28-34 in DB range, ADA knee/toe clearance open BY CONSTRUCTION on the
accessible side (at default params AND when narrowed), the thin-top/raised-
top resolution for tight surface_height vs. knee_clearance_height
combinations, the self-emitted leg-to-top weld joints, and component
naming. Mirrors the conventions of test_street_light.py."""
import math

import pytest

import blender.builders  # noqa: F401  registers builders
from blender.builders.accessible_table import (
    IN,
    KNEE_CLEARANCE_WIDTH,
    MIN_TOP_THICKNESS,
    PENETRATION,
    TOP_THICKNESS_DEFAULT,
)
from blender.builders.base import compute_primitives
from blender.builders.hardware import _aabb
from standards.validator import validate_spec


def make_spec(surface_height=30, knee_clearance_height=27, toe_clearance_depth=17,
              table_width=60, table_depth=30):
    return {
        "asset_type": "accessible_table",
        "name": "TestAccessibleTable",
        "units": "imperial",
        "code_mode": "strict",
        "parameters": [
            {"id": "surface_height", "label": "Surface Height", "type": "slider",
             "min": 28, "max": 34, "step": 0.5, "value": surface_height, "unit": "in"},
            {"id": "knee_clearance_height", "label": "Knee Clearance Height", "type": "slider",
             "min": 27, "max": 40, "step": 0.5, "value": knee_clearance_height, "unit": "in"},
            {"id": "toe_clearance_depth", "label": "Toe Clearance Depth", "type": "slider",
             "min": 17, "max": 30, "step": 0.5, "value": toe_clearance_depth, "unit": "in"},
            {"id": "table_width", "label": "Table Width", "type": "slider",
             "min": 20, "max": 96, "step": 1, "value": table_width, "unit": "in"},
            {"id": "table_depth", "label": "Table Depth", "type": "slider",
             "min": 24, "max": 48, "step": 1, "value": table_depth, "unit": "in"},
        ],
        "toggles": [],
        "materials": [{"slot": "top", "preset": "wood_slat"},
                      {"slot": "frame", "preset": "galvanized_steel"}],
        "seed": 7,
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


def _top_z(prim):
    center, half = _aabb(prim)
    return center[2] + half[2]


def _bottom_z(prim):
    center, half = _aabb(prim)
    return center[2] - half[2]


def _aabb_overlaps_box(prim, box_center, box_half, eps=1e-6):
    """True if prim's world AABB truly overlaps the axis-aligned box (a
    shared boundary face counts as separated, not overlapping)."""
    center, half = _aabb(prim)
    for k in range(3):
        lo_p, hi_p = center[k] - half[k], center[k] + half[k]
        lo_b, hi_b = box_center[k] - box_half[k], box_center[k] + box_half[k]
        if hi_p <= lo_b + eps or lo_p >= hi_b - eps:
            return False
    return True


@pytest.mark.parametrize(
    "surface_height,knee_clearance_height,toe_clearance_depth",
    [
        (28, 27, 17),  # DB minimums
        (30, 27, 17),  # DB default
        (34, 27, 17),  # DB maximum
        (30, 32, 17),  # knee clearance raised above the default surface height
    ],
)
def test_geometry_is_finite_at_param_extremes(surface_height, knee_clearance_height,
                                               toe_clearance_depth):
    prims = compute_primitives(make_spec(surface_height, knee_clearance_height,
                                          toe_clearance_depth))
    assert prims
    for p in prims:
        values = list(p.location) + list(p.rotation) + list(_flat(p.params))
        assert all(math.isfinite(v) for v in values), f"non-finite value in {p.name}"


@pytest.mark.parametrize("surface_height_in", [28, 30, 34])
def test_surface_height_matches_spec_across_db_range(surface_height_in):
    prims = compute_primitives(make_spec(surface_height=surface_height_in))
    top = next(p for p in prims if p.name == "top")
    top_surface = _top_z(top)
    assert top_surface == pytest.approx(surface_height_in * IN)
    assert 28 * IN - 1e-9 <= top_surface <= 34 * IN + 1e-9


def test_component_naming_convention():
    prims = compute_primitives(make_spec())
    components = {p.component for p in prims}
    assert {"top", "legs", "apron"} <= components
    names = [p.name for p in prims]
    assert len(names) == len(set(names)), "primitive names must be unique"


class TestTopThicknessResolution:
    """The DEFAULT-vs-MIN tension between a thin top and a low
    surface_height is resolved deterministically: thin the top first (down
    to MIN_TOP_THICKNESS); only once that bottoms out does the effective
    surface height rise to protect the knee clearance floor."""

    def test_thin_top_at_min_surface_height(self):
        # 28 in surface, 27 in knee minimum -> only 1 in of headroom, well
        # under TOP_THICKNESS_DEFAULT (1.5 in) but above MIN_TOP_THICKNESS
        prims = compute_primitives(make_spec(surface_height=28, knee_clearance_height=27))
        top = next(p for p in prims if p.name == "top")
        thickness = top.params["size"][2]
        gap = (28 - 27) * IN
        assert thickness == pytest.approx(gap)
        assert thickness < TOP_THICKNESS_DEFAULT
        assert thickness >= MIN_TOP_THICKNESS - 1e-9

    def test_default_thickness_when_headroom_is_ample(self):
        prims = compute_primitives(make_spec(surface_height=34, knee_clearance_height=27))
        top = next(p for p in prims if p.name == "top")
        assert top.params["size"][2] == pytest.approx(TOP_THICKNESS_DEFAULT)

    def test_raising_knee_clearance_height_raises_the_top(self):
        base = compute_primitives(make_spec())
        raised = compute_primitives(make_spec(knee_clearance_height=32))
        base_top = next(p for p in base if p.name == "top")
        raised_top = next(p for p in raised if p.name == "top")
        raised_underside = _bottom_z(raised_top)
        # underside always clears the requested knee floor
        assert raised_underside >= 32 * IN - 1e-9
        # the top rose (or at minimum did not drop) to make room
        assert _top_z(raised_top) >= _top_z(base_top)
        # top thickness never dips below the floor
        assert raised_top.params["size"][2] >= MIN_TOP_THICKNESS - 1e-9

    def test_top_underside_never_below_knee_clearance_height(self):
        for surface_height, knee in [(28, 27), (30, 27), (34, 27), (30, 33), (28, 30)]:
            prims = compute_primitives(
                make_spec(surface_height=surface_height, knee_clearance_height=knee)
            )
            top = next(p for p in prims if p.name == "top")
            assert _bottom_z(top) >= knee * IN - 1e-9


class TestLegJoints:
    """Legs are round members that interpenetrate the top and each get
    their own welded joint at the top's underside (C3 via weld_fillet),
    mirroring how street_light emits its own ground_connection."""

    def test_one_weld_joint_per_leg_at_top_underside(self):
        prims = compute_primitives(make_spec())
        legs = [p for p in prims if p.component == "legs" and p.kind == "cylinder"]
        welds = [p for p in prims if p.name.endswith("_weld_bead")]
        assert len(legs) == len(welds) == 2
        top = next(p for p in prims if p.name == "top")
        top_underside = _bottom_z(top)
        for leg in legs:
            weld = next(w for w in welds if w.name == f"{leg.name}_weld_bead")
            assert weld.kind == "lathe"
            assert weld.location[0] == pytest.approx(leg.location[0])
            assert weld.location[1] == pytest.approx(leg.location[1])
            assert weld.location[2] == pytest.approx(top_underside, abs=1e-6)

    def test_legs_interpenetrate_the_top(self):
        prims = compute_primitives(make_spec())
        top = next(p for p in prims if p.name == "top")
        top_underside = _bottom_z(top)
        legs = [p for p in prims if p.component == "legs" and p.kind == "cylinder"]
        assert legs
        for leg in legs:
            leg_top = _top_z(leg)
            overlap = leg_top - top_underside
            assert 0.008 <= overlap <= 0.025, (
                f"{leg.name} overlaps the top by {overlap * 1000:.1f} mm, "
                "expected a real ~10-20 mm fabrication joint"
            )
            assert overlap == pytest.approx(PENETRATION)


class TestKneeToeClearance:
    """ADA-306 knee/toe clearance under the accessible (front, -Y) edge is
    open BY CONSTRUCTION: no structural primitive (leg, apron, joint
    detail) may overlap the clear-floor box, at default params AND when
    table_width is narrowed — proving the builder enforces this rather than
    merely permitting it at one convenient width."""

    @staticmethod
    def _clear_box(prims, table_depth_in, knee_clearance_height_in, toe_clearance_depth_in):
        top = next(p for p in prims if p.name == "top")
        top_underside = _bottom_z(top)
        depth_m = table_depth_in * IN
        knee_h = knee_clearance_height_in * IN
        toe_d = toe_clearance_depth_in * IN
        front_edge_y = -depth_m / 2
        z_top = min(knee_h, top_underside)
        box_half = (KNEE_CLEARANCE_WIDTH / 2, toe_d / 2, z_top / 2)
        box_center = (0.0, front_edge_y + toe_d / 2, z_top / 2)
        return box_center, box_half

    @pytest.mark.parametrize("table_width_in", [60, 24, 90])
    def test_no_structural_primitive_overlaps_the_clear_zone(self, table_width_in):
        spec = make_spec(table_width=table_width_in)
        prims = compute_primitives(spec)
        box_center, box_half = self._clear_box(prims, 30, 27, 17)
        for p in prims:
            assert not _aabb_overlaps_box(p, box_center, box_half), (
                f"{p.name} ({p.component}) intrudes on the ADA knee/toe "
                f"clearance zone at table_width={table_width_in} in"
            )

    def test_narrowed_table_still_clears(self):
        """A width narrower than the ADA knee-clearance zone itself doesn't
        break the guarantee — the clearance comes from the legs' Y set-back,
        not from keeping them apart in X."""
        prims = compute_primitives(make_spec(table_width=20))
        box_center, box_half = self._clear_box(prims, 30, 27, 17)
        structural = [p for p in prims if p.component in ("legs", "apron")]
        assert structural
        for p in structural:
            assert not _aabb_overlaps_box(p, box_center, box_half)


def test_validated_spec_clamps_out_of_code_surface_height():
    """End-to-end: out-of-code spec -> validator clamps -> builder consumes."""
    spec = make_spec(surface_height=50)  # above the 34 in DB max
    result = validate_spec(spec)
    assert not result.ok
    prims = compute_primitives(result.spec)
    top = next(p for p in prims if p.name == "top")
    assert _top_z(top) == pytest.approx(34 * IN)


def test_unknown_asset_type_still_raises():
    with pytest.raises(ValueError, match="No builder"):
        compute_primitives({"asset_type": "warp_core", "parameters": []})
