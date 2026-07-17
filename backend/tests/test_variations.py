"""Tests for /variations-spec: N INDEPENDENT single-spec edit calls (each
through the ordinary ``_run_edit`` retry/validation pipeline, integration
gate on) steered by a fixed, ordered tuple of perturbation directives
(``spec_ai._VARIATION_DIRECTIVES``) — never one call asked to return several
specs. A variant that exhausts MAX_ATTEMPTS is dropped; the endpoint returns
only the survivors, and zero survivors raises."""
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from backend.app import spec_ai  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.spec_ai import (  # noqa: E402
    MAX_ATTEMPTS,
    SpecGenerationError,
    _VARIATION_DIRECTIVES,
    variations_spec,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID = (REPO_ROOT / "examples" / "street_light.json").read_text()
client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")


def spec_dict():
    return json.loads(VALID)


def script_complete(monkeypatch, replies):
    """Replace spec_ai.complete with a scripted sequence of replies (one per
    call, in order across ALL variants; the last reply repeats if the
    combined loop outruns the list). Returns the list of user messages
    actually sent — mirrors test_improve.py's helper of the same name."""
    calls = []

    def fake_complete(system, user, **kwargs):
        calls.append(user)
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(spec_ai, "complete", fake_complete)
    return calls


class TestVariationsSpec:
    @pytest.mark.parametrize("count", [2, 4])
    def test_returns_count_variants(self, monkeypatch, count):
        script_complete(monkeypatch, [VALID] * count)
        out = variations_spec(spec_dict(), count=count)
        assert len(out["variants"]) == count
        for v in out["variants"]:
            assert v["label"]
            assert isinstance(v["label"], str)
            assert isinstance(v["violations"], list)
            assert v["spec"]["asset_type"] == "street_light"

    def test_calls_embed_base_spec_and_directives_diverge(self, monkeypatch):
        base = spec_dict()
        count = 4
        calls = script_complete(monkeypatch, [VALID] * count)
        out = variations_spec(base, count=count)
        assert len(calls) == count

        base_json = json.dumps(base, separators=(",", ":"))
        expected_labels = [_VARIATION_DIRECTIVES[i][0] for i in range(count)]
        expected_directives = [_VARIATION_DIRECTIVES[i][1] for i in range(count)]

        # every call embeds the base spec verbatim
        for call in calls:
            assert base_json in call

        # each call carries EXACTLY its own directive, and none of the
        # others' — a guard that would BREAK if the directives were
        # collapsed to one shared string across all N calls (divergence,
        # not just count)
        for i, call in enumerate(calls):
            assert expected_directives[i] in call
            for j, other_directive in enumerate(expected_directives):
                if j != i:
                    assert other_directive not in call

        assert [v["label"] for v in out["variants"]] == expected_labels

    def test_variant_dims_are_code_clamped(self, monkeypatch):
        # street_light.json's first parameter is pole_height (ft); push it
        # out of code range the same way test_api.py's
        # test_clamps_out_of_code_values does, and have the scripted AI
        # reply echo that out-of-code value back — strict mode must clamp
        # it in every surviving variant, same as any other edit call.
        out_of_code = spec_dict()
        out_of_code["parameters"][0]["value"] = 90
        count = 2
        script_complete(monkeypatch, [json.dumps(out_of_code)] * count)
        out = variations_spec(spec_dict(), count=count, code_mode="strict")
        assert len(out["variants"]) == count
        for v in out["variants"]:
            assert v["spec"]["parameters"][0]["value"] == 40
            assert v["violations"], "clamping an out-of-code value must record a violation"
            assert "changes" in v

    def test_garbage_only_variant_is_dropped_while_siblings_survive(self, monkeypatch):
        # the first variant returns unparseable garbage on EVERY attempt (it
        # burns MAX_ATTEMPTS calls and then gets dropped); the remaining
        # three variants each succeed on their first call.
        replies = ["not json at all"] * MAX_ATTEMPTS + [VALID, VALID, VALID]
        calls = script_complete(monkeypatch, replies)
        out = variations_spec(spec_dict(), count=4)
        assert len(out["variants"]) == 3
        assert len(calls) == MAX_ATTEMPTS + 3
        # the survivors are the LAST three directives (index 0's variant was
        # dropped) — labels prove which one got dropped, not just a count
        expected_labels = [_VARIATION_DIRECTIVES[i][0] for i in (1, 2, 3)]
        assert [v["label"] for v in out["variants"]] == expected_labels

    def test_zero_survivors_raises(self, monkeypatch):
        script_complete(monkeypatch, ["not json at all"])
        with pytest.raises(SpecGenerationError):
            variations_spec(spec_dict(), count=2)


class TestVariationsEndpoint:
    @pytest.mark.parametrize("count", [2, 4])
    def test_endpoint_returns_count_variants(self, count):
        r = client.post(
            "/api/variations-spec", json={"spec": spec_dict(), "count": count}
        )
        assert r.status_code == 200
        data = r.json()
        assert len(data["variants"]) == count
        for v in data["variants"]:
            assert v["label"]
            assert "violations" in v

    def test_default_count_is_four(self):
        r = client.post("/api/variations-spec", json={"spec": spec_dict()})
        assert r.status_code == 200
        assert len(r.json()["variants"]) == 4

    def test_unknown_model_rejected(self):
        r = client.post(
            "/api/variations-spec",
            json={"spec": spec_dict(), "model": "gpt-4o"},
        )
        assert r.status_code == 422

    @pytest.mark.parametrize("count", [1, 7])
    def test_count_out_of_bounds_rejected(self, count):
        r = client.post(
            "/api/variations-spec", json={"spec": spec_dict(), "count": count}
        )
        assert r.status_code == 422

    def test_zero_survivors_returns_502(self, monkeypatch):
        script_complete(monkeypatch, ["not json at all"])
        r = client.post(
            "/api/variations-spec", json={"spec": spec_dict(), "count": 2}
        )
        assert r.status_code == 502
