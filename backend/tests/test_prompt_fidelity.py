"""Prompt-refinement fidelity contract.

The "make my request more detailed" pass (the design brief) must ELABORATE
what the user asked for — in full per-part dimensional detail — WITHOUT
inventing parts, features, or optional add-ons they never requested. The
user refines and extends the asset in later passes, so the first build is
meant to be a lean, faithful, richly-dimensioned rendering of exactly the
request, not a maximal one.

These tests guard that contract against regression on all three prompt
surfaces that carry it: the four-persona design panel actually used by
generate_spec/stream_generate_spec (``_panel_system``), the legacy
single-voice brief (``ENHANCE_SYSTEM``), and the reinforcing SCOPE FIDELITY
rule in the spec generator itself (``_system_prompt``). They assert on the
contract (no invention mandate, explicit no-add rule, per-part dimensions),
not on exact wording, so faithful rephrasings don't churn them.
"""
from backend.app.spec_ai import ENHANCE_SYSTEM, _panel_system, _system_prompt

#: the two prompts that turn a raw request into a design brief.
BRIEF_PROMPTS = (ENHANCE_SYSTEM, _panel_system())


def test_brief_pass_no_longer_mandates_inventing_optional_features():
    # The regression the user reported: both briefs used to order the model
    # to add "2-4 optional features worth exposing as toggles" and to "only
    # add what is missing" — that invented parts nobody asked for.
    for prompt in BRIEF_PROMPTS:
        low = prompt.lower()
        assert "optional features worth exposing" not in low
        assert "2-4 optional" not in low and "2–4 optional" not in low
        assert "only add what is missing" not in low


def test_brief_pass_forbids_adding_unrequested_parts():
    for prompt in BRIEF_PROMPTS:
        low = prompt.lower()
        # scoped to precisely the request ...
        assert "exactly what" in low
        assert "precisely that" in low
        # ... with an explicit no-invention instruction
        assert "did not ask for" in low
        assert "no new part" in low


def test_brief_pass_demands_per_part_dimensional_detail():
    # "more detailed about the dimensions" — the brief must call for a real
    # dimension per named part, spelling out the dimensional vocabulary.
    for prompt in BRIEF_PROMPTS:
        low = prompt.lower()
        assert "each named part" in low
        for word in ("height", "width", "depth", "diameter", "wall thickness"):
            assert word in low, f"brief prompt missing dimension word {word!r}"


def test_brief_pass_notes_user_refines_later():
    # The rationale for staying lean: the user iterates. Both briefs say so,
    # so the model understands WHY not to volunteer extras.
    for prompt in BRIEF_PROMPTS:
        assert "later pass" in prompt.lower()


def test_system_prompt_carries_scope_fidelity_rule():
    prompt = _system_prompt("strict")
    assert "SCOPE FIDELITY" in prompt
    assert "do NOT invent" in prompt
    # the carve-out that keeps the rule from starving parts of their supports
    low = prompt.lower()
    assert "load-path" in low or "load path" in low


def test_scope_fidelity_rule_is_outside_the_archetypes_prose_region():
    # test_prompt_archetypes.py slices the prompt between the archetype
    # bullet and the MATERIALS header; the new rule must sit BEFORE that
    # slice so it neither breaks that slice nor its standards-leak guard.
    prompt = _system_prompt("strict")
    assert prompt.index("SCOPE FIDELITY") < prompt.index(
        "Map the request to a known archetype"
    )
