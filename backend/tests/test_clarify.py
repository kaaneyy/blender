"""Tests for the clarify-before-generate pass: 4 persona-tagged AI-written
questions (one from each of the same four personas that review assets under
the Improve button — architecture, mechanical, civil, design) x 3 offered
answers (plus the user's own typed answer) that surface the real request
behind a basic one. The questions ride the classified retry engine, are
sanitized/reordered into a fixed persona-ordered shape, and the answered
pairs are folded into the generation request ahead of the four-persona
design-panel brief pass, which also carries its own "panel" takes into the
generation prompt when it parses."""
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from backend.app import spec_ai  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.spec_ai import (  # noqa: E402
    CLARIFY_MIN_QUESTIONS,
    CLARIFY_OPTIONS,
    CLARIFY_QUESTIONS,
    MAX_ATTEMPTS,
    MAX_CLARIFICATIONS,
    PERSONA_ORDER,
    SpecGenerationError,
    _clarified_prompt,
    _clarify_finalize,
    _design_panel,
    _finalize_panel,
    clarify_request,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")


def questions_json(n_questions=CLARIFY_QUESTIONS, n_options=CLARIFY_OPTIONS, personas=None):
    """``personas`` is an optional list (parallel to the questions, by
    index) of persona ids to tag each question with; ``None``/missing
    entries stay untagged."""
    personas = personas or []
    questions = []
    for q in range(n_questions):
        entry = {"question": f"Question {q}?",
                 "options": [f"Answer {q}.{o}" for o in range(n_options)]}
        pid = personas[q] if q < len(personas) else None
        if pid:
            entry["persona"] = pid
        questions.append(entry)
    return json.dumps({"questions": questions})


def panel_json(personas=PERSONA_ORDER, brief="A synthesized brief for the request."):
    return json.dumps({
        "brief": brief,
        "panel": [{"id": pid, "take": f"{pid} take: worth noting."} for pid in personas],
    })


class TestSanitization:
    def test_valid_questions_get_stable_ids(self):
        out = _clarify_finalize(questions_json())
        assert [q["id"] for q in out["questions"]] == ["q1", "q2", "q3", "q4"]
        assert all(len(q["options"]) == CLARIFY_OPTIONS for q in out["questions"])

    def test_extra_questions_and_options_are_trimmed(self):
        out = _clarify_finalize(questions_json(n_questions=5, n_options=6))
        assert len(out["questions"]) == CLARIFY_QUESTIONS
        assert all(len(q["options"]) == CLARIFY_OPTIONS for q in out["questions"])

    def test_duplicate_options_disqualify_a_question(self):
        raw = json.dumps({"questions": [
            {"question": "Q?", "options": ["Same", "same", "SAME"]},
            {"question": "Q2?", "options": ["A", "B", "C"]},
            {"question": "Q3?", "options": ["A", "B", "C"]},
        ]})
        with pytest.raises(SpecGenerationError) as err:
            _clarify_finalize(raw)
        assert err.value.kind == "schema"

    def test_long_text_is_capped(self):
        raw = json.dumps({"questions": [
            {"question": "Q" * 400, "options": ["A" * 200, "B", "C"]},
            {"question": "Q2?", "options": ["A", "B", "C"]},
            {"question": "Q3?", "options": ["A", "B", "C"]},
        ]})
        out = _clarify_finalize(raw)
        assert len(out["questions"][0]["question"]) <= 160
        assert all(len(o) <= 80 for o in out["questions"][0]["options"])

    def test_fenced_answer_is_accepted(self):
        out = _clarify_finalize(f"```json\n{questions_json()}\n```")
        assert len(out["questions"]) == CLARIFY_QUESTIONS

    def test_prose_answer_is_classified_not_json(self):
        with pytest.raises(SpecGenerationError) as err:
            _clarify_finalize("Sure! Here are some questions you could ask.")
        assert err.value.kind == "not_json"

    def test_truncated_answer_is_classified(self):
        with pytest.raises(SpecGenerationError) as err:
            _clarify_finalize(questions_json()[:60])
        assert err.value.kind == "truncated"

    def test_missing_questions_array_is_schema_error(self):
        with pytest.raises(SpecGenerationError) as err:
            _clarify_finalize(json.dumps({"quiz": []}))
        assert err.value.kind == "schema"


class TestPersonaTagging:
    """Persona identity (id/label/icon) rides in ``PERSONA_ORDER``, and the
    finalize step must fail the round if that tagging or ordering is
    dropped — these tests exist specifically to catch that regression."""

    def test_tagged_questions_are_reordered_into_persona_order(self):
        # scrambled order in the raw reply: mechanical, design, architecture, civil
        scrambled = ["mechanical", "design", "architecture", "civil"]
        out = _clarify_finalize(questions_json(personas=scrambled))
        ids = [q["persona"]["id"] for q in out["questions"]]
        assert ids == list(PERSONA_ORDER)
        # every entry carries a full persona identity, not just an id
        for q in out["questions"]:
            assert q["persona"]["label"]
            assert q["persona"]["icon"]

    def test_unknown_persona_id_degrades_without_dropping_the_question(self):
        raw = questions_json(personas=["architecture", "mechanical", "not_a_real_persona", "design"])
        out = _clarify_finalize(raw)
        assert len(out["questions"]) == CLARIFY_QUESTIONS
        tagged = [q for q in out["questions"] if "persona" in q]
        untagged = [q for q in out["questions"] if "persona" not in q]
        assert {q["persona"]["id"] for q in tagged} == {"architecture", "mechanical", "design"}
        assert len(untagged) == 1
        assert untagged[0]["question"]  # the question text survives, just untagged

    def test_duplicate_persona_id_drops_the_repeat_tag_only(self):
        raw = questions_json(personas=["architecture", "architecture", "mechanical", "civil"])
        out = _clarify_finalize(raw)
        tagged_ids = [q["persona"]["id"] for q in out["questions"] if "persona" in q]
        # architecture claimed once (by the first occurrence), not twice
        assert tagged_ids.count("architecture") == 1
        assert sum("persona" not in q for q in out["questions"]) == 1

    def test_three_valid_questions_survive_without_a_retry(self):
        # only 3 of the 4 personas show up (design missing entirely) — this
        # must NOT raise, per the >=CLARIFY_MIN_QUESTIONS tolerance.
        out = _clarify_finalize(questions_json(
            n_questions=3, personas=["architecture", "mechanical", "civil"]))
        assert len(out["questions"]) == 3
        assert [q["persona"]["id"] for q in out["questions"]] == [
            "architecture", "mechanical", "civil",
        ]

    def test_below_minimum_still_raises(self):
        raw = json.dumps({"questions": [
            {"question": "Q1?", "options": ["A", "B", "C"], "persona": "architecture"},
            {"question": "Q2?", "options": ["A", "B", "C"], "persona": "mechanical"},
        ]})
        with pytest.raises(SpecGenerationError) as err:
            _clarify_finalize(raw)
        assert err.value.kind == "schema"
        assert CLARIFY_MIN_QUESTIONS == 3


class TestRetryIntegration:
    def test_recovers_on_second_attempt(self, monkeypatch):
        answers = iter(["not json at all", questions_json()])
        calls = []

        def fake_complete(system, user, **kw):
            calls.append(user)
            return next(answers)

        monkeypatch.setattr(spec_ai, "complete", fake_complete)
        out = clarify_request("a bench")
        assert len(out["questions"]) == CLARIFY_QUESTIONS
        assert len(calls) == 2
        assert "not_json" in calls[1]  # the correction names the failure

    def test_gives_up_after_max_attempts(self, monkeypatch):
        monkeypatch.setattr(spec_ai, "complete", lambda *a, **k: "still not json")
        with pytest.raises(SpecGenerationError):
            clarify_request("a bench")


class TestClarifiedPrompt:
    def test_no_answers_passes_prompt_through(self):
        assert _clarified_prompt("a bench", None) == "a bench"
        assert _clarified_prompt("a bench", []) == "a bench"
        assert _clarified_prompt("a bench", [{"question": "Q?", "answer": " "}]) == "a bench"

    def test_answers_are_folded_with_the_honor_instruction(self):
        out = _clarified_prompt("a bench", [
            {"question": "Who uses it?", "answer": "Children at a playground"},
        ])
        assert out.startswith("a bench")
        assert "honor EVERY answer" in out
        assert "Who uses it? → Children at a playground" in out

    def test_caps_and_junk_tolerance(self):
        many = [{"question": f"Q{i}?", "answer": f"A{i}"} for i in range(10)]
        out = _clarified_prompt("a bench", ["junk", *many])
        assert sum(1 for line in out.splitlines() if line.startswith("- ")) == MAX_CLARIFICATIONS
        long = _clarified_prompt("a bench", [{"question": "Q" * 400, "answer": "A" * 400}])
        assert "Q" * 201 not in long and "A" * 201 not in long  # capped at 200 each


class TestDesignPanel:
    """The four-persona brief pass: one AI call returning
    {"brief": ..., "panel": [...]}. Success attaches an ordered "panel" to
    the generation result AND steers the generation prompt with the takes;
    failure/degradation falls back to a plain brief with no "panel" key and
    must never raise."""

    def test_panel_parse_success_returns_ordered_panel_and_a_synthesized_brief(self):
        brief, panel = _finalize_panel(panel_json(), "a bench")
        assert brief == "A synthesized brief for the request."
        assert [p["id"] for p in panel] == list(PERSONA_ORDER)
        assert all(p["take"] and p["label"] and p["icon"] for p in panel)

    def test_unparseable_json_falls_back_to_plain_brief(self):
        brief, panel = _finalize_panel("just some prose, not JSON", "a bench")
        assert brief == "just some prose, not JSON"
        assert panel is None

    def test_empty_reply_falls_back_to_the_raw_prompt(self):
        brief, panel = _finalize_panel("", "a bench")
        assert brief == "a bench"
        assert panel is None

    def test_partial_panel_keeps_the_brief_but_drops_the_panel(self):
        # only 3 of 4 personas present -> the whole panel is dropped, but a
        # brief that DID parse is still honored
        partial = json.dumps({
            "brief": "Still a usable brief.",
            "panel": [{"id": pid, "take": "ok"} for pid in ("architecture", "mechanical", "civil")],
        })
        brief, panel = _finalize_panel(partial, "a bench")
        assert brief == "Still a usable brief."
        assert panel is None

    def test_panel_parse_success_steers_generation_prompt(self, monkeypatch):
        calls = []

        def fake_complete(system, user, **kw):
            calls.append(user)
            if user.startswith("ENHANCE PROMPT"):
                return panel_json(brief="A cast-iron bench with slatted seat.")
            return spec_ai.FEW_SHOT_CUSTOM

        monkeypatch.setattr(spec_ai, "complete", fake_complete)
        result = spec_ai.generate_spec("a bench")

        assert [p["id"] for p in result["panel"]] == list(PERSONA_ORDER)
        assert result["brief"] == "A cast-iron bench with slatted seat."
        generation_call = calls[-1]
        assert generation_call.startswith("Request:")
        assert any(p["take"] in generation_call for p in result["panel"])

    def test_panel_parse_failure_proceeds_with_fallback_brief_no_exception(self, monkeypatch):
        calls = []

        def fake_complete(system, user, **kw):
            calls.append(user)
            if user.startswith("ENHANCE PROMPT"):
                return "not json at all, just a plain brief"
            return spec_ai.FEW_SHOT_CUSTOM

        monkeypatch.setattr(spec_ai, "complete", fake_complete)
        result = spec_ai.generate_spec("a bench")  # must not raise

        assert "panel" not in result
        assert result["brief"] == "not json at all, just a plain brief"

    def test_provider_error_on_panel_pass_falls_back_to_raw_prompt(self, monkeypatch):
        def raising_complete(system, user, **kw):
            raise spec_ai.LLMError("boom")

        monkeypatch.setattr(spec_ai, "complete", raising_complete)
        brief, panel = _design_panel("a bench")
        assert brief == "a bench"
        assert panel is None


class TestEndpoints:
    def test_clarify_endpoint_returns_usable_persona_shaped_questions(self):
        resp = client.post("/clarify-request", json={"prompt": "a bench"})
        assert resp.status_code == 200
        questions = resp.json()["questions"]
        # the keyless mock provider predates persona tagging (3 untagged
        # questions) — the tolerant finalize must still accept it rather
        # than erroring, proving the passthrough survives a degraded parse.
        assert CLARIFY_MIN_QUESTIONS <= len(questions) <= CLARIFY_QUESTIONS
        for q in questions:
            assert q["id"] and q["question"]
            assert len(q["options"]) == CLARIFY_OPTIONS

    def test_clarify_endpoint_passes_persona_tags_through(self, monkeypatch):
        monkeypatch.setattr(spec_ai, "complete",
                            lambda *a, **k: questions_json(personas=list(PERSONA_ORDER)))
        resp = client.post("/clarify-request", json={"prompt": "a bench"})
        assert resp.status_code == 200
        questions = resp.json()["questions"]
        assert len(questions) == CLARIFY_QUESTIONS
        assert [q["persona"]["id"] for q in questions] == list(PERSONA_ORDER)
        for q in questions:
            assert q["persona"]["label"] and q["persona"]["icon"]

    def test_generate_folds_answers_into_the_brief(self):
        resp = client.post("/generate-spec", json={
            "prompt": "a bench",
            "clarifications": [
                {"question": "Who uses it?", "answer": "Children at a playground"},
            ],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["spec"]["asset_type"]
        # the mock brief echoes the request — the answer must have reached it
        assert "Children at a playground" in data["brief"]

    def test_generate_endpoint_carries_panel_when_it_parses(self, monkeypatch):
        def fake_complete(system, user, **kw):
            if user.startswith("ENHANCE PROMPT"):
                return panel_json()
            return spec_ai.FEW_SHOT_CUSTOM

        monkeypatch.setattr(spec_ai, "complete", fake_complete)
        resp = client.post("/generate-spec", json={"prompt": "a bench"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["panel"]) == CLARIFY_QUESTIONS
        assert [p["id"] for p in data["panel"]] == list(PERSONA_ORDER)
        for p in data["panel"]:
            assert p["label"] and p["icon"] and p["take"]

    def test_generate_stream_carries_answers(self):
        resp = client.post("/generate-spec-stream", json={
            "prompt": "a bench",
            "clarifications": [
                {"question": "Style?", "answer": "Classic cast iron with wood"},
            ],
        })
        assert resp.status_code == 200
        text = resp.text
        assert "Classic cast iron with wood" in text  # streamed brief pass
        payload = json.loads(text.split(spec_ai.STREAM_SENTINEL)[-1])
        assert payload["ok"] is True

    def test_generate_rejects_oversized_clarification_lists(self):
        resp = client.post("/generate-spec", json={
            "prompt": "a bench",
            "clarifications": [
                {"question": f"Q{i}?", "answer": "A"} for i in range(7)
            ],
        })
        assert resp.status_code == 422

    def test_clarify_failure_is_a_5xx_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(spec_ai, "complete", lambda *a, **k: "never json")
        resp = client.post("/clarify-request", json={"prompt": "a bench"})
        assert resp.status_code == 502


def test_max_attempts_shared_with_generation():
    """The clarify pass rides the same retry engine as spec generation."""
    assert MAX_ATTEMPTS == 3
