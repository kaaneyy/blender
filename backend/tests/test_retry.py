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
    _correction_user,
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


def _spec_with_out_of_range_pole_height(claimed_code_mode):
    """street_light has real standards rules (pole_height max 40 ft) so
    clamping is observable; the reply claims ``claimed_code_mode`` — which
    the requested code_mode must override."""
    spec = spec_dict()
    for param in spec["parameters"]:
        if param["id"] == "pole_height":
            param["value"] = 50  # above the 40 ft max
    spec["code_mode"] = claimed_code_mode
    return spec


class TestCodeModeIsAuthoritative:
    """The requested code_mode always wins over whatever code_mode the model
    wrote into its reply — a model can't use that to silently steer strict
    clamping on or off."""

    def test_requested_strict_wins_and_clamps_even_if_reply_says_advisory(self):
        bad = _spec_with_out_of_range_pole_height("advisory")
        out = _postprocess(json.dumps(bad), "strict")
        assert out["spec"]["code_mode"] == "strict"
        pole_height = next(p for p in out["spec"]["parameters"]
                           if p["id"] == "pole_height")
        assert pole_height["value"] == 40.0  # clamped to the code max
        violation = next(v for v in out["violations"]
                         if v.get("parameter_id") == "pole_height")
        assert violation["corrected_value"] == 40.0

    def test_requested_advisory_wins_and_leaves_it_unclamped_even_if_reply_says_strict(self):
        bad = _spec_with_out_of_range_pole_height("strict")
        out = _postprocess(json.dumps(bad), "advisory")
        assert out["spec"]["code_mode"] == "advisory"
        pole_height = next(p for p in out["spec"]["parameters"]
                           if p["id"] == "pole_height")
        assert pole_height["value"] == 50  # NOT clamped
        violation = next(v for v in out["violations"]
                         if v.get("parameter_id") == "pole_height")
        assert violation["corrected_value"] is None
        assert violation["message"]  # still flagged, just not force-fixed


class TestCorrectionEchoStripsReasoning:
    """The corrective prompt's echo of the previous answer must show the
    actual JSON, not a reasoning model's <think> preamble — otherwise the
    6000-char cap can be consumed entirely by thinking text, truncating the
    real answer away and sabotaging the in-place repair."""

    def test_think_block_is_stripped_from_the_echoed_answer(self):
        history = [SpecGenerationError("bad field", kind="schema", hint="fix it")]
        raw = ("<think>{ lots of braces } { { { reasoning reasoning } } } "
               "</think>{\"real\": 1}")
        message = _correction_user("orig prompt", 2, history, raw)
        assert '{"real": 1}' in message
        assert "lots of braces" not in message
        assert "reasoning reasoning" not in message

    def test_truncated_kind_still_skips_the_echo_entirely(self):
        # unchanged behavior: a truncated reply is never echoed back at all,
        # reasoning or not
        history = [SpecGenerationError("cut off", kind="truncated", hint="be compact")]
        raw = "<think>some thinking</think>{\"incomplete\": "
        message = _correction_user("orig prompt", 2, history, raw)
        assert "repair it in place" not in message
        assert "some thinking" not in message


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


def script_complete_kwargs(monkeypatch, replies):
    """Like ``script_complete``, but records each call's full kwargs (not
    just the prompt) so the per-attempt ``max_tokens`` budget can be
    inspected directly."""
    calls = []

    def fake_complete(system, user, **kwargs):
        calls.append(kwargs)
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(spec_ai, "complete", fake_complete)
    return calls


def script_stream_kwargs(monkeypatch, replies):
    """Streaming counterpart of ``script_complete_kwargs``."""
    calls = []

    def fake_stream(system, user, **kwargs):
        calls.append(kwargs)
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


class TestTruncatedTokenEscalation:
    """A "truncated" classified failure means the model ran out of output
    room, not that it made a content mistake — retrying with the SAME
    max_tokens truncates identically every time. The next attempt must call
    the provider with a strictly larger (bounded) budget."""

    def test_truncated_retry_escalates_max_tokens(self, monkeypatch):
        truncated = VALID[: len(VALID) // 2]
        calls = script_complete_kwargs(monkeypatch, [truncated, VALID])
        out = run_refine()
        assert out["spec"]["asset_type"] == "street_light"
        assert len(calls) == 2
        first_tokens = calls[0]["max_tokens"]
        second_tokens = calls[1]["max_tokens"]
        assert second_tokens > first_tokens
        assert second_tokens <= spec_ai.TRUNCATION_MAX_TOKENS_CEILING

    def test_stream_truncated_retry_escalates_max_tokens(self, monkeypatch):
        truncated = VALID[: len(VALID) // 2]
        calls = script_stream_kwargs(monkeypatch, [truncated, VALID])
        raw, payload = collect_stream(
            spec_ai.stream_refine_spec(spec_dict(), "make it taller"))
        assert payload["ok"] is True
        assert len(calls) == 2
        first_tokens = calls[0]["max_tokens"]
        second_tokens = calls[1]["max_tokens"]
        assert second_tokens > first_tokens
        assert second_tokens <= spec_ai.TRUNCATION_MAX_TOKENS_CEILING

    def test_non_truncated_failure_does_not_escalate_max_tokens(self, monkeypatch):
        # garbage -> not_json, then a schema violation -> schema: neither
        # kind is a budget problem, so max_tokens must stay put across all
        # three attempts (a reverted escalation would still pass a test that
        # only checked "some call escalated" — this asserts the concrete
        # equality instead).
        bad_schema = spec_dict()
        bad_schema["evil_extra"] = True
        calls = script_complete_kwargs(
            monkeypatch, ["garbage", json.dumps(bad_schema), VALID])
        run_refine()
        assert len(calls) == 3
        tokens = [c["max_tokens"] for c in calls]
        assert tokens[0] == tokens[1] == tokens[2]

    def test_escalation_never_shrinks_an_explicit_caller_budget(self):
        # propose_standards_update calls _complete_with_retries with
        # max_tokens=8000 — a truncated retry must escalate UP from that,
        # never fall back down toward the 6000 default.
        history = [SpecGenerationError("cut off", kind="truncated")]
        escalated = spec_ai._escalate_truncated_budget(8000, history)
        assert escalated > 8000
        assert escalated <= spec_ai.TRUNCATION_MAX_TOKENS_CEILING

    def test_escalation_is_bounded_by_the_ceiling(self):
        history = [SpecGenerationError("cut off", kind="truncated")]
        near_ceiling = spec_ai.TRUNCATION_MAX_TOKENS_CEILING - 10
        assert spec_ai._escalate_truncated_budget(
            near_ceiling, history) == spec_ai.TRUNCATION_MAX_TOKENS_CEILING

    def test_no_history_leaves_budget_untouched(self):
        assert spec_ai._escalate_truncated_budget(
            spec_ai.DEFAULT_MAX_TOKENS, []) == spec_ai.DEFAULT_MAX_TOKENS
