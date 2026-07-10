"""Tests for the classified 3-attempt generation retry system: every failure
kind is understood (truncated / not-JSON / schema / build / buildability /
transient provider), fed back as a targeted correction, and retried up to
MAX_ATTEMPTS model calls until the spec actually generates."""
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from backend.app import spec_ai  # noqa: E402
from backend.app.llm import LLMError  # noqa: E402
from backend.app.spec_ai import (  # noqa: E402
    MAX_ATTEMPTS,
    SpecGenerationError,
    _looks_truncated,
    _postprocess,
    refine_spec,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID = (REPO_ROOT / "examples" / "street_light.json").read_text()


@pytest.fixture(autouse=True)
def mock_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")


def script_complete(monkeypatch, replies):
    """Replace spec_ai.complete with a scripted sequence; returns the list of
    user messages the pipeline actually sent (one per attempt). A reply that
    is an Exception instance is raised instead of returned."""
    calls = []

    def fake_complete(system, user, **kwargs):
        calls.append(user)
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(spec_ai, "complete", fake_complete)
    return calls


def spec_dict():
    return json.loads(VALID)


def run_refine(message="make it taller"):
    return refine_spec(spec_dict(), message)


class TestRetryLoop:
    def test_recovers_on_second_attempt(self, monkeypatch):
        calls = script_complete(monkeypatch, ["I cannot help with that.", VALID])
        out = run_refine()
        assert out["spec"]["asset_type"] == "street_light"
        assert len(calls) == 2
        # the correction prompt names the attempt and the targeted fix
        assert f"ATTEMPT 2 of {MAX_ATTEMPTS}" in calls[1]
        assert "Return ONLY the AssetSpec JSON object" in calls[1]

    def test_recovers_on_third_attempt_with_error_history(self, monkeypatch):
        bad_schema = spec_dict()
        bad_schema["evil_extra"] = True
        calls = script_complete(
            monkeypatch, ["garbage", json.dumps(bad_schema), VALID])
        out = run_refine()
        assert out["spec"]["asset_type"] == "street_light"
        assert len(calls) == 3
        # the third prompt carries the FULL history so the model can't cycle
        assert "1. [not_json]" in calls[2]
        assert "2. [schema]" in calls[2]
        assert "evil_extra" in calls[2]
        assert "Do not repeat ANY of these mistakes." in calls[2]

    def test_gives_up_after_max_attempts(self, monkeypatch):
        calls = script_complete(monkeypatch, ["garbage forever"])
        with pytest.raises(SpecGenerationError,
                           match=f"after {MAX_ATTEMPTS} attempts"):
            run_refine()
        assert len(calls) == MAX_ATTEMPTS

    def test_truncated_answer_is_not_echoed_back(self, monkeypatch):
        truncated = VALID[: len(VALID) // 2]
        calls = script_complete(monkeypatch, [truncated, VALID])
        out = run_refine()
        assert out["spec"]["asset_type"] == "street_light"
        # regenerate compactly, don't re-feed the cut-off text
        assert "ran out of room" in calls[1]
        assert "repair it in place" not in calls[1]

    def test_fixable_answer_is_echoed_for_in_place_repair(self, monkeypatch):
        bad_schema = spec_dict()
        bad_schema["evil_extra"] = True
        calls = script_complete(monkeypatch, [json.dumps(bad_schema), VALID])
        run_refine()
        assert "repair it in place" in calls[1]

    def test_transient_provider_error_is_retried(self, monkeypatch):
        calls = script_complete(
            monkeypatch, [LLMError("LLM provider returned 503: upstream"), VALID])
        out = run_refine()
        assert out["spec"]["asset_type"] == "street_light"
        assert len(calls) == 2
        # the model never answered, so the second call repeats the prompt
        assert calls[0] == calls[1]

    def test_config_error_aborts_immediately(self, monkeypatch):
        calls = script_complete(
            monkeypatch, [LLMError("DEEPSEEK_API_KEY is not set. Add it...")])
        with pytest.raises(LLMError, match="DEEPSEEK_API_KEY"):
            run_refine()
        assert len(calls) == 1

    def _floating(self):
        return json.dumps({
            "asset_type": "sculpture", "name": "F", "units": "metric",
            "parameters": [],
            "primitives": [
                {"kind": "box", "name": "base", "component": "base",
                 "location": [0, 0, 0.1], "params": {"size": [0.5, 0.5, 0.2]}},
                {"kind": "sphere", "name": "orb", "component": "orb",
                 "location": [0, 0, 1.5], "params": {"radius": 0.2}},
            ],
        })

    def test_buildability_is_strict_until_the_final_attempt(self, monkeypatch):
        calls = script_complete(monkeypatch, [self._floating()])
        out = run_refine()
        # attempts 1-2 fail strict and feed the machine findings back;
        # attempt 3 accepts leniently with the findings as violations
        assert len(calls) == MAX_ATTEMPTS
        assert "floats" in calls[1]
        assert out["ok"] is False
        assert any(v.get("parameter_id") == "__buildability__"
                   for v in out["violations"])


class TestDiagnosis:
    def test_looks_truncated(self):
        assert _looks_truncated('{"a": [1, 2')
        assert _looks_truncated('{"a": "unterminated')
        assert not _looks_truncated('{"a": 1}')
        assert not _looks_truncated("no json here at all")

    def test_truncated_kind(self):
        with pytest.raises(SpecGenerationError) as err:
            _postprocess(VALID[: len(VALID) // 2], "strict")
        assert err.value.kind == "truncated"
        assert "compact" in err.value.hint

    def test_schema_hint_names_the_field(self):
        bad = spec_dict()
        bad["evil_extra"] = True
        with pytest.raises(SpecGenerationError) as err:
            _postprocess(json.dumps(bad), "strict")
        assert err.value.kind == "schema"
        assert "evil_extra" in err.value.hint
        assert "unknown fields" in err.value.hint

    def test_expression_hint_lists_available_ids(self):
        bad = {
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
        with pytest.raises(SpecGenerationError) as err:
            _postprocess(json.dumps(bad), "strict")
        assert err.value.kind == "build"
        assert "seat_height" in err.value.hint  # the id that IS available

    def test_enum_hint_lists_allowed_values(self):
        bad = spec_dict()
        bad["connections"] = [{"a": "pole", "b": "ground", "type": "duct_tape"}]
        with pytest.raises(SpecGenerationError) as err:
            _postprocess(json.dumps(bad), "strict")
        assert err.value.kind == "schema"
        assert "anchor_base" in err.value.hint


SENTINEL = "<<<ASSETFORGE_RESULT>>>"


class TestStreamingRetry:
    def script_stream(self, monkeypatch, replies):
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

    def collect(self, gen):
        text = "".join(gen)
        raw, payload = text.split(spec_ai.STREAM_SENTINEL)
        return raw, json.loads(payload)

    def test_stream_recovers_and_reports_attempts(self, monkeypatch):
        calls = self.script_stream(monkeypatch, ["not json", VALID])
        raw, payload = self.collect(
            spec_ai.stream_refine_spec(spec_dict(), "make it taller"))
        assert payload["ok"] is True
        assert payload["attempts"] == 2
        assert payload["result"]["spec"]["asset_type"] == "street_light"
        assert f"[attempt 1 of {MAX_ATTEMPTS} failed" in raw
        assert "wasn't clean JSON" in raw
        assert len(calls) == 2

    def test_stream_gives_up_with_classified_error(self, monkeypatch):
        calls = self.script_stream(monkeypatch, ["still not json"])
        raw, payload = self.collect(
            spec_ai.stream_refine_spec(spec_dict(), "make it taller"))
        assert payload["ok"] is False
        assert payload["kind"] == "not_json"
        assert payload["attempts"] == MAX_ATTEMPTS
        assert f"after {MAX_ATTEMPTS} attempts" in payload["error"]
        assert len(calls) == MAX_ATTEMPTS

    def test_stream_retries_transient_provider_error(self, monkeypatch):
        calls = self.script_stream(
            monkeypatch, [LLMError("LLM request failed: ReadTimeout: x"), VALID])
        raw, payload = self.collect(
            spec_ai.stream_refine_spec(spec_dict(), "make it taller"))
        assert payload["ok"] is True
        assert "provider hiccuped" in raw
        assert len(calls) == 2

    def test_install_guide_stream_stays_single_attempt(self, monkeypatch):
        # retry=False pipelines (install guide) must not loop
        calls = self.script_stream(monkeypatch, ["guide text"])
        raw, payload = self.collect(spec_ai.stream_install_guide(spec_dict()))
        assert payload["ok"] is True
        assert len(calls) == 1
