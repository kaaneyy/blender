"""Tests for the clarify-before-generate pass: 3 AI-written questions x 3
offered answers (plus the user's own typed answer) that surface the real
request behind a basic one. The questions ride the classified retry engine,
are sanitized to a fixed 3x3 shape, and the answered pairs are folded into
the generation request ahead of the design-brief pass."""
import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from backend.app import spec_ai  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.spec_ai import (  # noqa: E402
    CLARIFY_OPTIONS,
    CLARIFY_QUESTIONS,
    MAX_ATTEMPTS,
    MAX_CLARIFICATIONS,
    SpecGenerationError,
    _clarified_prompt,
    _clarify_finalize,
    clarify_request,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def mock_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")


def questions_json(n_questions=3, n_options=3):
    return json.dumps({
        "questions": [
            {"question": f"Question {q}?",
             "options": [f"Answer {q}.{o}" for o in range(n_options)]}
            for q in range(n_questions)
        ]
    })


class TestSanitization:
    def test_valid_questions_get_stable_ids(self):
        out = _clarify_finalize(questions_json())
        assert [q["id"] for q in out["questions"]] == ["q1", "q2", "q3"]
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


class TestEndpoints:
    def test_clarify_endpoint_returns_three_by_three(self):
        resp = client.post("/clarify-request", json={"prompt": "a bench"})
        assert resp.status_code == 200
        questions = resp.json()["questions"]
        assert len(questions) == CLARIFY_QUESTIONS
        for q in questions:
            assert q["id"] and q["question"]
            assert len(q["options"]) == CLARIFY_OPTIONS

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
