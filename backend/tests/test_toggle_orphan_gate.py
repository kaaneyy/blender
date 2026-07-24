"""Tests for the toggle feature-completeness generation gate: a spec where
switching a single toggle off from its own default orphans a still-visible
part it was never co-gated with (the "double the arm" bug report — doubling
the arm doesn't double the light riding on it) is a classified generation
failure (kind="toggle_orphan"), lenient on the final attempt — the same
pattern buildability/dead-controls/scale already use (see test_retry.py /
test_dead_controls.py / test_scale_gate.py)."""
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from backend.app.spec_ai import SpecGenerationError, _postprocess  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID = (REPO_ROOT / "examples" / "street_light.json").read_text()


@pytest.fixture(autouse=True)
def mock_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")


def _toggle_orphan_repro(bulb_gated: bool) -> dict:
    """A post + an arm gated by toggle 'second_arm' + a light resting on
    the arm's tip. Sized so it is otherwise clean on every OTHER gate
    (buildability, dead-controls, scale) — isolating the toggle-orphan
    defect from unrelated findings that would otherwise raise first and
    mask the one this test targets. With `bulb_gated` False, the light
    shares NO visible_if with the arm it rests on: everything is grounded
    at the toggle's own default (arm present, light supported by it), but
    switching second_arm off removes the arm and leaves the light floating
    with nothing under it. With `bulb_gated` True, the light shares the
    arm's visible_if, so the whole feature adds/removes together."""
    bulb = {
        "kind": "sphere", "name": "bulb", "component": "light",
        "material_slot": "m", "location": [0.55, 0, 1.15],
        "params": {"radius": 0.05},
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
             "material_slot": "m", "location": [0, 0, 0.6],
             "params": {"radius": 0.03, "depth": 1.2}},
            {"kind": "box", "name": "arm", "component": "arm",
             "material_slot": "m", "location": [0.25, 0, 1.15],
             "params": {"size": [0.5, 0.02, 0.02]},
             "visible_if": "second_arm"},
            bulb,
        ],
    }


BROKEN = json.dumps(_toggle_orphan_repro(bulb_gated=False))
FIXED = json.dumps(_toggle_orphan_repro(bulb_gated=True))


class TestToggleOrphanGate:
    def test_ungated_dependent_part_raises_toggle_orphan(self):
        with pytest.raises(SpecGenerationError) as err:
            _postprocess(BROKEN, "advisory")
        assert err.value.kind == "toggle_orphan"
        # names both the orphaned part and the toggle that orphans it
        assert "light" in str(err.value)
        assert "second_arm" in str(err.value)
        assert "light" in err.value.hint
        assert "second_arm" in err.value.hint
        # the remedy — co-gate with the same visible_if, or support it
        # independently — is named in the hint
        assert "visible_if" in err.value.hint.lower()

    def test_final_attempt_is_lenient_and_ships_with_violation(self):
        out = _postprocess(BROKEN, "advisory", lenient_buildability=True)
        assert out["ok"] is False
        findings = [v for v in out["violations"] if v.get("limit_type") == "toggle_orphan"]
        assert findings and "light" in findings[0]["message"]
        assert findings[0]["severity"] == "error"

    def test_co_gated_reply_never_raises_toggle_orphan(self):
        # the corrected spec: the light shares the arm's visible_if, so
        # switching second_arm off removes both together — no orphan, and
        # the spec sails through this gate untouched.
        out = _postprocess(FIXED, "advisory")
        assert out["spec"]["asset_type"] == "custom"
        assert not any(v.get("limit_type") == "toggle_orphan" for v in out["violations"])

    def test_clean_street_light_reply_never_raises_toggle_orphan(self):
        # sanity check against a false-positive gate: the untouched bundled
        # example (double_arm/banner_bracket both fully self-contained —
        # double_arm mirrors the arm AND its luminaire together) must sail
        # through unchanged.
        out = _postprocess(VALID, "strict")
        assert out["spec"]["asset_type"] == "street_light"
        assert not any(v.get("limit_type") == "toggle_orphan" for v in out["violations"])
