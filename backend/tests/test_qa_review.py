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
    """generate now runs the 4-layer pipeline (structure → function →
    connections → materials); QA is a FINAL ADVISORY review — it reports a
    verdict but never re-runs the layers. The non-stream complete() sequence
    for a clean generate is: [design brief, layer1, layer2, layer3, layer4,
    QA] = 6 calls."""
    #: the 5 spec-writing calls (brief + 4 layers), before the QA call.
    LAYER_REPLIES = [PANEL_REPLY, VALID, VALID, VALID, VALID]

    def test_qa_verdict_and_layers_trace(self, monkeypatch):
        calls = script_complete(monkeypatch, [*self.LAYER_REPLIES, qa_json("approve")])
        out = generate_spec("a street light")

        assert len(calls) == 6
        assert calls[5].startswith("QA REVIEW.")  # QA is the final call
        assert out["spec"]["asset_type"] == "street_light"
        assert out["qa"]["verdict"] == "approved"
        assert [l["layer"] for l in out["layers"]] == list(spec_ai.LAYER_ORDER)
        assert [l["status"] for l in out["layers"]] == ["built"] * 4

    def test_qa_reject_is_advisory_ships_without_retry(self, monkeypatch):
        # QA reject on the layered pipeline is ADVISORY: the spec still ships
        # with the rejection visible, and there is NO regeneration.
        reject = qa_json("reject", problems=["problem X"], fixes=["fix X"])
        calls = script_complete(monkeypatch, [*self.LAYER_REPLIES, reject])
        out = generate_spec("a street light")

        assert len(calls) == 6  # exactly one pass + one QA call — no retry
        assert out["spec"]["asset_type"] == "street_light"
        assert out["qa"]["verdict"] == "rejected"
        assert out["qa"]["problems"] == ["problem X"]
        assert out["qa"]["fixes"] == ["fix X"]

    def test_qa_provider_error_skips_and_ships(self, monkeypatch):
        calls = script_complete(monkeypatch, [
            *self.LAYER_REPLIES,
            LLMError("LLM provider returned 500: upstream boom"),
        ])
        out = generate_spec("a street light")

        assert len(calls) == 6
        assert out["spec"]["asset_type"] == "street_light"
        assert out["qa"]["verdict"] == "skipped"
        assert out["qa"]["problems"] == [] and out["qa"]["fixes"] == []

    def test_qa_malformed_json_skips(self, monkeypatch):
        script_complete(monkeypatch, [*self.LAYER_REPLIES, "this is not JSON at all"])
        out = generate_spec("a street light")

        assert out["spec"]["asset_type"] == "street_light"
        assert out["qa"]["verdict"] == "skipped"

    def test_qa_missing_verdict_field_skips(self, monkeypatch):
        script_complete(monkeypatch, [
            *self.LAYER_REPLIES, json.dumps({"problems": ["x"], "fixes": []}),
        ])
        out = generate_spec("a street light")

        assert out["qa"]["verdict"] == "skipped"

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

    def test_stream_generate_spec_carries_layers_and_skips_qa(self, monkeypatch):
        """The STREAMING generate does NOT run the QA reviewer. Its verdict is
        advisory and nothing in the UI reads it, while the call added a whole
        extra provider round-trip at the very end of an already-long run —
        exactly where a serverless/proxy timeout cuts the connection and costs
        the user the entire generation. Non-streamed generate_spec keeps it
        (see test_qa_verdict_and_layers_trace above)."""
        stream_calls = script_stream(monkeypatch, [PANEL_REPLY, VALID, VALID, VALID, VALID])
        complete_calls = script_complete(monkeypatch, [qa_json("approve")])

        raw, payload = collect_stream(stream_generate_spec("a street light"))

        assert payload["ok"] is True
        assert payload["result"]["spec"]["asset_type"] == "street_light"
        assert [l["status"] for l in payload["result"]["layers"]] == ["built"] * 4
        assert len(stream_calls) == 5  # brief + 4 layers, streamed
        assert complete_calls == []  # no QA round-trip on the streaming path
        assert "qa" not in payload["result"]


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
