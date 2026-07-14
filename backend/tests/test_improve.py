"""Tests for /improve-spec: the deterministic Python-side checks (connection
audit + buildability + US-code validation) are gathered, embedded in the AI
prompt alongside the current spec, and the reply is routed through the
ordinary classified retry/validation pipeline — same envelope as
/refine-spec, plus a "findings" key carrying what was fed in."""
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from backend.app import spec_ai  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.spec_ai import (  # noqa: E402
    MAX_ATTEMPTS,
    _gather_findings,
    improve_spec,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID = (REPO_ROOT / "examples" / "street_light.json").read_text()
client = TestClient(app)

#: this test file is about the findings-gathering/prompt-embedding behavior
#: of improve_spec, not the four persona evaluators (covered in
#: test_perspectives_ai.py) — stub evaluate_perspectives so these tests don't
#: depend on blender/builders/perspectives.py (a separate sibling module) or
#: burn extra spec_ai.complete()/complete_stream() calls that would throw
#: off the "calls" assertions below.
FAKE_PERSPECTIVES = [
    {"id": pid, "label": pid.title(), "icon": "x", "summary": "", "findings": [], "error": None}
    for pid in ("architecture", "mechanical", "civil", "design")
]


@pytest.fixture(autouse=True)
def mock_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setattr(spec_ai, "evaluate_perspectives",
                        lambda spec, model=None: FAKE_PERSPECTIVES)


def spec_dict():
    return json.loads(VALID)


def floating_spec():
    """A component ('orb') with no load path to the ground — a buildability
    finding both the auditor and check_buildability can catch."""
    return {
        "asset_type": "sculpture", "name": "F", "units": "metric",
        "parameters": [],
        "primitives": [
            {"kind": "box", "name": "base", "component": "base",
             "location": [0, 0, 0.1], "params": {"size": [0.5, 0.5, 0.2]}},
            {"kind": "sphere", "name": "orb", "component": "orb",
             "location": [0, 0, 1.5], "params": {"radius": 0.2}},
        ],
    }


def bad_expression_spec():
    """A primitive expression referencing an id that doesn't exist — makes
    compute_primitives raise outright (not just report a soft finding)."""
    return {
        "asset_type": "table", "name": "T", "units": "metric",
        "parameters": [
            {"id": "seat_height", "label": "H", "type": "slider",
             "min": 0.3, "max": 1.0, "step": 0.05, "value": 0.45, "unit": "m"},
        ],
        "primitives": [
            {"kind": "box", "name": "top", "component": "top",
             "location": [0, 0, "seat_heigth + 0.1"],  # typo'd id
             "params": {"size": [1, 0.5, 0.04]}},
        ],
    }


def script_complete(monkeypatch, replies):
    """Replace spec_ai.complete with a scripted sequence; returns the list of
    user messages the pipeline actually sent (one per attempt)."""
    calls = []

    def fake_complete(system, user, **kwargs):
        calls.append(user)
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(spec_ai, "complete", fake_complete)
    return calls


class TestFindingsGatherer:
    def test_reports_floating_part(self):
        findings = _gather_findings(floating_spec())
        assert findings, "a floating part must produce at least one finding"
        assert any("float" in f["message"].lower() for f in findings)
        assert all({"severity", "kind", "message"} <= f.keys() for f in findings)

    def test_clean_spec_gathers_without_crashing(self):
        findings = _gather_findings(spec_dict())
        assert isinstance(findings, list)

    def test_broken_expression_yields_single_build_failure_finding(self):
        findings = _gather_findings(bad_expression_spec())
        assert len(findings) == 1
        assert findings[0]["severity"] == "error"
        assert findings[0]["kind"] == "build_failure"
        assert "seat_heigth" in findings[0]["message"]


class TestImproveSpec:
    def test_prompt_embeds_a_finding_and_returns_improved_spec(self, monkeypatch):
        calls = script_complete(monkeypatch, [VALID])
        out = improve_spec(floating_spec())
        assert len(calls) == 1
        assert "float" in calls[0].lower()
        assert out["spec"]["asset_type"] == "street_light"

    def test_findings_key_is_present_in_result(self, monkeypatch):
        script_complete(monkeypatch, [VALID])
        out = improve_spec(spec_dict())
        assert "findings" in out
        assert isinstance(out["findings"], list)

    def test_broken_input_prompt_carries_build_failure_finding(self, monkeypatch):
        calls = script_complete(monkeypatch, [VALID])
        out = improve_spec(bad_expression_spec())
        assert len(calls) == 1
        assert "build_failure" in calls[0]
        assert "seat_heigth" in calls[0]
        assert out["findings"][0]["kind"] == "build_failure"

    def test_removing_findings_gathering_would_fail_this(self, monkeypatch):
        # a regression guard: if the findings gatherer or its embedding into
        # the prompt is ever removed, this assertion (not just "findings" key
        # presence) breaks. Compare via the same JSON encoding the prompt
        # builder uses (embedded unicode is escaped, so a raw substring check
        # on the message text itself is not reliable).
        calls = script_complete(monkeypatch, [VALID])
        findings = _gather_findings(floating_spec())
        improve_spec(floating_spec())
        assert json.dumps(findings, separators=(",", ":")) in calls[0]

    def test_retry_loop_is_inherited_from_run(self, monkeypatch):
        calls = script_complete(monkeypatch, ["garbage", VALID])
        out = improve_spec(spec_dict())
        assert len(calls) == 2
        assert f"ATTEMPT 2 of {MAX_ATTEMPTS}" in calls[1]
        assert out["spec"]["asset_type"] == "street_light"


class TestStreamImproveSpec:
    SENTINEL = spec_ai.STREAM_SENTINEL

    def script_stream(self, monkeypatch, replies):
        calls = []

        def fake_stream(system, user, **kwargs):
            calls.append(user)
            reply = replies[min(len(calls) - 1, len(replies) - 1)]
            for i in range(0, len(reply), 256):
                yield reply[i : i + 256]

        monkeypatch.setattr(spec_ai, "complete_stream", fake_stream)
        return calls

    def collect(self, gen):
        text = "".join(gen)
        raw, payload = text.split(spec_ai.STREAM_SENTINEL)
        return raw, json.loads(payload)

    def test_stream_result_carries_findings(self, monkeypatch):
        calls = self.script_stream(monkeypatch, [VALID])
        raw, payload = self.collect(spec_ai.stream_improve_spec(floating_spec()))
        assert len(calls) == 1
        assert "float" in calls[0].lower()
        assert payload["ok"] is True
        assert payload["result"]["spec"]["asset_type"] == "street_light"
        assert isinstance(payload["result"]["findings"], list)
        assert payload["result"]["findings"]


class TestEndpoints:
    def test_improve_endpoint_mock(self):
        r = client.post("/api/improve-spec", json={"spec": spec_dict()})
        assert r.status_code == 200
        data = r.json()
        assert "findings" in data and isinstance(data["findings"], list)
        assert data["spec"]["asset_type"] == "street_light"

    def test_improve_stream_endpoint_mock(self):
        r = client.post("/api/improve-spec-stream", json={"spec": spec_dict()})
        assert r.status_code == 200
        payload = json.loads(r.text.split("<<<ASSETFORGE_RESULT>>>")[-1])
        assert payload["ok"] is True
        assert "findings" in payload["result"]

    def test_improve_rejects_unknown_model(self):
        r = client.post("/api/improve-spec",
                        json={"spec": spec_dict(), "model": "gpt-4o"})
        assert r.status_code == 422
