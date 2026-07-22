"""Tests for the scale-aware generation gate: ``_postprocess`` runs the
sibling ``blender.builders.connectivity.check_scale_sanity`` check after
buildability, gates non-final attempts on it with a targeted "scale" hint,
is lenient on the final attempt (same as buildability), and never crashes
if that sibling module hasn't landed ``check_scale_sanity`` yet (it is
being built independently in a separate worktree and does not exist in
this tree). ``_gather_findings`` (the Improve flow) picks up the same
findings."""
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from backend.app import spec_ai  # noqa: E402
from backend.app.spec_ai import (  # noqa: E402
    MAX_ATTEMPTS,
    SpecGenerationError,
    _gather_findings,
    _postprocess,
    refine_spec,
)
from blender.builders import connectivity  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID = (REPO_ROOT / "examples" / "street_light.json").read_text()


@pytest.fixture(autouse=True)
def mock_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")


def spec_dict():
    return json.loads(VALID)


def run_refine(message="make it taller"):
    return refine_spec(spec_dict(), message)


def script_complete(monkeypatch, replies):
    calls = []

    def fake_complete(system, user, **kwargs):
        calls.append(user)
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(spec_ai, "complete", fake_complete)
    return calls


SCALE_FINDING = {
    "severity": "warning",
    "kind": "scale_outlier",
    "message": "solar panel is 0.3m tall, expected roughly 1.65 x 1.0 x 0.04m",
    "component": "solar_panel",
}


class TestScaleGate:
    def test_scale_finding_triggers_retry_with_targeted_hint(self, monkeypatch):
        # first _postprocess call (attempt 1) reports a scale finding, the
        # second (the retry, attempt 2) reports none — proves the finding's
        # message and the "scale" kind ride the correction prompt/history.
        calls_check = {"n": 0}

        def fake_scale(prims, spec):
            calls_check["n"] += 1
            return [SCALE_FINDING] if calls_check["n"] == 1 else []

        monkeypatch.setattr(connectivity, "check_scale_sanity", fake_scale,
                            raising=False)
        calls = script_complete(monkeypatch, [VALID, VALID])
        out = run_refine()
        assert len(calls) == 2
        assert out["spec"]["asset_type"] == "street_light"
        assert SCALE_FINDING["message"] in calls[1]
        assert "[scale]" in calls[1]
        assert "real-world dimensions" in calls[1]

    def test_postprocess_raises_scale_kind_directly(self, monkeypatch):
        monkeypatch.setattr(connectivity, "check_scale_sanity",
                            lambda prims, spec: [SCALE_FINDING], raising=False)
        with pytest.raises(SpecGenerationError) as err:
            _postprocess(VALID, "strict")
        assert err.value.kind == "scale"
        assert SCALE_FINDING["message"] in err.value.hint
        assert "real-world dimensions" in err.value.hint

    def test_final_attempt_is_lenient_on_scale(self, monkeypatch):
        # every attempt reports the same scale finding — generation must
        # still succeed on the final (lenient) attempt instead of raising,
        # exactly like the buildability leniency it mirrors.
        monkeypatch.setattr(connectivity, "check_scale_sanity",
                            lambda prims, spec: [SCALE_FINDING], raising=False)
        calls = script_complete(monkeypatch, [VALID, VALID, VALID])
        out = run_refine()
        assert len(calls) == MAX_ATTEMPTS
        assert out["spec"]["asset_type"] == "street_light"
        assert any(v.get("kind") == "scale_outlier" for v in out["violations"])

    def test_missing_scale_checker_is_a_no_op(self, monkeypatch):
        # Simulate check_scale_sanity being absent from the sibling module
        # — regardless of whether this tree actually has it yet (it may or
        # may not, depending on whether that independently-evolving change
        # has landed) — so this test passes either way. delattr with
        # raising=False also tolerates the attribute genuinely not existing.
        # _postprocess must behave EXACTLY as it did before this feature: no
        # gating, no crash, no "scale" kind ever raised.
        monkeypatch.delattr(connectivity, "check_scale_sanity", raising=False)
        out = _postprocess(VALID, "strict")
        assert out["spec"]["asset_type"] == "street_light"
        assert not any(v.get("kind") == "scale_outlier"
                       for v in out.get("violations", []))

    def test_gather_findings_includes_scale_findings(self, monkeypatch):
        finding = {**SCALE_FINDING, "kind": "scale_giant",
                  "message": "luminaire head is 12m, expected 0.6-0.8m"}
        monkeypatch.setattr(connectivity, "check_scale_sanity",
                            lambda prims, spec: [finding], raising=False)
        findings = _gather_findings(spec_dict())
        assert any(f["kind"] == "scale_giant" and finding["message"] in f["message"]
                   for f in findings)

    def test_gather_findings_missing_checker_is_a_no_op(self, monkeypatch):
        monkeypatch.delattr(connectivity, "check_scale_sanity", raising=False)
        findings = _gather_findings(spec_dict())
        assert not any(f["kind"].startswith("scale") for f in findings)


#: The WHY repro (Brief 6): a "15 ft victorian post" generated on the
#: generic primitives path as a base plate, a hairline ~20mm-diameter/
#: 4.57m-tall cylinder shaft, and a small finial. Unlike the tests above,
#: this exercises the REAL check_scale_sanity (no monkeypatch) end-to-end
#: through _postprocess, proving the sliver_member finding it now emits
#: rides the exact same "scale" gate wiring the other tests here pin.
VICTORIAN_POST_REPLY = json.dumps({
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
})


class TestSliverMemberGate:
    def test_wire_thin_post_raises_scale_kind_naming_the_member(self):
        with pytest.raises(SpecGenerationError) as err:
            _postprocess(VICTORIAN_POST_REPLY, "advisory")
        assert err.value.kind == "scale"
        assert "shaft/shaft" in err.value.hint
        assert "hairline" in err.value.hint
        assert "real-world dimensions" in err.value.hint

    def test_final_attempt_ships_with_sliver_finding_in_violations(self):
        out = _postprocess(VICTORIAN_POST_REPLY, "advisory",
                           lenient_buildability=True)
        assert out["spec"]["asset_type"] == "custom"
        slivers = [v for v in out["violations"] if v.get("kind") == "sliver_member"]
        assert len(slivers) == 1
        assert slivers[0]["component"] == "shaft"
        assert "shaft/shaft" in slivers[0]["message"]
