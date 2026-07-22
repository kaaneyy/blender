"""Tests for the dead-control generation gate: a spec whose sliders/toggles
provably drive nothing (an invented control on a curated-builder spec, or an
id no primitive expression references on the generic path) is a classified
generation failure (kind="dead_controls"), lenient on the final attempt —
the same pattern buildability/scale already use (see test_retry.py /
test_scale_gate.py). Also keeps BUILTIN_BUILDERS (the ids the system prompt
advertises to the model) in exact sync with each curated builder module's
own declared CONSUMED_PARAMS/CONSUMED_TOGGLES/CONSUMED_SELECTS — the same
keep-in-sync pattern test_schedule.py uses for the bolt catalog."""
import json
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from backend.app.spec_ai import (  # noqa: E402
    BUILTIN_BUILDERS,
    SpecGenerationError,
    _postprocess,
)
from blender.builders import accessible_table, street_light  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
VALID = (REPO_ROOT / "examples" / "street_light.json").read_text()

#: BUILTIN_BUILDERS' "parameters" entries carry a " (unit)" display suffix
#: for the prompt (e.g. "pole_height (ft)") — strip it to get the bare id.
_UNIT_SUFFIX_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _bare_ids(entries):
    return {_UNIT_SUFFIX_RE.sub("", e) for e in entries}


def spec_dict():
    return json.loads(VALID)


class TestBuiltinBuildersVocabInSync:
    """BUILTIN_BUILDERS is what the model is TOLD each curated builder
    consumes; CONSUMED_PARAMS/CONSUMED_TOGGLES/CONSUMED_SELECTS is what the
    builder ACTUALLY consumes. A drift between the two would either mislead
    the model (prompt promises an id the builder ignores) or make
    check_dead_controls flag a real, prompt-advertised id as dead."""

    def test_street_light_parameters_match(self):
        assert _bare_ids(BUILTIN_BUILDERS["street_light"]["parameters"]) == \
            set(street_light.CONSUMED_PARAMS)

    def test_street_light_toggles_match(self):
        assert set(BUILTIN_BUILDERS["street_light"]["toggles"]) == \
            set(street_light.CONSUMED_TOGGLES)

    def test_street_light_selects_match(self):
        assert set(BUILTIN_BUILDERS["street_light"]["selects"]) == \
            set(street_light.CONSUMED_SELECTS)

    def test_accessible_table_parameters_match(self):
        assert _bare_ids(BUILTIN_BUILDERS["accessible_table"]["parameters"]) == \
            set(accessible_table.CONSUMED_PARAMS)

    def test_accessible_table_toggles_match(self):
        assert set(BUILTIN_BUILDERS["accessible_table"].get("toggles", [])) == \
            set(accessible_table.CONSUMED_TOGGLES)

    def test_accessible_table_selects_match(self):
        assert set(BUILTIN_BUILDERS["accessible_table"].get("selects", {})) == \
            set(accessible_table.CONSUMED_SELECTS)


@pytest.fixture(autouse=True)
def mock_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")


def _street_light_with_invented_control():
    bad = spec_dict()
    bad["parameters"].append(
        {"id": "lantern_height", "label": "Lantern Height", "type": "slider",
         "min": 0.1, "max": 1.0, "step": 0.05, "value": 0.4, "unit": "m"}
    )
    return bad


class TestDeadControlGate:
    def test_invented_control_on_curated_builder_raises_dead_controls(self):
        bad = _street_light_with_invented_control()
        with pytest.raises(SpecGenerationError) as err:
            _postprocess(json.dumps(bad), "strict")
        assert err.value.kind == "dead_controls"
        assert "lantern_height" in str(err.value)
        assert "lantern_height" in err.value.hint
        # both remedies named
        assert "remove" in err.value.hint.lower()
        assert "primitives" in err.value.hint.lower()

    def test_final_attempt_is_lenient_and_ships_with_violation(self):
        bad = _street_light_with_invented_control()
        out = _postprocess(json.dumps(bad), "strict", lenient_buildability=True)
        assert out["ok"] is False
        findings = [v for v in out["violations"] if v.get("limit_type") == "dead_control"]
        assert findings and "lantern_height" in findings[0]["message"]

    def test_clean_street_light_reply_never_raises_dead_controls(self):
        # sanity check against a false-positive gate: the untouched example
        # must sail through unchanged.
        out = _postprocess(VALID, "strict")
        assert out["spec"]["asset_type"] == "street_light"
        assert not any(v.get("limit_type") == "dead_control" for v in out["violations"])
