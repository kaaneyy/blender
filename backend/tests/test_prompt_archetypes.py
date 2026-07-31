"""ARCHETYPES/ANCHOR-SIZES prose coverage for the 4 accessible asset_types.

standards/us_codes.json already carries full ADA entries for drinking_fountain,
ramp, accessible_table, and trash_receptacle, and that JSON gets embedded
verbatim into the system prompt (see the "US CODE STANDARDS" block) — so a
naive substring check against the WHOLE prompt would false-pass on the type
keys alone, without the prose ever describing what these fixtures look like
or how big they physically are.

This test slices the prompt down to just the ARCHETYPES/ANCHOR-SIZES prose
region — between the "Map the request to a known archetype" bullet and the
"MATERIALS" section header — which excludes the standards JSON dump entirely,
and asserts each of the 4 accessible types is covered there by name, by a
characteristic-form keyword, and by a real anchor dimension.
"""
import re

from backend.app.spec_ai import BUILTIN_BUILDERS, _system_prompt

START_MARKER = "Map the request to a known archetype"
END_MARKER = "MATERIALS"


def _prose_region() -> str:
    prompt = _system_prompt("advisory")
    start = prompt.index(START_MARKER)
    end = prompt.index(END_MARKER, start)
    assert end > start, "expected MATERIALS section to follow the archetypes bullet"
    return prompt[start:end]


def test_prose_region_excludes_standards_json():
    # sanity check on the slice itself: the full standards JSON block (with
    # its "parameters"/"code_ref" keys) must NOT leak into the sliced region,
    # otherwise the assertions below would trivially pass on the JSON alone.
    region = _prose_region()
    assert "code_ref" not in region
    assert '"parameters"' not in region


def test_drinking_fountain_archetype_and_anchor():
    region = _prose_region().lower()
    assert "drinking fountain" in region
    # characteristic form: pedestal/wall-hung bowl or basin with a spout
    assert re.search(r"(pedestal|wall-hung).*(bowl|basin)", region)
    assert "spout" in region
    # bi-level hi-lo is the common accessible configuration
    assert "hi-lo" in region or "hi/lo" in region or "bi-level" in region
    # anchor dimension: spout ~0.9 m / 36 in above grade
    assert "0.9 m" in region and "36 in" in region
    assert "0.4 m" in region  # basin width


def test_ramp_archetype_and_anchor():
    region = _prose_region().lower()
    assert re.search(r"\bramp\b", region)
    # characteristic form: sloped deck plane + landings + curbs/handrails
    assert "sloped deck plane" in region or "sloped deck" in region
    assert "landing" in region
    assert "curb" in region or "handrail" in region
    # anchor dimensions: 36 in / 0.9 m clear width, 60 in / 1.5 m landings, 1:12 slope
    assert "0.9 m" in region and "36 in" in region
    assert "1.5 m" in region and "60 in" in region
    assert "1:12" in region


def test_accessible_table_archetype_and_anchor():
    region = _prose_region().lower()
    assert "accessible table" in region
    # characteristic form: flat top on legs, clear knee space, no cross-brace
    assert "knee space" in region or "knee clearance" in region
    assert "cross-brace" in region or "apron" in region
    # anchor dimensions: 0.71-0.86 m / 28-34 in top height, 0.69 m / 27 in knee clearance
    assert "0.71" in region and "0.86" in region
    assert "28-34 in" in region
    assert "0.69 m" in region and "27 in" in region


def test_trash_receptacle_archetype_and_anchor():
    region = _prose_region().lower()
    assert "trash" in region and "receptacle" in region
    # characteristic form: tube or slatted body with a domed/lathe lid cap
    assert re.search(r"(tube|slatted).*(dome|lathe)", region) or (
        "lid cap" in region and ("dome" in region or "lathe" in region)
    )
    # anchor dimensions: ~0.5 m across, ~0.9-1.1 m tall
    assert "0.5 m" in region
    assert "0.9-1.1 m" in region or ("0.9" in region and "1.1 m" in region)


def test_structural_post_anchor_present():
    # freestanding post/pole base diameter anchor (Brief 6) — guards against
    # "15 ft victorian post" regenerating as a hairline wire: pins the real
    # diameter range, the Victorian/ornamental fluted-shaft callout, and the
    # explicit "never a hairline member" instruction.
    region = _prose_region().lower()
    assert "post/pole base diameter" in region
    assert "3-8 in" in region and "0.08-0.20 m" in region
    assert "victorian" in region and "4-6 in" in region and "fluted" in region
    assert "never" in region and "hairline" in region


def test_dimension_slider_must_drive_geometry_rule_present():
    # The "6 mm pole while the diameter slider says 7 in" bug: a diameter/
    # width slider must actually DRIVE the member's built cross-section, not
    # sit next to a hardcoded sliver. Pin the rule + its concrete expression
    # so the guidance can't quietly regress.
    region = _prose_region()
    assert "WIRE EACH DIMENSION SLIDER" in region
    assert "pole_base_diameter/2" in region
    low = region.lower()
    assert "hardcode a thin cross-section" in low
    assert "hairline" in low


def test_fabrication_stock_rule_present():
    # The geometry now models real stock — hollow sections, wall thickness,
    # square HSS, called-out bends — but the model only reaches for it if the
    # prompt asks. Pin the rule and its concrete numbers so a generated asset
    # comes out fabrication-grade instead of a solid sculpture.
    region = _prose_region()
    assert "BUILD IT LIKE A FABRICATOR" in region
    low = region.lower()
    assert '"section": "square"' in region, "square stock must be named"
    assert '"wall"' in region and '"shell"' in region
    assert '"bend_radius"' in region
    assert "billet" in low, "the solid-member failure mode is called out"
    # the worked example: a 2 in square rack member
    assert "0.0254" in region and "0.0048" in region


def test_no_dead_anchor_bolts_toggle():
    # anchor_bolts was advertised as a street_light toggle but no builder
    # ever consumed it (Brief 4) — it must be gone from both the prompt
    # dict and the rendered system prompt.
    assert "anchor_bolts" not in BUILTIN_BUILDERS["street_light"]["toggles"]
    prompt = _system_prompt("strict")
    assert "anchor_bolts" not in prompt


def test_builtin_builders_advertises_mounting_select():
    # street_light.py actually reads spec_selects()["mounting"] for its
    # ground connection — the prompt must advertise it (with its real
    # allowed values) instead of a phantom toggle.
    assert "mounting" in BUILTIN_BUILDERS["street_light"]["selects"]
    assert BUILTIN_BUILDERS["street_light"]["selects"]["mounting"] == [
        "flange", "burial", "embedded",
    ]
    prompt = _system_prompt("strict")
    assert '"mounting"' in prompt


def test_primitives_precedence_guidance_present():
    # A styled/extended request against a curated asset_type (e.g. a
    # "victorian post with lantern" typed as asset_type street_light) must
    # be steered onto the primitives path — assert on the distinctive
    # phrase introduced for this (Brief 4).
    prompt = _system_prompt("strict")
    assert "PRIMITIVES ALWAYS WIN" in prompt


def test_primitive_budget_is_complexity_adaptive_not_capped():
    # The GEOMETRY RULES primitive-count guidance must no longer hard-cap
    # every request at 40 primitives (that contradicts the very next
    # COMPLETENESS bullet requiring every named part to exist as its own
    # component for multi-feature requests). It must still explicitly
    # discourage micro-detail in favor of readable massing.
    prompt = _system_prompt("strict")
    assert "10–40 primitives is the sweet spot" not in prompt
    assert "10-40 primitives is the sweet spot" not in prompt
    assert "readable massing" in prompt
    assert "micro-detail" in prompt


def test_toggle_completeness_rule_present():
    # Round 6 brief 10 (the "double the arm" bug report: adding a second
    # arm didn't add the light on top of it) — the GEOMETRY RULES must
    # forcefully require a toggle to co-gate EVERY primitive of the feature
    # it adds, including dependent parts riding on a toggled member. Pin
    # the distinctive phrase introduced for this rule plus a couple of the
    # named examples so the rule can't quietly regress to only mentioning
    # the member itself.
    prompt = _system_prompt("strict")
    assert "TOGGLE COMPLETENESS" in prompt
    assert "gate EVERY primitive of that feature with the SAME visible_if" in prompt
    assert "luminaire on a second arm" in prompt
