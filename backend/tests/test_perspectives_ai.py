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
    MAX_PEER_NOTES,
    MAX_PERSPECTIVE_FINDINGS,
    PERSONA_SYSTEM,
    _cross_review_finalize,
    _cross_review_system,
    _persona_finalize,
    _sanitize_consensus,
    _sanitize_peer_note,
    _sanitize_perspective_finding,
    cross_review_perspectives,
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


def good_cross_review_reply():
    """A cross-review reply with a self-note (must be dropped) and a bogus
    stance (must clamp to "refine") mixed in with otherwise-valid notes."""
    return json.dumps({
        "peer_notes": {
            "architecture": [
                {"from": "architecture", "stance": "concur",
                 "note": "self-note, must be dropped"},
                {"from": "mechanical", "stance": "concur",
                 "note": "Massing checks out structurally."},
                {"from": "civil", "stance": "not_a_real_stance",
                 "note": "Needs an anchor detail called out."},
            ],
            "mechanical": [
                {"from": "design", "stance": "dispute",
                 "note": "The bolt is fine for this load case."},
            ],
        },
        "consensus": {
            "summary": "Panel agrees the base needs reinforcement.",
            "priorities": ["Reinforce base anchor", "Verify bolt torque"],
        },
    })


def fake_complete_with_cross_review(calls, cross_review_system):
    """A fake ``complete`` that routes persona / cross-review / main-improve
    calls to distinct canned replies and records every call."""
    def fake_complete(system, user, **kw):
        calls.append((system, user))
        if system in PERSONA_SYSTEM.values():
            return good_persona_reply("Persona note")
        if system == cross_review_system:
            return good_cross_review_reply()
        return VALID  # the main spec-generation call
    return fake_complete


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
        cross_review_system = _cross_review_system()
        calls = []
        monkeypatch.setattr(spec_ai, "complete",
                            fake_complete_with_cross_review(calls, cross_review_system))
        out = improve_spec(spec_dict())

        assert [p["id"] for p in out["perspectives"]] == list(PERSPECTIVE_IDS)
        # one call per persona + one for cross-review + one for the main improve pass
        assert len(calls) == 6
        main_calls = [u for s, u in calls
                      if s not in PERSONA_SYSTEM.values() and s != cross_review_system]
        assert len(main_calls) == 1
        assert "Bolt undersized for the load." in main_calls[0]

    def test_removing_perspectives_would_fail_this(self, monkeypatch):
        """Regression guard: if evaluate_perspectives is dropped from
        improve_spec, "perspectives" disappears from the result."""
        install_fake_perspectives_module(monkeypatch)
        cross_review_system = _cross_review_system()
        monkeypatch.setattr(spec_ai, "complete",
                            fake_complete_with_cross_review([], cross_review_system))
        out = improve_spec(spec_dict())
        assert "perspectives" in out
        assert len(out["perspectives"]) == 4


class TestCrossReviewSanitizer:
    """Unit-level tests for the sanitizer helpers behind
    ``cross_review_perspectives`` — the pieces that guarantee a malformed
    reply never leaks unsafe/self-referential data into the envelope."""

    VALID_IDS = set(PERSPECTIVE_IDS)

    def test_drops_self_note(self):
        assert _sanitize_peer_note(
            {"from": "architecture", "stance": "concur", "note": "hi"},
            "architecture", self.VALID_IDS) is None

    def test_drops_unknown_from(self):
        assert _sanitize_peer_note(
            {"from": "plumbing", "stance": "concur", "note": "hi"},
            "architecture", self.VALID_IDS) is None

    def test_drops_empty_note(self):
        assert _sanitize_peer_note(
            {"from": "mechanical", "stance": "concur", "note": "   "},
            "architecture", self.VALID_IDS) is None

    def test_clamps_bogus_stance_to_refine(self):
        note = _sanitize_peer_note(
            {"from": "mechanical", "stance": "furious", "note": "hi"},
            "architecture", self.VALID_IDS)
        assert note["stance"] == "refine"

    def test_consensus_requires_summary_and_priorities(self):
        assert _sanitize_consensus({"summary": "x"}) is None
        assert _sanitize_consensus({"priorities": ["x"]}) is None
        assert _sanitize_consensus("not a dict") is None
        assert _sanitize_consensus(None) is None

    def test_consensus_caps_priorities_to_three(self):
        consensus = _sanitize_consensus(
            {"summary": "ok", "priorities": ["a", "b", "c", "d", "e"]})
        assert len(consensus["priorities"]) == 3

    def test_finalize_drops_self_notes_and_clamps_stances(self):
        peer_notes, consensus = _cross_review_finalize(
            good_cross_review_reply(), self.VALID_IDS)
        arch_notes = peer_notes["architecture"]
        assert all(n["from"] != "architecture" for n in arch_notes)
        assert len(arch_notes) == 2
        civil_note = next(n for n in arch_notes if n["from"] == "civil")
        assert civil_note["stance"] == "refine"  # was a bogus stance
        assert consensus["priorities"] == ["Reinforce base anchor", "Verify bolt torque"]

    def test_finalize_rejects_non_json(self):
        with pytest.raises(Exception):
            _cross_review_finalize("not json at all", self.VALID_IDS)


class TestCrossReviewPerspectives:
    """Integration tests for ``cross_review_perspectives`` itself and its
    wiring into ``improve_spec``/``stream_improve_spec``."""

    def test_happy_path_notes_land_and_consensus_attached(self, monkeypatch):
        install_fake_perspectives_module(monkeypatch)
        cross_review_system = _cross_review_system()
        calls = []
        monkeypatch.setattr(spec_ai, "complete",
                            fake_complete_with_cross_review(calls, cross_review_system))
        out = improve_spec(spec_dict())

        by_id = {p["id"]: p for p in out["perspectives"]}
        arch_notes = by_id["architecture"]["peer_notes"]
        assert all(n["from"] != "architecture" for n in arch_notes)
        assert {n["from"] for n in arch_notes} == {"mechanical", "civil"}
        # the bogus stance clamped instead of being dropped
        civil_note = next(n for n in arch_notes if n["from"] == "civil")
        assert civil_note["stance"] == "refine"
        assert by_id["mechanical"]["peer_notes"][0]["from"] == "design"
        # entries the cross-review didn't mention get no peer_notes key at all
        assert "peer_notes" not in by_id["civil"]
        assert "peer_notes" not in by_id["design"]

        assert out["consensus"]["summary"] == "Panel agrees the base needs reinforcement."
        assert out["consensus"]["priorities"] == [
            "Reinforce base anchor", "Verify bolt torque"]

        # the improve prompt itself carries a priority string
        main_calls = [u for s, u in calls
                      if s not in PERSONA_SYSTEM.values() and s != cross_review_system]
        assert "Reinforce base anchor" in main_calls[0]
        assert "panel's agreed priorities" in main_calls[0].lower()

    def test_cross_review_provider_error_degrades_silently(self, monkeypatch):
        install_fake_perspectives_module(monkeypatch)
        cross_review_system = _cross_review_system()

        def fake_complete(system, user, **kw):
            if system in PERSONA_SYSTEM.values():
                return good_persona_reply("Persona note")
            if system == cross_review_system:
                raise spec_ai.LLMError("provider hiccup")
            return VALID

        monkeypatch.setattr(spec_ai, "complete", fake_complete)
        out = improve_spec(spec_dict())

        assert out["ok"] is True
        assert "consensus" not in out
        assert all("peer_notes" not in p for p in out["perspectives"])

    def test_cross_review_invalid_json_degrades_silently(self, monkeypatch):
        install_fake_perspectives_module(monkeypatch)
        cross_review_system = _cross_review_system()

        def fake_complete(system, user, **kw):
            if system in PERSONA_SYSTEM.values():
                return good_persona_reply("Persona note")
            if system == cross_review_system:
                return "not json at all"
            return VALID

        monkeypatch.setattr(spec_ai, "complete", fake_complete)
        out = improve_spec(spec_dict())

        assert out["ok"] is True
        assert "consensus" not in out
        assert all("peer_notes" not in p for p in out["perspectives"])

    def test_all_empty_evaluations_skip_the_cross_review_call(self, monkeypatch):
        # both the checks findings AND the persona AI findings come back
        # empty — genuinely nothing for the panel to react to.
        empty_entries = [dict(e, findings=[]) for e in FAKE_ENTRIES]
        install_fake_perspectives_module(monkeypatch, entries=empty_entries)
        cross_review_system = _cross_review_system()
        calls = []
        empty_persona_reply = json.dumps({"summary": "Nothing to report.", "findings": []})

        def fake_complete(system, user, **kw):
            calls.append((system, user))
            if system in PERSONA_SYSTEM.values():
                return empty_persona_reply
            if system == cross_review_system:
                return good_cross_review_reply()
            return VALID

        monkeypatch.setattr(spec_ai, "complete", fake_complete)
        out = improve_spec(spec_dict())

        assert all(len(p["findings"]) == 0 for p in out["perspectives"])
        # the cross-review system prompt was never called
        assert not any(s == cross_review_system for s, u in calls)
        assert "consensus" not in out
        assert all("peer_notes" not in p for p in out["perspectives"])

    def test_direct_call_returns_empty_on_malformed_reply(self, monkeypatch):
        install_fake_perspectives_module(monkeypatch)
        monkeypatch.setattr(spec_ai, "complete", lambda system, user, **kw: "garbage")
        peer_notes, consensus = cross_review_perspectives(spec_dict(), FAKE_ENTRIES)
        assert peer_notes == {}
        assert consensus is None

    def test_max_peer_notes_per_entry_enforced(self, monkeypatch):
        install_fake_perspectives_module(monkeypatch)
        many_notes = json.dumps({
            "peer_notes": {
                "architecture": [
                    {"from": pid, "stance": "concur", "note": f"note {i}"}
                    for i, pid in enumerate(
                        ["mechanical", "civil", "design"] * 3)
                ],
            },
            "consensus": None,
        })
        monkeypatch.setattr(spec_ai, "complete", lambda system, user, **kw: many_notes)
        peer_notes, consensus = cross_review_perspectives(spec_dict(), FAKE_ENTRIES)
        assert len(peer_notes["architecture"]) == MAX_PEER_NOTES


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

    def test_improve_endpoint_passes_through_peer_notes_and_consensus(self, monkeypatch):
        install_fake_perspectives_module(monkeypatch)
        cross_review_system = _cross_review_system()
        monkeypatch.setattr(spec_ai, "complete",
                            fake_complete_with_cross_review([], cross_review_system))
        r = client.post("/api/improve-spec", json={"spec": spec_dict()})
        assert r.status_code == 200
        data = r.json()
        by_id = {p["id"]: p for p in data["perspectives"]}
        assert "peer_notes" in by_id["architecture"]
        assert by_id["architecture"]["peer_notes"][0]["from"] != "architecture"
        assert "consensus" in data
        assert data["consensus"]["priorities"]
