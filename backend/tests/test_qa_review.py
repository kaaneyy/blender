"""Tests for the AI pre-delivery QA reviewer pass on the GENERATE flows only
(generate_spec / stream_generate_spec): every generated spec is read back by
ONE AI reviewer call before it ships; a sanitized "reject" on a non-lenient
attempt rides the EXISTING classified retry engine with the reviewer's fixes
as the hint; the shipped envelope always reports the QA verdict; QA
infrastructure failures degrade to "skipped" and never block or brick
generation. refine/focus/wizard/improve/variations never run QA at all."""
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from backend.app import spec_ai  # noqa: E402
from backend.app.llm import LLMError  # noqa: E402
from backend.app.spec_ai import (  # noqa: E402
    MAX_ATTEMPTS,
    generate_spec,
    refine_spec,
    stream_generate_spec,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID = (REPO_ROOT / "examples" / "street_light.json").read_text()

PANEL_REPLY = "Design brief: a simple galvanized steel street light."


@pytest.fixture(autouse=True)
def mock_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")


def spec_dict():
    return json.loads(VALID)


def qa_json(verdict, problems=None, fixes=None):
    return json.dumps({"verdict": verdict, "problems": problems or [], "fixes": fixes or []})


def script_complete(monkeypatch, replies):
    """Replace spec_ai.complete with a scripted sequence; returns the list of
    user messages the pipeline actually sent (one per call, across BOTH the
    design panel, spec generation, and QA review passes — they all go
    through spec_ai.complete for generate_spec). A reply that is an
    Exception instance is raised instead of returned."""
    calls = []

    def fake_complete(system, user, **kwargs):
        calls.append(user)
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(spec_ai, "complete", fake_complete)
    return calls


def script_stream(monkeypatch, replies):
    """Like script_complete, but for spec_ai.complete_stream (the design
    panel and spec-generation passes in the streaming pipeline)."""
    calls = []

    def fake_stream(system, user, **kwargs):
        calls.append(user)
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        for i in range(0, len(reply), 256):
            yield reply[i : i + 256]

    monkeypatch.setattr(spec_ai, "complete_stream", fake_stream)
    return calls


def collect_stream(gen):
    text = "".join(gen)
    raw, payload = text.split(spec_ai.STREAM_SENTINEL)
    return raw, json.loads(payload)


class TestGenerateQAReview:
    def test_reject_then_approve(self, monkeypatch):
        """attempt 1 spec -> QA reject (problems/fixes) -> the correction
        prompt for attempt 2 carries the reviewer's fix text -> attempt 2
        spec -> QA approve -> the shipped spec is attempt 2's."""
        valid_2 = json.dumps({**spec_dict(), "name": "Attempt Two Light"})
        calls = script_complete(monkeypatch, [
            PANEL_REPLY,
            VALID,
            qa_json("reject",
                    problems=["Pole height reads as 0.02 m — absurdly short for a street light"],
                    fixes=["Set pole_height to at least 3 m as the request implies"]),
            valid_2,
            qa_json("approve"),
        ])
        out = generate_spec("a street light")

        assert len(calls) == 5
        assert calls[2].startswith("QA REVIEW.")
        # the correction prompt for attempt 2 contains the reviewer's fix
        assert "Set pole_height to at least 3 m as the request implies" in calls[3]
        # the shipped spec is attempt 2's, not attempt 1's
        assert out["spec"]["name"] == "Attempt Two Light"
        assert out["qa"]["verdict"] == "approved"
        assert out["qa"]["problems"] == []

    def test_qa_provider_error_skips_and_ships_on_first_attempt(self, monkeypatch):
        calls = script_complete(monkeypatch, [
            PANEL_REPLY,
            VALID,
            LLMError("LLM provider returned 500: upstream boom"),
        ])
        out = generate_spec("a street light")

        assert len(calls) == 3  # panel, spec attempt 1, QA attempt 1 — no retry
        assert out["spec"]["asset_type"] == "street_light"
        assert out["qa"]["verdict"] == "skipped"
        assert out["qa"]["problems"] == []
        assert out["qa"]["fixes"] == []

    def test_qa_malformed_json_skips_and_generation_unaffected(self, monkeypatch):
        calls = script_complete(monkeypatch, [
            PANEL_REPLY,
            VALID,
            "this is not JSON at all, sorry",
        ])
        out = generate_spec("a street light")

        assert len(calls) == 3
        assert out["spec"]["asset_type"] == "street_light"
        assert out["qa"]["verdict"] == "skipped"

    def test_qa_missing_verdict_field_skips(self, monkeypatch):
        calls = script_complete(monkeypatch, [
            PANEL_REPLY,
            VALID,
            json.dumps({"problems": ["something"], "fixes": []}),  # no "verdict"
        ])
        out = generate_spec("a street light")

        assert len(calls) == 3
        assert out["qa"]["verdict"] == "skipped"

    def test_persistent_reject_ships_on_lenient_final_attempt(self, monkeypatch):
        """Rejects on every attempt: the final (lenient) attempt SHIPS
        anyway with the rejection visible — generation never fails."""
        reject = qa_json("reject", problems=["problem X"], fixes=["fix X"])
        calls = script_complete(monkeypatch, [
            PANEL_REPLY, VALID, reject, VALID, reject, VALID, reject,
        ])
        out = generate_spec("a street light")

        assert len(calls) == 1 + 2 * MAX_ATTEMPTS  # panel + (spec, QA) per attempt
        assert out["spec"]["asset_type"] == "street_light"
        assert out["qa"]["verdict"] == "rejected"
        assert out["qa"]["problems"] == ["problem X"]
        assert out["qa"]["fixes"] == ["fix X"]

    def test_refine_spec_never_runs_qa_review(self, monkeypatch):
        """refine/focus/wizard/improve/variations must be completely
        unaffected: no QA-prefixed message is ever sent, and the result
        carries no "qa" key."""
        def fake_complete(system, user, **kwargs):
            assert not user.startswith("QA REVIEW"), (
                "refine_spec must never trigger the QA reviewer pass")
            return VALID

        monkeypatch.setattr(spec_ai, "complete", fake_complete)
        out = refine_spec(spec_dict(), "make it taller")
        assert out["spec"]["asset_type"] == "street_light"
        assert "qa" not in out

    def test_stream_generate_spec_carries_qa(self, monkeypatch):
        stream_calls = script_stream(monkeypatch, [PANEL_REPLY, VALID])
        complete_calls = script_complete(monkeypatch, [qa_json("approve")])

        raw, payload = collect_stream(stream_generate_spec("a street light"))

        assert payload["ok"] is True
        assert payload["result"]["spec"]["asset_type"] == "street_light"
        assert payload["result"]["qa"]["verdict"] == "approved"
        assert len(stream_calls) == 2  # panel pass + spec generation, streamed
        assert len(complete_calls) == 1  # the QA reviewer call, non-streamed
        assert complete_calls[0].startswith("QA REVIEW.")

    def test_stream_generate_spec_qa_reject_then_approve(self, monkeypatch):
        valid_2 = json.dumps({**spec_dict(), "name": "Streamed Attempt Two"})
        stream_calls = script_stream(monkeypatch, [PANEL_REPLY, VALID, valid_2])
        complete_calls = script_complete(monkeypatch, [
            qa_json("reject", problems=["too short"], fixes=["make the pole taller"]),
            qa_json("approve"),
        ])

        raw, payload = collect_stream(stream_generate_spec("a street light"))

        assert payload["ok"] is True
        assert payload["result"]["spec"]["name"] == "Streamed Attempt Two"
        assert payload["result"]["qa"]["verdict"] == "approved"
        assert len(complete_calls) == 2  # one QA call per attempt
        assert "make the pole taller" in stream_calls[2]  # attempt-2 correction prompt
        assert "addressing the AI reviewer's rejections" in raw


class TestGeometryDigest:
    def test_shape_and_bounds(self):
        from blender.builders.base import compute_primitives

        spec = spec_dict()
        prims = compute_primitives(spec)
        digest = spec_ai._geometry_digest(prims, spec, violations=[{"message": "v"}])

        assert set(digest) == {"components", "primitives", "violations", "standards"}
        assert digest["violations"] == [{"message": "v"}]
        # street_light has a real standards entry
        assert isinstance(digest["standards"], dict)
        assert "parameters" in digest["standards"]

        for comp, entry in digest["components"].items():
            assert isinstance(comp, str)
            assert len(entry["envelope_mm"]) == 3
            assert all(isinstance(d, int) for d in entry["envelope_mm"])
            assert entry["prim_count"] >= 1

        assert len(digest["primitives"]) <= spec_ai.QA_DIGEST_MAX_PRIMS
        for p in digest["primitives"]:
            assert set(p) == {"name", "kind", "dims_mm"}
            assert len(p["dims_mm"]) == 3
            # sorted descending
            assert p["dims_mm"] == sorted(p["dims_mm"], reverse=True)
        # hardware/cut prims are excluded
        assert all("hardware" not in p["name"].split("/")[0] for p in digest["primitives"])

    def test_no_standards_entry_note(self):
        from blender.builders.base import compute_primitives

        spec = {
            "asset_type": "totally_unknown_widget", "name": "W", "units": "metric",
            "parameters": [],
            "primitives": [
                {"kind": "box", "name": "body", "component": "body",
                 "location": [0, 0, 0.1], "params": {"size": [0.2, 0.2, 0.2]}},
            ],
        }
        prims = compute_primitives(spec)
        digest = spec_ai._geometry_digest(prims, spec, violations=[])
        assert digest["standards"] == "no standards entry for this asset_type"


class TestQAFinalize:
    def test_approve_round_trips(self):
        qa = spec_ai._qa_finalize(qa_json("approve"))
        assert qa == {"verdict": "approve", "problems": [], "fixes": []}

    def test_reject_caps_counts_and_text_length(self):
        problems = [f"problem {i}" for i in range(20)]
        long_fix = "x" * 1000
        raw = qa_json("reject", problems=problems, fixes=[long_fix])
        qa = spec_ai._qa_finalize(raw)
        assert qa["verdict"] == "reject"
        assert len(qa["problems"]) == spec_ai.QA_MAX_ITEMS
        assert len(qa["fixes"][0]) == spec_ai.QA_MAX_TEXT_LEN

    def test_bad_verdict_raises(self):
        with pytest.raises(ValueError):
            spec_ai._qa_finalize(json.dumps({"verdict": "maybe"}))

    def test_not_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            spec_ai._qa_finalize("not json")
