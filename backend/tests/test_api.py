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

    def test_rejects_unbuildable_geometry(self):
        spec = self._spec()
        spec["asset_type"] = "mystery_prop"  # no builder, no primitives
        with pytest.raises(SpecGenerationError, match="does not build"):
            _postprocess(json.dumps(spec), "strict")

    def test_rejects_non_json(self):
        with pytest.raises(SpecGenerationError, match="not valid JSON"):
            _postprocess("I cannot help with that.", "strict")


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
