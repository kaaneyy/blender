"""Tests for the AI connection review ("Check connections with AI"): the AI
answers in the deterministic auditor's findings format, every proposal is
sanitized against the real component names, bounded, test-built, and the
whole pass rides the classified 3-attempt retry engine."""
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from backend.app import spec_ai  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.spec_ai import (  # noqa: E402
    MAX_ATTEMPTS,
    _review_finalize,
    review_connections,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")


def spec_dict():
    return json.loads((REPO_ROOT / "examples" / "street_light.json").read_text())


def det():
    from blender.builders.audit import audit_connections

    return audit_connections(spec_dict())


def review_json(findings):
    return json.dumps({"findings": findings})


GOOD_FINDING = {
    "severity": "warning",
    "title": "Slip fitter and band clamp are redundant",
    "detail": "One fitting is enough at the arm.",
    "component": "arm",
    "joint": 2,
    "fix": {
        "summary": "Suppress the band clamp",
        "before": "band clamp + slip fitter at the arm",
        "after": "slip fitter only",
        "ops": [{"op": "declare", "a": "arm", "b": "pole", "type": "none"}],
    },
}


class TestSanitization:
    def test_valid_finding_passes_with_stable_id(self):
        out = _review_finalize(review_json([GOOD_FINDING]), spec_dict(), det())
        assert len(out["findings"]) == 1
        f = out["findings"][0]
        assert f["id"] == "ai:1" and f["kind"] == "ai_review"
        assert f["fix"]["ops"][0]["type"] == "none"
        assert out["joints"] >= 1 and out["components"] >= 1

    def test_unknown_component_ops_are_dropped(self):
        bad = json.loads(json.dumps(GOOD_FINDING))
        bad["fix"]["ops"] = [
            {"op": "nudge", "key": "zeppelin", "delta": [0.01, 0, 0]},
            {"op": "declare", "a": "arm", "b": "warp_core", "type": "weld"},
        ]
        out = _review_finalize(review_json([bad]), spec_dict(), det())
        assert out["findings"][0]["fix"] is None  # report-only after cleaning

    def test_oversized_and_zero_nudges_are_dropped(self):
        bad = json.loads(json.dumps(GOOD_FINDING))
        bad["fix"]["ops"] = [
            {"op": "nudge", "key": "arm", "delta": [0.9, 0, 0]},   # too big
            {"op": "nudge", "key": "arm", "delta": [0, 0, 0]},     # no move
        ]
        out = _review_finalize(review_json([bad]), spec_dict(), det())
        assert out["findings"][0]["fix"] is None

    def test_unknown_connection_type_is_dropped(self):
        bad = json.loads(json.dumps(GOOD_FINDING))
        bad["fix"]["ops"] = [{"op": "declare", "a": "arm", "b": "pole",
                              "type": "duct_tape"}]
        out = _review_finalize(review_json([bad]), spec_dict(), det())
        assert out["findings"][0]["fix"] is None

    def test_part_path_refs_are_accepted(self):
        f = json.loads(json.dumps(GOOD_FINDING))
        f["fix"]["ops"] = [
            {"op": "nudge", "key": "pole/cap", "delta": [0, 0, 0.02]}]
        out = _review_finalize(review_json([f]), spec_dict(), det())
        assert out["findings"][0]["fix"]["ops"][0]["key"] == "pole/cap"

    def test_findings_are_capped(self):
        many = [dict(GOOD_FINDING, fix=None) for _ in range(20)]
        out = _review_finalize(review_json(many), spec_dict(), det())
        assert len(out["findings"]) == spec_ai.MAX_REVIEW_FINDINGS

    def test_titleless_findings_are_dropped(self):
        out = _review_finalize(
            review_json([{"severity": "warning", "detail": "??"}]),
            spec_dict(), det())
        assert out["findings"] == []

    def test_missing_findings_array_is_classified(self):
        with pytest.raises(spec_ai.SpecGenerationError) as err:
            _review_finalize('{"notes": "looks fine"}', spec_dict(), det())
        assert err.value.kind == "schema"

    def test_truncated_review_is_classified(self):
        with pytest.raises(spec_ai.SpecGenerationError) as err:
            _review_finalize('{"findings": [{"severity": "warn', spec_dict(), det())
        assert err.value.kind == "truncated"


class TestReviewRetryLoop:
    def script(self, monkeypatch, replies):
        calls = []

        def fake_complete(system, user, **kwargs):
            calls.append(user)
            return replies[min(len(calls) - 1, len(replies) - 1)]

        monkeypatch.setattr(spec_ai, "complete", fake_complete)
        return calls

    def test_recovers_from_bad_review_json(self, monkeypatch):
        calls = self.script(
            monkeypatch, ["The connections look mostly fine to me!",
                          review_json([GOOD_FINDING])])
        out = review_connections(spec_dict())
        assert len(calls) == 2
        assert f"ATTEMPT 2 of {MAX_ATTEMPTS}" in calls[1]
        assert out["findings"][0]["title"].startswith("Slip fitter")

    def test_prompt_carries_schedule_and_valid_names(self, monkeypatch):
        calls = self.script(monkeypatch, [review_json([])])
        out = review_connections(spec_dict())
        assert out["findings"] == []
        assert "Joint schedule" in calls[0]
        assert "Component names you may reference" in calls[0]
        assert '"arm"' in calls[0]
        assert "do NOT repeat" in calls[0]


SENTINEL = "<<<ASSETFORGE_RESULT>>>"


class TestEndpoints:
    def test_review_endpoint_mock(self):
        r = client.post("/api/review-connections", json={"spec": spec_dict()})
        assert r.status_code == 200
        data = r.json()
        assert data["joints"] >= 1
        assert data["findings"][0]["kind"] == "ai_review"
        assert data["findings"][0]["fix"] is None  # mock finding is report-only

    def test_review_stream_endpoint_mock(self):
        r = client.post("/api/review-connections-stream",
                        json={"spec": spec_dict()})
        assert r.status_code == 200
        payload = json.loads(r.text.split(SENTINEL)[-1])
        assert payload["ok"] is True
        assert payload["result"]["findings"][0]["kind"] == "ai_review"

    def test_review_rejects_unknown_model(self):
        r = client.post("/api/review-connections",
                        json={"spec": spec_dict(), "model": "gpt-4o"})
        assert r.status_code == 422
