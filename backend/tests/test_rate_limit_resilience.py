"""Resilience to a small provider rate limit and to unexpected streaming
failures — the "The stream ended without a result" report.

Two guarantees:

1. Transient provider failures (429 / 5xx / timeout) are retried WITH a real
   exponential backoff, so a small rate limit gets time to clear instead of
   being re-hit the instant it arrives (which spends every attempt on the same
   limit). The keyless ``mock`` provider skips the sleep, so the suite pays no
   wall-clock cost.

2. The streaming pipeline ALWAYS ends with a terminal result envelope: even an
   unexpected (non-``LLMError``) exception mid-stream yields a sentinel+payload
   the frontend can act on, instead of tearing the SSE stream with no sentinel
   (which surfaces to the user as "The stream ended without a result").
"""
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from backend.app import spec_ai  # noqa: E402
from backend.app.llm import LLMError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID = (REPO_ROOT / "examples" / "street_light.json").read_text()


@pytest.fixture(autouse=True)
def mock_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")


def _collect(gen):
    """Split a streamed generator into (raw_text, final_payload). Asserts
    exactly one terminal sentinel was emitted — two would mean a torn stream
    was papered over with a second envelope (unparseable for the frontend)."""
    text = "".join(gen)
    parts = text.split(spec_ai.STREAM_SENTINEL)
    assert len(parts) == 2, f"expected exactly one sentinel, got {len(parts) - 1}"
    return parts[0], json.loads(parts[1])


def _script_stream(monkeypatch, replies):
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


class TestBackoffSchedule:
    def test_exponential_and_capped(self):
        s = spec_ai._transient_backoff_seconds
        assert s(1) == 2.0
        assert s(2) == 4.0
        assert s(3) == 8.0
        assert s(4) == spec_ai._RETRY_BACKOFF_CAP_S  # capped, never runs away
        assert s(99) == spec_ai._RETRY_BACKOFF_CAP_S

    def test_mock_provider_never_sleeps(self, monkeypatch):
        slept = []
        monkeypatch.setattr(spec_ai.time, "sleep", lambda s: slept.append(s))
        spec_ai._backoff_before_retry(1)  # env is mock (autouse) -> skipped
        spec_ai._backoff_before_retry(3)
        assert slept == []

    def test_real_provider_sleeps_on_the_backoff_schedule(self, monkeypatch):
        slept = []
        monkeypatch.setattr(spec_ai.time, "sleep", lambda s: slept.append(s))
        monkeypatch.setenv("LLM_PROVIDER", "deepseek")
        spec_ai._backoff_before_retry(1)
        spec_ai._backoff_before_retry(2)
        assert slept == [2.0, 4.0]


class TestTransientRetryUsesBackoff:
    def test_stream_backoff_between_transient_retries(self, monkeypatch):
        seen = []
        monkeypatch.setattr(spec_ai, "_backoff_before_retry", lambda a: seen.append(a))
        self_calls = _script_stream(
            monkeypatch, [LLMError("LLM provider returned 429: slow down"), VALID])
        raw, payload = _collect(
            spec_ai.stream_refine_spec(json.loads(VALID), "make it taller"))
        assert payload["ok"] is True
        assert len(self_calls) == 2
        assert seen == [1]  # one backoff, before the single retry

    def test_nonstream_backoff_between_transient_retries(self, monkeypatch):
        seen = []
        monkeypatch.setattr(spec_ai, "_backoff_before_retry", lambda a: seen.append(a))
        calls = []

        def fake_complete(system, user, **kwargs):
            calls.append(user)
            if len(calls) == 1:
                raise LLMError("LLM provider returned 429: slow down")
            return VALID

        monkeypatch.setattr(spec_ai, "complete", fake_complete)
        out = spec_ai.refine_spec(json.loads(VALID), "make it taller")
        assert out["spec"]["asset_type"] == "street_light"
        assert len(calls) == 2
        assert seen == [1]

    def test_repeated_rate_limit_still_gives_up_cleanly_with_a_payload(self, monkeypatch):
        # every attempt 429s: the stream must still terminate with a classified
        # provider error envelope (not tear), after MAX_ATTEMPTS.
        monkeypatch.setattr(spec_ai, "_backoff_before_retry", lambda a: None)
        _script_stream(monkeypatch, [LLMError("LLM provider returned 429: slow down")])
        raw, payload = _collect(
            spec_ai.stream_refine_spec(json.loads(VALID), "make it taller"))
        assert payload["ok"] is False
        assert payload["kind"] == "provider"


class TestStreamAlwaysTerminates:
    def test_unexpected_stream_exception_yields_terminal_payload(self, monkeypatch):
        # a NON-LLMError raised by complete_stream used to escape the generator
        # and tear the SSE stream (no sentinel) -> the frontend's "The stream
        # ended without a result". Now it must yield a terminal error envelope.
        def boom_stream(system, user, **kwargs):
            raise RuntimeError("kaboom in the provider client")
            yield  # pragma: no cover — makes this a generator function

        monkeypatch.setattr(spec_ai, "complete_stream", boom_stream)
        raw, payload = _collect(
            spec_ai.stream_refine_spec(json.loads(VALID), "make it taller"))
        assert payload["ok"] is False
        assert payload["kind"] == "unknown"
        assert "kaboom" in payload["error"]

    def test_unexpected_finalize_exception_yields_terminal_payload(self, monkeypatch):
        # finalize raising a non-SpecGenerationError also used to tear the
        # stream; now caught by the last-resort guard in _stream_pipeline.
        def ok_stream(system, user, **kwargs):
            yield "streamed text"

        monkeypatch.setattr(spec_ai, "complete_stream", ok_stream)

        def boom_finalize(raw, lenient=False):
            raise RuntimeError("finalize boom")

        raw, payload = _collect(spec_ai._stream_pipeline("sys", "usr", boom_finalize))
        assert payload["ok"] is False
        assert payload["kind"] == "unknown"
        assert "finalize boom" in payload["error"]

    def test_generate_stream_survives_unexpected_brief_pass_failure(self, monkeypatch):
        # An unexpected (non-LLMError) failure in the design-brief pass must not
        # tear the generate stream: it degrades to designing from the raw
        # request and the spec pass still ships the one terminal payload. The
        # brief pass streams first, then the spec pass — so a scripted sequence
        # of [brief boom, VALID spec] exercises both.
        replies = [RuntimeError("brief pass exploded"), VALID]
        calls = _script_stream(monkeypatch, replies)
        raw, payload = _collect(spec_ai.stream_generate_spec("a park bench"))
        assert payload["ok"] is True
        assert payload["result"]["spec"]["asset_type"] == "street_light"
        assert "designing from your request as-is" in raw
        assert len(calls) == 2
