"""The 4-layer generation pipeline (the default generate).

generate_spec / stream_generate_spec build an asset with four specialized
calls in dependency order — STRUCTURE → FUNCTION → CONNECTIONS → MATERIALS —
each building on the accumulated spec, combined into one validated product
with a final ADVISORY QA review. Layer 1 is a generate; layers 2-4 are scoped
edits that degrade gracefully (a layer that can't build is skipped and the
build carries on, recorded in the "layers" trace). See
spec_ai._generate_layered / stream_generate_spec.
"""
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from backend.app import spec_ai  # noqa: E402
from backend.app.llm import LLMError  # noqa: E402
from backend.app.spec_ai import (  # noqa: E402
    LAYER_ORDER,
    SpecGenerationError,
    generate_spec,
    stream_generate_spec,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID = (REPO_ROOT / "examples" / "street_light.json").read_text()
PANEL_REPLY = "Design brief: a galvanized steel street light."
QA_APPROVE = json.dumps({"verdict": "approve", "problems": [], "fixes": []})
#: a clean non-stream run: brief + 4 layer specs + QA = 6 complete() calls.
CLEAN = [PANEL_REPLY, VALID, VALID, VALID, VALID, QA_APPROVE]
NON_TRANSIENT = LLMError("LLM provider returned 400: bad request")  # not retried


@pytest.fixture(autouse=True)
def mock_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")


def script_complete(monkeypatch, replies):
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


class TestShape:
    def test_four_layers_in_order(self):
        assert LAYER_ORDER == ("structure", "function", "connections", "materials")

    def test_directives_and_labels_present(self):
        for layer in LAYER_ORDER:
            assert layer in spec_ai._LAYER_DIRECTIVES
            assert layer in spec_ai._LAYER_LABEL
        assert "LAYER 1 of 4" in spec_ai._LAYER_DIRECTIVES["structure"]
        assert "LAYER 4 of 4" in spec_ai._LAYER_DIRECTIVES["materials"]


class TestOrderAndAccumulation:
    def test_layer_calls_carry_prior_spec_and_directive(self, monkeypatch):
        calls = script_complete(monkeypatch, CLEAN)
        generate_spec("a street light")

        assert len(calls) == 6  # brief + 4 layers + QA
        # layer 1 = STRUCTURE generate: its own directive, but NO prior spec
        assert "LAYER 1 of 4" in calls[1] and "current AssetSpec" not in calls[1]
        # layers 2-4 = edits, each carrying the accumulated spec + its directive
        assert "current AssetSpec" in calls[2] and "LAYER 2 of 4" in calls[2]
        assert "current AssetSpec" in calls[3] and "LAYER 3 of 4" in calls[3]
        assert "current AssetSpec" in calls[4] and "LAYER 4 of 4" in calls[4]
        assert calls[5].startswith("QA REVIEW.")  # combine step

    def test_clean_run_builds_valid_spec_with_trace(self, monkeypatch):
        script_complete(monkeypatch, CLEAN)
        out = generate_spec("a street light")

        assert out["spec"]["asset_type"] == "street_light"
        assert [l["layer"] for l in out["layers"]] == list(LAYER_ORDER)
        assert [l["status"] for l in out["layers"]] == ["built"] * 4
        assert out["qa"]["verdict"] == "approved"
        assert out["brief"].startswith("Design brief")


class TestGracefulDegradation:
    def test_middle_layer_failure_is_skipped_and_build_ships(self, monkeypatch):
        # CONNECTIONS (the 4th complete call) fails; structure + function +
        # materials still ship, and the connections layer is marked skipped.
        calls = script_complete(monkeypatch, [
            PANEL_REPLY, VALID, VALID, NON_TRANSIENT, VALID, QA_APPROVE,
        ])
        out = generate_spec("a street light")

        assert len(calls) == 6  # non-transient fails in one call, no retry
        assert out["spec"]["asset_type"] == "street_light"  # build still ships
        by_layer = {l["layer"]: l["status"] for l in out["layers"]}
        assert by_layer == {"structure": "built", "function": "built",
                            "connections": "skipped", "materials": "built"}

    def test_structure_layer_failure_fails_generation(self, monkeypatch):
        # layer 1 has nothing to fall back to — a persistent structure failure
        # raises, exactly like the old single-shot generate.
        script_complete(monkeypatch, [PANEL_REPLY, "not a spec at all"])
        with pytest.raises(SpecGenerationError):
            generate_spec("a street light")


class TestStreaming:
    def test_streams_a_stage_per_layer_and_one_sentinel(self, monkeypatch):
        # the STREAMING path skips the advisory QA call (nothing reads its
        # verdict, and it sat on the critical path where a timeout cuts the
        # connection) — so the 5 streamed replies are the whole run.
        script_stream(monkeypatch, [PANEL_REPLY, VALID, VALID, VALID, VALID])
        calls = script_complete(monkeypatch, [])

        raw, payload = collect_stream(stream_generate_spec("a street light"))

        assert calls == []  # no extra non-streamed provider call

        for stage in ("layer 1/4 — structure", "layer 2/4 — function",
                      "layer 3/4 — connections", "layer 4/4 — materials & finish",
                      "combining the layers"):
            assert stage in raw
        assert payload["ok"] is True
        assert [l["status"] for l in payload["result"]["layers"]] == ["built"] * 4

    def test_stream_middle_layer_skipped_still_ships(self, monkeypatch):
        # brief, structure, function, connections(fail), materials
        script_stream(monkeypatch, [PANEL_REPLY, VALID, VALID, NON_TRANSIENT, VALID])
        script_complete(monkeypatch, [])

        raw, payload = collect_stream(stream_generate_spec("a street light"))

        assert payload["ok"] is True
        assert "skipped — keeping the build so far" in raw
        by_layer = {l["layer"]: l["status"] for l in payload["result"]["layers"]}
        assert by_layer["connections"] == "skipped"
        assert by_layer["materials"] == "built"
