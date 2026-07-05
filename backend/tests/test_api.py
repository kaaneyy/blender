"""Backend API tests. The LLM endpoints run against the keyless `mock`
provider, which returns the bundled example specs."""
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402
from backend.app.spec_ai import SpecGenerationError, _postprocess  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")


def test_health_on_both_mounts():
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/api/health").json() == {"status": "ok"}


def test_generate_spec_returns_valid_spec():
    r = client.post("/api/generate-spec", json={"prompt": "a 30 ft street light"})
    assert r.status_code == 200
    data = r.json()
    assert data["spec"]["asset_type"] == "street_light"
    assert data["checked"] is True


def test_generate_spec_custom_path():
    r = client.post("/api/generate-spec", json={"prompt": "a park bench with backrest"})
    assert r.status_code == 200
    spec = r.json()["spec"]
    assert spec["asset_type"] == "bench"
    assert spec["primitives"], "custom assets carry their geometry in the spec"


def test_refine_spec_revalidates():
    spec = json.loads((REPO_ROOT / "examples" / "street_light.json").read_text())
    r = client.post("/api/refine-spec", json={"spec": spec, "message": "make it taller"})
    assert r.status_code == 200
    assert r.json()["spec"]["asset_type"] == "street_light"


def test_prompt_length_limit():
    r = client.post("/api/generate-spec", json={"prompt": "x" * 3000})
    assert r.status_code == 422


class TestPostprocess:
    def _spec(self):
        return json.loads((REPO_ROOT / "examples" / "street_light.json").read_text())

    def test_strips_markdown_fences_and_prose(self):
        raw = "Here you go:\n```json\n" + json.dumps(self._spec()) + "\n```\nEnjoy!"
        out = _postprocess(raw, "strict")
        assert out["spec"]["asset_type"] == "street_light"

    def test_clamps_out_of_code_values(self):
        spec = self._spec()
        spec["parameters"][0]["value"] = 90
        out = _postprocess(json.dumps(spec), "strict")
        assert out["ok"] is False
        assert out["spec"]["parameters"][0]["value"] == 40

    def test_rejects_unknown_fields(self):
        spec = self._spec()
        spec["evil_extra"] = True
        with pytest.raises(SpecGenerationError, match="Schema violation"):
            _postprocess(json.dumps(spec), "strict")

    def test_accepts_declared_connections(self):
        spec = self._spec()
        spec["connections"] = [
            {"a": "pole", "b": "ground", "type": "anchor_base"},
            {"a": "arm", "b": "pole", "type": "band_clamp", "load": "heavy", "count": 2},
        ]
        out = _postprocess(json.dumps(spec), "strict")
        assert out["spec"]["connections"][0]["type"] == "anchor_base"

    def test_rejects_unknown_connection_type(self):
        spec = self._spec()
        spec["connections"] = [{"a": "pole", "b": "ground", "type": "duct_tape"}]
        with pytest.raises(SpecGenerationError, match="Schema violation"):
            _postprocess(json.dumps(spec), "strict")

    def _floating_spec(self):
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

    def test_floating_part_fails_buildability(self):
        """T2.6 fuel: a floating part raises with the machine finding so the
        retry prompt carries 'component X floats ... Nmm away'."""
        with pytest.raises(SpecGenerationError, match="floats.*mm away"):
            _postprocess(json.dumps(self._floating_spec()), "strict")

    def test_lenient_retry_accepts_floating_with_warnings(self):
        out = _postprocess(json.dumps(self._floating_spec()), "strict",
                           lenient_buildability=True)
        assert out["ok"] is False
        findings = [v for v in out["violations"]
                    if v.get("parameter_id") == "__buildability__"]
        assert findings and findings[0]["severity"] == "error"


def test_validate_spec_reports_buildability():
    spec = json.loads((REPO_ROOT / "examples" / "park_bench.json").read_text())
    spec["primitives"].append({
        "kind": "sphere", "name": "orb", "component": "orb",
        "material_slot": "frame", "location": [0, 0, 3.0],
        "params": {"radius": 0.2},
    })
    r = client.post("/api/validate-spec", json=spec)
    assert r.status_code == 200
    findings = [v for v in r.json()["violations"]
                if v.get("parameter_id") == "__buildability__"]
    assert findings and "floats" in findings[0]["message"]

    def test_rejects_unbuildable_geometry(self):
        spec = self._spec()
        spec["asset_type"] = "mystery_prop"  # no builder, no primitives
        with pytest.raises(SpecGenerationError, match="does not build"):
            _postprocess(json.dumps(spec), "strict")

    def test_rejects_non_json(self):
        with pytest.raises(SpecGenerationError, match="not valid JSON"):
            _postprocess("I cannot help with that.", "strict")

    def test_reasoning_block_before_json(self):
        """A thinking model's <think>…</think> preamble is stripped and the
        JSON that follows still parses."""
        spec = json.dumps(self._spec())
        raw = ("<think>Let me plan this street light. Base plate first, then the "
               "pole {tapered}, then the arm.</think>\nHere is the spec:\n" + spec)
        out = _postprocess(raw, "strict")
        assert out["spec"]["asset_type"] == "street_light"

    def test_braces_in_reasoning_prose_do_not_fool_extraction(self):
        """Untagged reasoning prose full of stray braces before the real answer
        must not break JSON extraction (the last balanced object wins)."""
        spec = json.dumps(self._spec())
        raw = ("Thinking: the height should be around {40} ft and the arm {8} ft, "
               "using a set like {a, b}. Final answer:\n" + spec)
        out = _postprocess(raw, "strict")
        assert out["spec"]["asset_type"] == "street_light"

    def test_truncated_reasoning_fails_cleanly(self):
        """A response cut off mid-thought (unclosed <think>) has no answer, so
        it fails as invalid JSON rather than silently mis-parsing."""
        with pytest.raises(SpecGenerationError, match="not valid JSON"):
            _postprocess("<think>Still working through the geometry, first the",
                         "strict")


class TestReasoningModel:
    def test_strip_reasoning_removes_think_blocks(self):
        from backend.app.llm import strip_reasoning

        assert strip_reasoning("<think>a</think>ANSWER") == "ANSWER"
        # unterminated block (truncated) is dropped from its opening tag on
        assert strip_reasoning("done<think>still going") == "done"
        assert strip_reasoning("no tags here") == "no tags here"

    def test_reasoning_model_gets_longer_timeout_and_more_tokens(self):
        from backend.app.llm import (
            _budget, is_reasoning_model, REASONING_TIMEOUT,
            REASONING_MIN_TOKENS, TIMEOUT,
        )

        assert is_reasoning_model("deepseek-v4-pro")
        assert not is_reasoning_model("deepseek-chat")
        # reasoning model: longer timeout and a floor on the token budget
        assert _budget("deepseek-v4-pro", 6000) == (REASONING_TIMEOUT, REASONING_MIN_TOKENS)
        assert _budget("deepseek-v4-pro", 20000) == (REASONING_TIMEOUT, 20000)
        # plain model: defaults, budget untouched
        assert _budget("deepseek-chat", 6000) == (TIMEOUT, 6000)


def test_install_guide_mock():
    spec = json.loads((REPO_ROOT / "examples" / "street_light.json").read_text())
    r = client.post("/api/install-guide", json={"spec": spec})
    assert r.status_code == 200
    guide = r.json()["guide"]
    assert "## Assembly sequence" in guide and "licensed engineer" in guide


def test_update_standards_mock_without_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    r = client.post("/api/update-standards")
    assert r.status_code == 200
    data = r.json()
    assert data["committed"] is False
    assert "GITHUB_TOKEN" in data["detail"]
    assert any("bike_rack" in c for c in data["changes"])
    from standards.validator import validate_standards_db
    assert validate_standards_db(data["proposal"]) == []


SENTINEL = "<<<ASSETFORGE_RESULT>>>"


def stream_payload(resp):
    assert resp.status_code == 200
    assert SENTINEL in resp.text
    return json.loads(resp.text.split(SENTINEL)[-1])


def test_generate_spec_stream():
    r = client.post("/api/generate-spec-stream", json={"prompt": "a park bench"})
    payload = stream_payload(r)
    assert payload["ok"] is True
    assert payload["result"]["spec"]["asset_type"] == "bench"
    # raw text streamed before the sentinel
    assert '"asset_type"' in r.text.split(SENTINEL)[0]


def test_install_guide_stream():
    spec = json.loads((REPO_ROOT / "examples" / "street_light.json").read_text())
    r = client.post("/api/install-guide-stream", json={"spec": spec})
    payload = stream_payload(r)
    assert payload["ok"] is True
    assert "Assembly sequence" in payload["result"]["guide"]


def test_update_standards_stream(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    r = client.post("/api/update-standards-stream")
    payload = stream_payload(r)
    assert payload["ok"] is True
    assert payload["result"]["committed"] is False
    assert any("bike_rack" in c for c in payload["result"]["changes"])


def test_prompt_enhancer_layer():
    """The extra AI pass: vague request -> design brief -> spec, with the
    brief returned so the UI can show the interpretation."""
    from backend.app.spec_ai import enhance_prompt

    brief = enhance_prompt("a lamp")
    assert "a lamp" in brief and len(brief) > len("a lamp")

    r = client.post("/api/generate-spec", json={"prompt": "a park bench"})
    assert r.status_code == 200
    data = r.json()
    assert data["brief"].startswith("Design brief: a park bench")
    assert data["spec"]["asset_type"] == "bench"


def test_generate_stream_has_brief_stages():
    r = client.post("/api/generate-spec-stream", json={"prompt": "a park bench"})
    payload = stream_payload(r)
    raw = r.text.split(SENTINEL)[0]
    assert "[refining your request into a design brief]" in raw
    assert "[designing the asset from the brief]" in raw
    assert payload["ok"] is True
    assert payload["result"]["brief"].startswith("Design brief:")
    assert payload["result"]["spec"]["asset_type"] == "bench"


def test_model_dropdown_allowlist():
    """resolve_model: only the three DeepSeek ids pass through; junk is
    ignored (falls back to the env/default), so a client can't inject one."""
    from backend.app.llm import resolve_model, DEFAULT_DEEPSEEK_MODEL

    assert resolve_model("deepseek", "deepseek-v4-pro") == "deepseek-v4-pro"
    assert resolve_model("deepseek", "deepseek-v4-flash") == "deepseek-v4-flash"
    assert resolve_model("deepseek", "evil-model") == DEFAULT_DEEPSEEK_MODEL
    assert resolve_model("deepseek", "") == DEFAULT_DEEPSEEK_MODEL
    assert resolve_model("deepseek", None) == DEFAULT_DEEPSEEK_MODEL


def test_generate_accepts_model_field():
    r = client.post("/api/generate-spec",
                    json={"prompt": "a park bench", "model": "deepseek-v4-pro"})
    assert r.status_code == 200
    assert r.json()["spec"]["asset_type"] == "bench"


def test_generate_rejects_unknown_model():
    r = client.post("/api/generate-spec",
                    json={"prompt": "a park bench", "model": "gpt-4o"})
    assert r.status_code == 422  # pattern rejects non-DeepSeek ids


def test_focus_spec_returns_valid_spec():
    spec = json.loads((REPO_ROOT / "examples" / "street_light.json").read_text())
    r = client.post("/api/focus-spec",
                    json={"spec": spec, "area": "the luminaire head"})
    assert r.status_code == 200
    data = r.json()
    assert data["spec"]["asset_type"] == "street_light"
    assert data["checked"] is True


def test_focus_spec_stream():
    spec = json.loads((REPO_ROOT / "examples" / "street_light.json").read_text())
    r = client.post("/api/focus-spec-stream",
                    json={"spec": spec, "area": "the base flange", "model": "deepseek-v4-flash"})
    payload = stream_payload(r)
    assert payload["ok"] is True
    assert payload["result"]["spec"]["asset_type"] == "street_light"
