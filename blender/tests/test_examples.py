"""Every file in examples/ must be a valid, buildable, grounded AssetSpec —
they ship as demo assets and as the AI's few-shot references, so a broken one
would mislead both users and the model. This validates them exactly the way
the server's _postprocess does: schema → builds → load-path check."""
import json
from pathlib import Path

import jsonschema
import pytest

import blender.builders  # noqa: F401  (registers curated builders)
from blender.builders.base import compute_primitives
from blender.builders.connectivity import check_buildability

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((REPO_ROOT / "schemas" / "asset_spec.schema.json").read_text())
EXAMPLES = sorted((REPO_ROOT / "examples").glob("*.json"))


def _ids(paths):
    return [p.name for p in paths]


assert EXAMPLES, "no example specs found"


@pytest.mark.parametrize("path", EXAMPLES, ids=_ids(EXAMPLES))
def test_example_matches_schema(path):
    jsonschema.validate(json.loads(path.read_text()), SCHEMA)


@pytest.mark.parametrize("path", EXAMPLES, ids=_ids(EXAMPLES))
def test_example_builds_and_is_grounded(path):
    spec = json.loads(path.read_text())
    prims = compute_primitives(spec)
    assert prims, f"{path.name} produced no primitives"
    errors = [f for f in check_buildability(prims, spec) if f["severity"] == "error"]
    assert not errors, f"{path.name} has load-path errors: {[e['message'] for e in errors]}"


@pytest.mark.parametrize("path", EXAMPLES, ids=_ids(EXAMPLES))
def test_example_builds_with_hardware(path):
    """The connection-hardware pass must not throw on any example."""
    spec = json.loads(path.read_text())
    spec.setdefault("toggles", []).append(
        {"id": "connection_hardware", "label": "Connection Hardware", "value": True}
    )
    assert compute_primitives(spec)  # exercises the orchestrator + emitters


def test_expected_examples_present():
    names = {p.name for p in EXAMPLES}
    assert {"street_light.json", "park_bench.json", "bike_rack.json",
            "planter.json"} <= names
