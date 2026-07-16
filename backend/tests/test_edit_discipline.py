"""Edit-discipline enforcement for the AI edit tools (refine / wizard
Form-Joints-Materials-Details / improve):

1. ``spec_changes`` — a pure, deterministic before→after AssetSpec diff
   (added/removed/changed components, changed parameter/toggle ids, a human
   summary) attached to every edit response as "changes".
2. The materials wizard step is mechanically barred from changing geometry
   (``_enforce_materials_scope``) — the prompt's "geometry is read-only"
   promise is enforced, not just asked for.
3. Results with parts rammed through existing geometry get a targeted retry
   (``_enforce_integration`` over the sibling ``check_embedded_parts``
   checker, imported LAZILY — it does not exist in this tree yet, built
   independently in a separate worktree, so every test that wants it must
   SIMULATE its presence via monkeypatch, and the "absence" tests must
   simulate absence too rather than relying on it, exactly like the "scale"
   gate's round before this one)."""
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from backend.app import spec_ai  # noqa: E402
from backend.app.main import app  # noqa: E402
from backend.app.spec_ai import (  # noqa: E402
    MAX_ATTEMPTS,
    focus_spec,
    improve_spec,
    refine_spec,
    spec_changes,
    wizard_step,
)
from blender.builders import connectivity  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
STREET_LIGHT = json.loads((REPO_ROOT / "examples" / "street_light.json").read_text())
BENCH = json.loads((REPO_ROOT / "examples" / "park_bench.json").read_text())
client = TestClient(app)

#: improve_spec calls evaluate_perspectives (4 more complete() calls) —
#: stubbed out the same way test_improve.py/test_scale_gate.py's siblings do
#: so the "calls" counting in these tests stays about the spec-generation
#: retry loop only.
FAKE_PERSPECTIVES = [
    {"id": pid, "label": pid.title(), "icon": "x", "summary": "", "findings": [], "error": None}
    for pid in ("architecture", "mechanical", "civil", "design")
]


@pytest.fixture(autouse=True)
def mock_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setattr(spec_ai, "evaluate_perspectives",
                        lambda spec, model=None: FAKE_PERSPECTIVES)


def bench():
    return json.loads(json.dumps(BENCH))


def street_light():
    return json.loads(json.dumps(STREET_LIGHT))


def script_complete(monkeypatch, replies):
    """Replace spec_ai.complete with a scripted sequence of replies (one per
    attempt; the last reply repeats if the loop outruns the list). Returns
    the list of user messages actually sent, so a test can inspect what the
    correction prompt said."""
    calls = []

    def fake_complete(system, user, **kwargs):
        calls.append(user)
        reply = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(spec_ai, "complete", fake_complete)
    return calls


def _bump_seat(spec):
    """Mutate the 'seat' component's middle slat (a real primitive edit) —
    used to synthesize a "changed" component."""
    for p in spec["primitives"]:
        if p.get("name") == "slat_mid":
            p["location"] = list(p["location"])
            p["location"][2] = "seat_height + 0.05"
            return spec
    raise AssertionError("fixture missing slat_mid")


def _add_cupholder(spec):
    """Append a brand-new 'cupholder' component/primitive, cloned from an
    existing seat slat so it overlaps real geometry and never floats — used
    to synthesize an "added" component."""
    template = next(p for p in BENCH["primitives"] if p["name"] == "slat_mid")
    cupholder = json.loads(json.dumps(template))
    cupholder["name"] = "cupholder"
    cupholder["component"] = "cupholder"
    spec["primitives"].append(cupholder)
    spec["components"] = spec["components"] + ["cupholder"]
    return spec


# ---------------------------------------------------------------------------
# spec_changes — pure diff
# ---------------------------------------------------------------------------

class TestSpecChanges:
    def test_added_removed_changed_from_primitives(self):
        before = {"primitives": [
            {"kind": "box", "name": "a", "component": "seat", "params": {"size": [1, 1, 1]}},
            {"kind": "box", "name": "b", "component": "legs", "params": {"size": [1, 1, 1]}},
        ]}
        after = {"primitives": [
            {"kind": "box", "name": "a", "component": "seat", "params": {"size": [2, 1, 1]}},
            {"kind": "box", "name": "c", "component": "cupholder", "params": {"size": [1, 1, 1]}},
        ]}
        out = spec_changes(before, after)
        assert out["added"] == ["cupholder"]
        assert out["removed"] == ["legs"]
        assert out["changed"] == ["seat"]

    def test_unchanged_primitives_yield_no_changed(self):
        spec = {"primitives": [
            {"kind": "box", "name": "a", "component": "seat", "params": {"size": [1, 1, 1]}},
        ]}
        out = spec_changes(spec, json.loads(json.dumps(spec)))
        assert out["changed"] == []
        assert out["added"] == []
        assert out["removed"] == []

    def test_fallback_to_components_when_primitives_absent(self):
        before = {"components": ["pole", "base"]}
        after = {"components": ["pole", "base", "arm"]}
        out = spec_changes(before, after)
        assert out["added"] == ["arm"]
        assert out["removed"] == []
        # no primitives on either side -> nothing to diff at the primitive
        # level, so "changed" can't be determined and stays empty
        assert out["changed"] == []

    def test_params_changed_covers_parameters_and_toggles(self):
        before = {"parameters": [{"id": "h", "value": 1}],
                  "toggles": [{"id": "backrest", "value": True}]}
        after = {"parameters": [{"id": "h", "value": 2}],
                 "toggles": [{"id": "backrest", "value": False}]}
        out = spec_changes(before, after)
        assert out["params_changed"] == ["backrest", "h"]

    def test_summary_no_structural_changes(self):
        spec = bench()
        out = spec_changes(spec, json.loads(json.dumps(spec)))
        assert out["added"] == out["removed"] == out["changed"] == out["params_changed"] == []
        assert out["summary"] == "No structural changes"

    def test_summary_composed_from_changed_and_added(self):
        before = bench()
        after = _add_cupholder(_bump_seat(bench()))
        out = spec_changes(before, after)
        assert out["changed"] == ["seat"]
        assert out["added"] == ["cupholder"]
        assert out["summary"] == "Changed seat; added cupholder"

    def test_never_raises_on_malformed_input(self):
        assert spec_changes(None, {})["summary"] == "No structural changes"
        assert spec_changes({"primitives": "not a list"}, {})["summary"]


# ---------------------------------------------------------------------------
# "changes" attached to every edit response
# ---------------------------------------------------------------------------

class TestChangesAttached:
    def test_refine_result_carries_changes_matching_constructed_mutation(self, monkeypatch):
        base = bench()
        after = _add_cupholder(_bump_seat(bench()))
        calls = script_complete(monkeypatch, [json.dumps(after)])
        out = refine_spec(base, "raise the middle seat slat and add a cupholder")
        assert len(calls) == 1
        assert out["changes"]["added"] == ["cupholder"]
        assert out["changes"]["removed"] == []
        assert out["changes"]["changed"] == ["seat"]
        assert out["changes"]["params_changed"] == []
        assert out["changes"]["summary"] == "Changed seat; added cupholder"

    def test_focus_result_carries_changes(self, monkeypatch):
        base = bench()
        script_complete(monkeypatch, [json.dumps(base)])
        out = focus_spec(base, "seat")
        assert out["changes"]["summary"] == "No structural changes"

    def test_wizard_step_result_carries_changes(self, monkeypatch):
        base = street_light()
        script_complete(monkeypatch, [json.dumps(base)])
        out = wizard_step(base, "connections")
        assert "changes" in out
        assert out["changes"]["summary"] == "No structural changes"

    def test_improve_result_carries_changes(self, monkeypatch):
        base = street_light()
        script_complete(monkeypatch, [json.dumps(base)])
        out = improve_spec(base)
        assert "changes" in out
        assert out["changes"]["summary"] == "No structural changes"

    def test_removing_the_diff_would_be_caught(self, monkeypatch):
        # documents the contract this whole test class guards: a "changes"
        # key must exist and be the right shape, not just be present.
        base = bench()
        script_complete(monkeypatch, [json.dumps(base)])
        out = refine_spec(base, "no-op")
        assert set(out["changes"]) == {"added", "removed", "changed",
                                       "params_changed", "summary"}


# ---------------------------------------------------------------------------
# Materials wizard step — mechanical scope gate
# ---------------------------------------------------------------------------

class TestMaterialsScopeGate:
    def test_geometry_mutation_triggers_scope_retry_naming_the_component(self, monkeypatch):
        base = bench()
        bad = _bump_seat(bench())
        bad["materials"][0]["color"] = "#334455"
        good = bench()
        good["materials"][0]["color"] = "#334455"
        calls = script_complete(monkeypatch, [json.dumps(bad), json.dumps(good)])
        out = wizard_step(base, "materials")
        assert len(calls) == 2
        assert "[scope]" in calls[1]
        assert "seat" in calls[1]
        assert out["spec"]["materials"][0]["color"] == "#334455"
        assert out["changes"]["changed"] == []

    def test_materials_only_mutation_passes_immediately(self, monkeypatch):
        base = bench()
        good = bench()
        good["materials"][1]["uv_scale"] = 4.0
        calls = script_complete(monkeypatch, [json.dumps(good)])
        out = wizard_step(base, "materials")
        assert len(calls) == 1
        assert out["spec"]["materials"][1]["uv_scale"] == 4.0
        assert out["changes"]["summary"] == "No structural changes"

    def test_final_attempt_is_lenient_and_surfaces_a_violation(self, monkeypatch):
        base = bench()
        bad = _bump_seat(bench())
        # every attempt returns the same illegal mutation
        calls = script_complete(monkeypatch, [json.dumps(bad)] * MAX_ATTEMPTS)
        out = wizard_step(base, "materials")
        assert len(calls) == MAX_ATTEMPTS
        assert any(v.get("kind") == "scope" for v in out["violations"])

    def test_connections_step_is_not_scope_gated(self, monkeypatch):
        # the brief explicitly excludes connections/details from this gate —
        # a geometry-adding connections-step reply must NOT retry as "scope"
        base = street_light()
        changed = street_light()
        changed["connections"] = changed["connections"] + [
            {"a": "arm", "b": "pole", "type": "band_clamp"}
        ]
        calls = script_complete(monkeypatch, [json.dumps(changed)])
        out = wizard_step(base, "connections")
        assert len(calls) == 1
        assert out["spec"]["connections"][-1]["type"] == "band_clamp"


# ---------------------------------------------------------------------------
# Integration gate — embedded parts
# ---------------------------------------------------------------------------

EMBED_FINDING = {
    "severity": "warning",
    "kind": "embedded_part",
    "message": "cupholder is embedded 40mm inside seat — seat it on a "
               "surface instead",
    "component": "cupholder",
}

#: the sibling checker's findings may carry either kind — same shape,
#: different detection signature (fully engulfed vs. pass-through pierced).
PIERCED_FINDING = {
    "severity": "warning",
    "kind": "pierced_part",
    "message": "cupholder pierces straight through the seat slat",
    "component": "cupholder",
}


class TestIntegrationGate:
    def test_first_attempt_embedded_triggers_retry_with_hint(self, monkeypatch):
        calls_check = {"n": 0}

        def fake_embed(prims, spec):
            calls_check["n"] += 1
            return [EMBED_FINDING] if calls_check["n"] == 1 else []

        monkeypatch.setattr(connectivity, "check_embedded_parts", fake_embed,
                            raising=False)
        base = bench()
        reply = json.dumps(_add_cupholder(bench()))
        calls = script_complete(monkeypatch, [reply, reply])
        out = refine_spec(base, "add a cupholder")
        assert len(calls) == 2
        assert "[integration]" in calls[1]
        assert EMBED_FINDING["message"] in calls[1]
        assert "connections" in calls[1]

    def test_final_attempt_is_lenient_and_relays_both_finding_kinds(self, monkeypatch):
        # the upstream checker's findings may carry EITHER "embedded_part" or
        # "pierced_part" as their kind (same shape, same never-raises
        # guarantee) — the gate must treat the findings list generically and
        # relay whichever kinds it's handed, not just one hardcoded kind.
        monkeypatch.setattr(
            connectivity, "check_embedded_parts",
            lambda prims, spec: [EMBED_FINDING, PIERCED_FINDING], raising=False,
        )
        base = bench()
        reply = json.dumps(_add_cupholder(bench()))
        calls = script_complete(monkeypatch, [reply] * MAX_ATTEMPTS)
        out = refine_spec(base, "add a cupholder")
        assert len(calls) == MAX_ATTEMPTS
        kinds = {v.get("kind") for v in out["violations"]
                if v.get("kind") in ("embedded_part", "pierced_part")}
        assert kinds == {"embedded_part", "pierced_part"}
        messages = " ".join(v.get("message", "") for v in out["violations"])
        assert EMBED_FINDING["message"] in messages
        assert PIERCED_FINDING["message"] in messages

    def test_absent_checker_is_a_noop(self, monkeypatch):
        # SIMULATE absence (round-6 lesson): delattr with raising=False
        # tolerates the attribute genuinely not existing (it doesn't, in
        # this tree) AND removes it if some other test happened to add it.
        monkeypatch.delattr(connectivity, "check_embedded_parts", raising=False)
        base = bench()
        reply = json.dumps(_add_cupholder(bench()))
        calls = script_complete(monkeypatch, [reply])
        out = refine_spec(base, "add a cupholder")
        assert len(calls) == 1
        assert not any(v.get("kind") in ("embedded_part", "pierced_part")
                       for v in out.get("violations", []))

    def test_wizard_step_is_gated_too(self, monkeypatch):
        calls_check = {"n": 0}

        def fake_embed(prims, spec):
            calls_check["n"] += 1
            return [EMBED_FINDING] if calls_check["n"] == 1 else []

        monkeypatch.setattr(connectivity, "check_embedded_parts", fake_embed,
                            raising=False)
        base = bench()
        reply = json.dumps(_add_cupholder(bench()))
        calls = script_complete(monkeypatch, [reply, reply])
        wizard_step(base, "connections")
        assert len(calls) == 2
        assert "[integration]" in calls[1]

    def test_improve_spec_is_gated_too(self, monkeypatch):
        calls_check = {"n": 0}

        def fake_embed(prims, spec):
            calls_check["n"] += 1
            return [EMBED_FINDING] if calls_check["n"] == 1 else []

        monkeypatch.setattr(connectivity, "check_embedded_parts", fake_embed,
                            raising=False)
        base = bench()
        reply = json.dumps(_add_cupholder(bench()))
        calls = script_complete(monkeypatch, [reply, reply])
        improve_spec(base)
        assert len(calls) == 2
        assert "[integration]" in calls[1]


# ---------------------------------------------------------------------------
# Endpoint passthrough
# ---------------------------------------------------------------------------

class TestEndpointPassthrough:
    def test_refine_endpoint_carries_changes(self):
        r = client.post("/api/refine-spec",
                        json={"spec": STREET_LIGHT, "message": "make it taller"})
        assert r.status_code == 200
        data = r.json()
        assert "changes" in data
        assert set(data["changes"]) == {"added", "removed", "changed",
                                        "params_changed", "summary"}
