"""bike_rack.json as a third few-shot example in _system_prompt.

FEW_SHOT_BUILTIN (street_light) demonstrates the curated no-primitives path
and FEW_SHOT_CUSTOM (park_bench) demonstrates custom primitives with only
kind: box and no array. Neither shows the sweep fabrication kind, the
array {count, step} repetition pattern, or a weld connection — all of which
the GEOMETRY RULES / CONNECTION RULES prose promotes but never demonstrates.

bike_rack.json (asset name "LoopRack5") has all three: a "sweep" primitive
for the hoop, an "array": {"count": ..., "step": ...} on both the hoop and
the base_channel primitives, and a top-level "weld" connection between
hoops and base. This test pins that it is embedded into the system prompt
as a worked example, alongside (not instead of) the existing two.
"""
from backend.app.spec_ai import _system_prompt


def test_bike_rack_source_has_sweep_array_and_weld():
    # Sanity check on the claim itself before trusting the prompt embeds it.
    import json
    from pathlib import Path

    spec = json.loads(
        (Path(__file__).resolve().parents[2] / "examples" / "bike_rack.json").read_text()
    )
    kinds = {p["kind"] for p in spec["primitives"]}
    assert "sweep" in kinds
    assert any("array" in p for p in spec["primitives"])
    assert any(c.get("type") == "weld" for c in spec["connections"])


def test_system_prompt_embeds_bike_rack_as_third_example():
    prompt = _system_prompt("advisory")
    assert "LoopRack5" in prompt
    assert "EXAMPLE" in prompt
    # the label should indicate what this example demonstrates
    idx = prompt.index("LoopRack5")
    label_region = prompt[max(0, idx - 200):idx]
    assert "EXAMPLE" in label_region
    assert "array" in label_region.lower() or "sweep" in label_region.lower()


def test_system_prompt_keeps_all_three_examples():
    prompt = _system_prompt("advisory")
    # distinctive asset "name" markers from each example file, none of which
    # otherwise appear in the schema/standards JSON dumped into the prompt
    assert "CobraHead30" in prompt   # street_light (FEW_SHOT_BUILTIN)
    assert "SlatBench6ft" in prompt  # park_bench (FEW_SHOT_CUSTOM)
    assert "LoopRack5" in prompt     # bike_rack (new arrayed/swept example)
