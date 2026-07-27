"""Every stream ends with a result — never silently torn.

The frontend reads a stream as "live text, then STREAM_SENTINEL + a result
envelope". If that sentinel never arrives it can only raise "The stream ended
without a result", throwing away every token the run already spent. These
tests pin the two guarantees that stop that happening:

* :func:`spec_ai._with_terminal_sentinel` wraps every streaming endpoint, so an
  exception raised BETWEEN passes (a combine step, a finalizer, a plain bug)
  still reaches the client as a result envelope instead of tearing the stream.
* the layered generate never throws away a spec the layers already built —
  a failing combine step ships the accumulated spec rather than nothing.
"""
import json

import pytest

from backend.app import spec_ai
from backend.app.llm import LLMError
from backend.app.spec_ai import STREAM_SENTINEL, stream_generate_spec

from .test_layered import (  # reuse the layered scripting helpers
    PANEL_REPLY,
    VALID,
    collect_stream,
    mock_provider,  # noqa: F401 — autouse fixture
    script_complete,
    script_stream,
)


def sentinels(text: str) -> int:
    return text.count(STREAM_SENTINEL)


class TestTerminalSentinelWrapper:
    def test_emits_a_sentinel_when_the_inner_stream_raises(self):
        def inner():
            yield "some live text"
            raise RuntimeError("boom mid-stream")

        text = "".join(spec_ai._with_terminal_sentinel(inner()))

        assert sentinels(text) == 1
        raw, payload = text.split(STREAM_SENTINEL)
        assert raw == "some live text"  # streamed text is preserved
        payload = json.loads(payload)
        assert payload["ok"] is False
        assert "boom mid-stream" in payload["error"]

    def test_emits_a_sentinel_when_the_inner_stream_just_ends(self):
        def inner():
            yield "text but no verdict"

        text = "".join(spec_ai._with_terminal_sentinel(inner()))

        assert sentinels(text) == 1
        assert json.loads(text.split(STREAM_SENTINEL)[1])["ok"] is False

    def test_does_not_add_a_second_sentinel(self):
        payload = {"ok": True, "result": {"spec": {}}}

        def inner():
            yield "text"
            yield STREAM_SENTINEL + json.dumps(payload)

        text = "".join(spec_ai._with_terminal_sentinel(inner()))

        assert sentinels(text) == 1
        assert json.loads(text.split(STREAM_SENTINEL)[1]) == payload

    def test_a_raise_after_the_sentinel_does_not_add_another(self):
        def inner():
            yield STREAM_SENTINEL + json.dumps({"ok": True, "result": {}})
            raise RuntimeError("too late to matter")

        text = "".join(spec_ai._with_terminal_sentinel(inner()))

        assert sentinels(text) == 1
        assert json.loads(text.split(STREAM_SENTINEL)[1])["ok"] is True

    def test_client_disconnect_closes_cleanly_without_a_sentinel(self):
        """GeneratorExit is a BaseException, so closing the generator (the
        client went away) must NOT be swallowed into a result nobody reads."""
        emitted = []

        def inner():
            yield "first"
            yield "second"

        gen = spec_ai._with_terminal_sentinel(inner())
        emitted.append(next(gen))
        gen.close()  # client disconnected

        assert emitted == ["first"]


class TestLayeredStreamAlwaysShips:
    def test_combine_failure_still_ships_the_built_spec(self, monkeypatch):
        """The 4 layers each validated and built their spec. If the combine
        step then blows up, that work — and everything it cost — must not be
        thrown away."""
        script_stream(monkeypatch, [PANEL_REPLY, VALID, VALID, VALID, VALID])
        script_complete(monkeypatch, [])

        def boom(*args, **kwargs):
            raise RuntimeError("combine exploded")

        monkeypatch.setattr(spec_ai, "_finalize_layered", boom)

        raw, payload = collect_stream(stream_generate_spec("a street light"))

        assert payload["ok"] is True  # NOT a lost generation
        assert payload["result"]["spec"]["asset_type"] == "street_light"
        assert "combine exploded" in payload["result"]["combine_error"]
        assert [l["status"] for l in payload["result"]["layers"]] == ["built"] * 4

    def test_unexpected_failure_mid_pipeline_still_ends_with_a_result(self, monkeypatch):
        """An exception no per-pass guard expects (here: raised while building
        a layer's prompt) reaches the client as an envelope, not a torn
        stream."""
        script_stream(monkeypatch, [PANEL_REPLY, VALID, VALID, VALID, VALID])
        script_complete(monkeypatch, [])

        def boom(*args, **kwargs):
            raise RuntimeError("prompt builder exploded")

        monkeypatch.setattr(spec_ai, "_layer_user", boom)

        text = "".join(stream_generate_spec("a street light"))

        assert sentinels(text) == 1
        payload = json.loads(text.split(STREAM_SENTINEL)[1])
        assert payload["ok"] is False
        assert "prompt builder exploded" in payload["error"]

    def test_streaming_generate_skips_the_advisory_qa_call(self, monkeypatch):
        """Nothing in the UI reads the QA verdict, and the call sat at the end
        of an already-long run — right where a timeout costs the whole
        generation. The streamed result ships without it."""
        script_stream(monkeypatch, [PANEL_REPLY, VALID, VALID, VALID, VALID])
        calls = script_complete(monkeypatch, [])

        _, payload = collect_stream(stream_generate_spec("a street light"))

        assert calls == []
        assert payload["ok"] is True
        assert "qa" not in payload["result"]


class TestOtherStreamsGuarded:
    def test_refine_stream_survives_a_finalizer_that_explodes(self, monkeypatch):
        """_stream_pipeline drives refine/focus/wizard/improve/review — an
        unexpected finalize failure must still produce one envelope."""
        script_stream(monkeypatch, [VALID])

        def boom(raw, lenient=False):
            raise RuntimeError("finalizer exploded")

        text = "".join(spec_ai._stream_pipeline("sys", "user", boom, retry=False))

        assert sentinels(text) == 1
        assert json.loads(text.split(STREAM_SENTINEL)[1])["ok"] is False

    def test_provider_outage_still_ends_with_a_result(self, monkeypatch):
        def dead(system, user, **kwargs):
            raise LLMError("LLM provider returned 400: bad request")
            yield  # pragma: no cover — generator marker

        monkeypatch.setattr(spec_ai, "complete_stream", dead)

        text = "".join(spec_ai._stream_pipeline("sys", "user", lambda raw, lenient=False: {},
                                                retry=False))

        assert sentinels(text) == 1
        payload = json.loads(text.split(STREAM_SENTINEL)[1])
        assert payload["ok"] is False
        assert payload["kind"] == "provider"


@pytest.mark.parametrize("factory", [
    lambda: spec_ai.stream_refine_spec({"name": "x"}, "make it taller"),
    lambda: spec_ai.stream_focus_spec({"name": "x"}, "the head"),
    lambda: spec_ai.stream_wizard_step({"name": "x"}, "materials"),
    lambda: spec_ai.stream_wizard_step({"name": "x"}, "nonsense-step"),
    lambda: stream_generate_spec("a bench"),
])
def test_every_stream_entry_point_ends_with_exactly_one_sentinel(monkeypatch, factory):
    """Whatever the provider does — here it fails outright — each streaming
    endpoint still terminates with exactly one result envelope."""
    def dead(system, user, **kwargs):
        raise LLMError("LLM provider returned 400: bad request")
        yield  # pragma: no cover — generator marker

    monkeypatch.setattr(spec_ai, "complete_stream", dead)
    monkeypatch.setattr(spec_ai, "complete", lambda *a, **k: (_ for _ in ()).throw(
        LLMError("LLM provider returned 400: bad request")))

    text = "".join(factory())

    assert sentinels(text) == 1
    assert json.loads(text.split(STREAM_SENTINEL)[1])["ok"] is False
