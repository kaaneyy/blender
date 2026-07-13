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


# ---------------------------------------------------------------------------
# Person-adaptive parameters: site furniture is sized for the PEOPLE using it
# (ADA seat heights, rider capacity), and the whole pipeline — geometry AND
# generated connection hardware — must follow those sliders, not fight them.
# ---------------------------------------------------------------------------

IN = 0.0254


def _example(name, **param_overrides):
    spec = json.loads((REPO_ROOT / "examples" / name).read_text())
    ids = set()
    for p in spec["parameters"]:
        if p["id"] in param_overrides:
            p["value"] = param_overrides.pop(p["id"])
        ids.add(p["id"])
    assert not param_overrides, f"unknown parameter(s): {sorted(param_overrides)}"
    spec.setdefault("toggles", []).append(
        {"id": "connection_hardware", "label": "CH", "value": True}
    )
    return spec


@pytest.mark.parametrize("inches", [17.0, 19.0])
def test_bench_adapts_to_its_users(inches):
    """The ADA-903.5 seat-height slider moves the whole seat assembly and
    the carriage bolts follow the relocated seat/frame joints."""
    prims = compute_primitives(_example("park_bench.json", seat_height=inches))
    seat_z = inches * IN
    slat = next(p for p in prims if p.name == "slat_mid")
    assert slat.location[2] == pytest.approx(seat_z + 0.02)
    # carriage-bolt domes ride at the seat face, wherever the slider put it
    domes = [p for p in prims if p.component == "hardware"
             and p.name.endswith("_dome")]
    assert domes, "the declared seat/frame carriage bolts must materialize"
    for d in domes:
        assert abs(d.location[2] - seat_z) < 0.12, d.name


@pytest.mark.parametrize("count", [2, 5])
def test_bike_rack_scales_with_rider_capacity(count):
    """Hoop count is a person-facing capacity slider: every extra rider gets
    a hoop with its own base channel, and the build stays clean."""
    prims = compute_primitives(_example("bike_rack.json", hoop_count=count))
    hoops = [p for p in prims if p.component == "hoops"]
    channels = [p for p in prims if p.component == "base"]
    assert len(hoops) == count
    assert len(channels) == count
