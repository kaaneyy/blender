"""Loft profile-shape synonym normalization.

The loft cross-section vocabulary that the schema enum and
``shapes.ring_points`` share is exactly {"rect", "ellipse"}. But the model
naturally writes "circle" for a round section — the observed failure was
"Generation failed after 3 attempts. Last error: Schema violation at
primitives/2/params/profile_start/shape: 'circle' is not one of
['rect','ellipse']".

``_normalize_profile_shapes`` maps that whole class of natural synonyms
(circle/round/oval/square/rectangle/…, case-insensitive) to the canonical
value BEFORE schema validation, so a reasonable word neither fails an
otherwise-good spec NOR — since ``ring_points`` treats any non-ellipse shape
as a softly-rounded rect — silently builds a square-ish section where a round
one was meant. An unrecognized shape is still left for the schema to reject.
"""
import json

import pytest

pytest.importorskip("jsonschema")

from backend.app.spec_ai import (  # noqa: E402
    SpecGenerationError,
    _normalize_profile_shapes,
    _postprocess,
)


def _loft_spec(start_shape, end_shape):
    """A minimal, grounded, buildable single-loft AssetSpec whose two profile
    shapes are the test's variables."""
    return {
        "asset_type": "test_widget",
        "name": "Loft Shape Test",
        "units": "metric",
        "parameters": [
            {"id": "h", "label": "Height", "type": "slider", "value": 1.0, "unit": "m"},
        ],
        "primitives": [
            {"kind": "loft", "name": "body", "component": "body",
             "material_slot": "body", "location": [0, 0, "h/2"],
             "params": {"depth": "h",
                        "profile_start": {"shape": start_shape, "w": 0.3, "h": 0.25},
                        "profile_end": {"shape": end_shape, "w": 0.2, "h": 0.18}}},
        ],
    }


def _shapes(spec):
    p = spec["primitives"][0]["params"]
    return p["profile_start"]["shape"], p["profile_end"]["shape"]


class TestNormalizeUnit:
    def test_circle_maps_to_ellipse(self):
        spec = _loft_spec("circle", "circle")
        _normalize_profile_shapes(spec)
        assert _shapes(spec) == ("ellipse", "ellipse")

    def test_round_oval_square_rectangle_box(self):
        cases = {"round": "ellipse", "oval": "ellipse", "circular": "ellipse",
                 "square": "rect", "rectangle": "rect", "box": "rect"}
        for word, canonical in cases.items():
            spec = _loft_spec(word, word)
            _normalize_profile_shapes(spec)
            assert _shapes(spec) == (canonical, canonical), word

    def test_canonical_values_pass_through(self):
        spec = _loft_spec("ellipse", "rect")
        _normalize_profile_shapes(spec)
        assert _shapes(spec) == ("ellipse", "rect")

    def test_case_and_whitespace_insensitive(self):
        # the schema enum is case-sensitive, so "Ellipse"/" RECT " would fail
        # validation untouched — normalization folds them to the enum value.
        spec = _loft_spec("  Ellipse ", "RECT")
        _normalize_profile_shapes(spec)
        assert _shapes(spec) == ("ellipse", "rect")

    def test_unknown_shape_left_untouched(self):
        spec = _loft_spec("hexagon", "ellipse")
        _normalize_profile_shapes(spec)
        # left for the schema to reject, not silently rewritten to something
        assert _shapes(spec) == ("hexagon", "ellipse")

    def test_malformed_input_never_raises(self):
        for bad in (
            {}, {"primitives": "nope"}, {"primitives": [None, 3, "x"]},
            {"primitives": [{}]}, {"primitives": [{"params": None}]},
            {"primitives": [{"params": {"profile_start": "x"}}]},
            {"primitives": [{"params": {"profile_start": {"shape": 5}}}]},
            {"primitives": [{"params": {"profile_start": {}}}]},
        ):
            _normalize_profile_shapes(bad)  # must not raise


class TestPostprocessIntegration:
    def test_circle_loft_no_longer_fails_schema(self):
        # advisory + lenient sidesteps unrelated buildability/scale RAISES;
        # JSON-Schema validation and the geometry build still run — so this
        # proves "circle" is now accepted end-to-end where it used to fail
        # after MAX_ATTEMPTS.
        out = _postprocess(json.dumps(_loft_spec("circle", "circle")),
                           "advisory", lenient_buildability=True)
        assert _shapes(out["spec"]) == ("ellipse", "ellipse")

    def test_unknown_shape_still_rejected_by_schema(self):
        # normalization is targeted, not a blanket bypass: a genuinely
        # unknown shape must still be a classified schema failure.
        with pytest.raises(SpecGenerationError) as exc:
            _postprocess(json.dumps(_loft_spec("hexagon", "ellipse")),
                         "advisory", lenient_buildability=True)
        assert exc.value.kind == "schema"
