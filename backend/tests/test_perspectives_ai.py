"""Tests for the four AI persona evaluators (architecture, mechanical,
civil/structural, industrial design) that back the Improve button.

blender/builders/perspectives.py is a SEPARATE, independently-evolving
sibling module (owned by another worker) that does not exist in this tree —
evaluate_perspectives imports it lazily, so every test here injects a fake
module into sys.modules instead of relying on the real thing. The provider
is always faked too (spec_ai.complete monkeypatched) — no network calls."""
import copy
import json
import sys
import types
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from backend.app import spec_ai  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.spec_ai import (  # noqa: E402
    MAX_PERSPECTIVE_FINDINGS,
    PERSONA_SYSTEM,
    _persona_finalize,
    _sanitize_perspective_finding,
    evaluate_perspectives,
    improve_spec,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID = (REPO_ROOT / "examples" / "street_light.json").read_text()
client = TestClient(app)

PERSPECTIVE_IDS = ("architecture", "mechanical", "civil", "design")

#: the shape blender/builders/perspectives.py::evaluate_all is contracted to
#: return — 4 entries, in this order, each with its own checks findings.
FAKE_ENTRIES = [
    {"id": "architecture", "label": "Architecture", "icon": "\U0001f3db",
     "metrics": {"height_m": 5.4},
     "findings": [{"severity": "info", "kind": "massing",
                   "message": "Proportions read as intended.", "source": "checks"}]},
    {"id": "mechanical", "label": "Mechanical Engineering", "icon": "\U0001f529",
     "metrics": {"joint_count": 3},
     "findings": [{"severity": "warning", "kind": "fastener_undersized",
                   "message": "Bolt undersized for the load.", "source": "checks"}]},
    {"id": "civil", "label": "Civil / Structural Engineering", "icon": "\U0001f3d7",
     "metrics": {"anchor_count": 1}, "findings": []},
    {"id": "design", "label": "Industrial Design", "icon": "\U0001f3a8",
     "metrics": {}, "findings": [{"severity": "error", "kind": "material_mismatch",
                                  "message": "Finish clashes with the base metal.",
                                  "source": "checks"}]},
]


def spec_dict():
    return json.loads(VALID)


def install_fake_perspectives_module(monkeypatch, entries=None):
    """Inject a fake blender.builders.perspectives module into sys.modules
    so evaluate_perspectives's lazy ``from blender.builders.perspectives
    import evaluate_all`` resolves without the real (sibling-owned, not-yet
    -present) module ever needing to exist."""
    mod = types.ModuleType("blender.builders.perspectives")
    mod.PERSPECTIVES = [{"id": e["id"], "label": e["label"], "icon": e["icon"]}
                        for e in (entries or FAKE_ENTRIES)]
    payload = copy.deepcopy(entries or FAKE_ENTRIES)
    mod.evaluate_all = lambda spec: copy.deepcopy(payload)
    monkeypatch.setitem(sys.modules, "blender.builders.perspectives", mod)


def good_persona_reply(message="AI says hi", severity="warning"):
    return json.dumps({"summary": "Looks fine overall.",
                       "findings": [{"severity": severity, "kind": "note",
                                     "message": message}]})


@pytest.fixture(autouse=True)
def mock_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")


class TestSanitizer:
    def test_clamps_bogus_severity_to_warning(self):
        clean = _sanitize_perspective_finding(
            {"severity": "critical", "kind": "x", "message": "hello"})
        assert clean["severity"] == "warning"
        assert clean["source"] == "ai"

    def test_drops_empty_message(self):
        assert _sanitize_perspective_finding({"severity": "error", "message": ""}) is None
        assert _sanitize_perspective_finding("not a dict") is None

    def test_persona_finalize_truncates_to_five_findings(self):
        raw = json.dumps({"summary": "Many issues.", "findings": [
            {"severity": "warning", "kind": f"k{i}", "message": f"m{i}"}
            for i in range(9)
        ]})
        summary, findings = _persona_finalize(raw)
        assert summary == "Many issues."
        assert len(findings) == MAX_PERSPECTIVE_FINDINGS

    def test_persona_finalize_rejects_non_json(self):
        with pytest.raises(ValueError):
            _persona_finalize("not json at all")

    def test_persona_finalize_rejects_missing_findings_array(self):
        with pytest.raises(ValueError):
            _persona_finalize(json.dumps({"summary": "x"}))


class TestEvaluatePerspectives:
    def test_returns_four_entries_in_order_with_merged_findings(self, monkeypatch):
        install_fake_perspectives_module(monkeypatch)
        monkeypatch.setattr(spec_ai, "complete",
                            lambda system, user, **kw: good_persona_reply("AI note"))
        out = evaluate_perspectives(spec_dict())
        assert [e["id"] for e in out] == list(PERSPECTIVE_IDS)
        for entry, fake in zip(out, FAKE_ENTRIES):
            assert entry["label"] == fake["label"]
            assert entry["icon"] == fake["icon"]
            assert entry["error"] is None
            assert entry["summary"] == "Looks fine overall."
            sources = [f["source"] for f in entry["findings"]]
            # checks findings first, then ai findings — never reordered
            assert sources == (["checks"] * len(fake["findings"])) + ["ai"]
            if fake["findings"]:
                assert entry["findings"][0]["message"] == fake["findings"][0]["message"]
            assert entry["findings"][-1]["message"] == "AI note"

    def test_one_persona_failure_does_not_affect_the_others(self, monkeypatch):
        install_fake_perspectives_module(monkeypatch)

        def fake_complete(system, user, **kw):
            if system == PERSONA_SYSTEM["mechanical"]:
                return "this is not json"
            return good_persona_reply("fine")

        monkeypatch.setattr(spec_ai, "complete", fake_complete)
        out = evaluate_perspectives(spec_dict())
        by_id = {e["id"]: e for e in out}

        mech = by_id["mechanical"]
        assert mech["error"] is not None
        assert mech["summary"] == ""
        # the checks finding survives even though the AI call failed
        assert len(mech["findings"]) == 1
        assert mech["findings"][0]["source"] == "checks"
        assert mech["findings"][0]["message"] == "Bolt undersized for the load."

        for pid in ("architecture", "civil", "design"):
            entry = by_id[pid]
            assert entry["error"] is None
            assert entry["summary"] == "Looks fine overall."

    def test_provider_error_is_reported_not_raised(self, monkeypatch):
        install_fake_perspectives_module(monkeypatch)

        def raising_complete(system, user, **kw):
            raise spec_ai.LLMError("DEEPSEEK_API_KEY is not set.")

        monkeypatch.setattr(spec_ai, "complete", raising_complete)
        out = evaluate_perspectives(spec_dict())
        assert len(out) == 4
        assert all(e["error"] and "API_KEY" in e["error"] for e in out)
        assert all(e["summary"] == "" for e in out)


class TestImproveSpecCarriesPerspectives:
    def test_result_carries_perspectives_and_prompt_embeds_a_finding(self, monkeypatch):
        install_fake_perspectives_module(monkeypatch)
        calls = []

        def fake_complete(system, user, **kw):
            calls.append((system, user))
            if system in PERSONA_SYSTEM.values():
                return good_persona_reply("Persona note")
            return VALID  # the main spec-generation call

        monkeypatch.setattr(spec_ai, "complete", fake_complete)
        out = improve_spec(spec_dict())

        assert [p["id"] for p in out["perspectives"]] == list(PERSPECTIVE_IDS)
        # exactly one call per persona + one for the main improve pass
        assert len(calls) == 5
        main_calls = [u for s, u in calls if s not in PERSONA_SYSTEM.values()]
        assert len(main_calls) == 1
        assert "Bolt undersized for the load." in main_calls[0]

    def test_removing_perspectives_would_fail_this(self, monkeypatch):
        """Regression guard: if evaluate_perspectives is dropped from
        improve_spec, "perspectives" disappears from the result."""
        install_fake_perspectives_module(monkeypatch)
        monkeypatch.setattr(spec_ai, "complete",
                            lambda system, user, **kw: (
                                good_persona_reply() if system in PERSONA_SYSTEM.values()
                                else VALID
                            ))
        out = improve_spec(spec_dict())
        assert "perspectives" in out
        assert len(out["perspectives"]) == 4


class TestEndpoint:
    def test_improve_endpoint_returns_four_perspectives_in_order(self, monkeypatch):
        install_fake_perspectives_module(monkeypatch)
        monkeypatch.setattr(spec_ai, "complete",
                            lambda system, user, **kw: (
                                good_persona_reply() if system in PERSONA_SYSTEM.values()
                                else VALID
                            ))
        r = client.post("/api/improve-spec", json={"spec": spec_dict()})
        assert r.status_code == 200
        data = r.json()
        assert [p["id"] for p in data["perspectives"]] == list(PERSPECTIVE_IDS)
        for p in data["perspectives"]:
            assert {"id", "label", "icon", "summary", "findings", "error"} <= p.keys()
