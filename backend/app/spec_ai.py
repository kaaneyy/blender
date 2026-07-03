"""Prompt→AssetSpec and refine pipelines (T2.1–T2.3, T2.5, T2.6).

Flow: build a system prompt grounded in the JSON Schema, the US-code
standards DB (T2.2 — ranges come from the DB, not model memory), the curated
builder catalog, and a few-shot custom example → call the provider → strip
fences → parse → strict JSON-Schema validation (T7.4, rejects unknown
fields) → geometry sanity check → US-code validation/clamping (T2.3).
On any parse/validation failure, re-prompt once with the error appended
(T2.6).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import jsonschema

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from standards.validator import (  # noqa: E402
    load_standards,
    validate_spec,
    validate_standards_db,
)
from blender.builders.base import MATERIAL_PRESETS, compute_primitives  # noqa: E402
import blender.builders  # noqa: E402,F401  (registers curated builders)

from .llm import LLMError, complete, complete_stream  # noqa: E402

#: Marks the end of the streamed raw text; the JSON payload after it carries
#: the validated result (or the error). The frontend splits on this.
STREAM_SENTINEL = "\n<<<ASSETFORGE_RESULT>>>\n"

ASSET_SPEC_SCHEMA = json.loads(
    (REPO_ROOT / "schemas" / "asset_spec.schema.json").read_text(encoding="utf-8")
)
FEW_SHOT_CUSTOM = (REPO_ROOT / "examples" / "park_bench.json").read_text(encoding="utf-8")
FEW_SHOT_BUILTIN = (REPO_ROOT / "examples" / "street_light.json").read_text(encoding="utf-8")

#: Curated builders and the parameter/toggle ids their geometry understands.
BUILTIN_BUILDERS = {
    "street_light": {
        "parameters": ["pole_height (ft)", "arm_length (ft)",
                       "pole_base_diameter (in)", "pole_top_diameter (in)"],
        "toggles": ["double_arm", "banner_bracket", "anchor_bolts"],
        "material_slots": ["pole", "base", "luminaire", "lens"],
    },
}


class SpecGenerationError(RuntimeError):
    """LLM produced output that could not be turned into a valid spec."""


def _system_prompt(code_mode: str) -> str:
    standards = load_standards()
    standards.pop("_meta", None)
    return f"""You convert user requests into AssetSpec JSON for a parametric 3D asset generator (street furniture, lighting, signage, props of any kind).

OUTPUT RULES
- Return ONLY the AssetSpec JSON object. No prose, no markdown fences.
- It must validate against this JSON Schema (unknown fields are rejected):
{json.dumps(ASSET_SPEC_SCHEMA, separators=(",", ":"))}

GEOMETRY RULES
- Curated builders exist for these asset_types; when the request matches one, use it with EXACTLY these parameter/toggle ids and material slots, and DO NOT include "primitives":
{json.dumps(BUILTIN_BUILDERS, indent=1)}
- For ANY other asset, set a semantic asset_type (lowercase snake_case; reuse a standards key below when one fits) and model the geometry yourself in the "primitives" array: cylinders, cones, boxes, spheres.
- Primitive dimensions are METERS. +Z is up. The asset stands on the ground plane z=0 (nothing below z=0). A cylinder/cone's axis is Z; "location" is its center, so a post of depth H sits at z=H/2. rotation is Euler XYZ radians.
- Every numeric field in a primitive may instead be a string expression over parameter/toggle ids, e.g. "pole_height/2" or "seat_height + 0.02". Allowed: numbers, ids, + - * / ( ), min(), max(), abs(). Toggle ids evaluate to 1/0. Parameter values are pre-converted to meters regardless of their display unit.
- EVERY major dimension a designer would tweak must be a parameter (slider) referenced from expressions — never hard-code it. Optional features (backrest, second arm, finial, ...) must be toggles gating primitives via "visible_if".
- Give every primitive a component (nested grouping in exports) and a material_slot. 10–40 primitives is the sweet spot; favor simple, readable massing over micro-detail.

CONNECTION RULES (think like a fabricator — every joint must be buildable in real life)
- Every part must be physically supported through a real load path down to the ground. Before finishing, walk through your primitives joint by joint and ask: what holds this part, and how would a crew actually fasten it on site?
- Parts that join MUST interpenetrate by 10-20 mm at the joint (e.g. a leg whose top is inside the rail it supports, a rail whose top is inside the slats it carries). Never leave parts floating or merely touching at a zero-thickness face — the app detects real overlaps to place bolts, washers, and nuts exactly there.
- Choose the realistic connection for each joint and model its visible parts as their own primitives/components where a real one would be seen: base plates + anchor-bolt pads where a vertical member meets the ground; cross rails or stretchers between legs so seat/deck boards have something to bolt to; brackets, gussets, or collars where members meet at right angles; sleeves/sockets for post-in-tube fits. A slat can NOT attach to a leg it never touches — add the rail.
- Round vertical poles receive horizontal members via band clamps (the app adds the clamp when a horizontal round member overlaps an upright pole) — make the arm/bracket actually reach into the pole's surface.
- Nothing may extend below z=0; ground attachment is expressed with a plate or footing collar AT z=0.

MATERIALS
- Presets: {", ".join(MATERIAL_PRESETS)}.
- Per slot you may override: color (hex), metalness 0-1 (reflectivity), roughness 0-1, uv_scale 0.05-20 (texture tiling), emission 0-20 (glow — use 2-6 for lamp lenses).
- Honor style/material requests from the prompt ("weathered bronze" → cast_iron preset + color #6e5b3f, roughness 0.8).

US CODE STANDARDS (T2.2 — use these ranges and code_refs for slider min/max/default whenever a parameter maps to one; do not invent limits):
{json.dumps(standards, separators=(",", ":"))}

- Set "code_mode": "{code_mode}". Set "units" to what the user implies (default imperial). Parameters keep human units (ft/in for imperial).

EXAMPLE — curated builder:
{FEW_SHOT_BUILTIN}

EXAMPLE — custom primitives (the "anything" path):
{FEW_SHOT_CUSTOM}"""


def _strip_fences(raw: str) -> str:
    text = raw.strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    # tolerate stray prose around the object
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return text


def _postprocess(raw: str, code_mode: str) -> dict:
    """Parse, schema-validate (T7.4), geometry-check, and code-clamp (T2.3)."""
    try:
        spec = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as exc:
        raise SpecGenerationError(f"Output was not valid JSON: {exc}") from None

    if not isinstance(spec, dict):
        raise SpecGenerationError("Output was not a JSON object")
    spec.setdefault("code_mode", code_mode)

    try:
        jsonschema.validate(spec, ASSET_SPEC_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise SpecGenerationError(
            f"Schema violation at {'/'.join(str(p) for p in exc.absolute_path) or '<root>'}: "
            f"{exc.message}"
        ) from None

    result = validate_spec(spec)

    try:  # prove the spec actually builds (catches bad expressions/params)
        compute_primitives(result.spec)
    except Exception as exc:
        raise SpecGenerationError(f"Spec does not build: {exc}") from None

    return result.to_dict()


def _run(system: str, user: str, code_mode: str) -> dict:
    raw = complete(system, user)
    try:
        return _postprocess(raw, code_mode)
    except SpecGenerationError as first_error:
        # T2.6: one retry with the error appended
        retry_user = (
            f"{user}\n\nYour previous answer failed validation with this error:\n"
            f"{first_error}\n\nPrevious answer:\n{raw[:4000]}\n\n"
            f"Return the corrected AssetSpec JSON only."
        )
        raw = complete(system, retry_user)
        return _postprocess(raw, code_mode)


def generate_spec(prompt: str, code_mode: str = "strict") -> dict:
    """T2.1: natural-language prompt → validated AssetSpec (+ violations)."""
    return _run(_system_prompt(code_mode), f"Request: {prompt}", code_mode)


def refine_spec(spec: dict, message: str, code_mode: str = "strict") -> dict:
    """T2.5: current spec + chat message → modified, re-validated spec."""
    user = (
        f"Here is the current AssetSpec:\n{json.dumps(spec, separators=(',', ':'))}\n\n"
        f"Apply this change and return the FULL updated AssetSpec JSON "
        f"(keep everything else identical, including ids):\n{message}"
    )
    return _run(_system_prompt(code_mode), user, code_mode)


# ---------------------------------------------------------------------------
# Installation guide
# ---------------------------------------------------------------------------

def _install_guide_prompts(spec: dict) -> tuple:
    standards = load_standards()
    relevant = standards.get(spec.get("asset_type", ""), {})
    system = (
        "You are a licensed site-furnishing installation specialist writing for a "
        "homeowner/contractor audience. Produce a clear, numbered installation guide "
        "in Markdown for the asset described by the AssetSpec JSON you are given. "
        "Structure: ## Overview (what it is, overall dimensions in ft/in AND meters), "
        "## Tools & materials, ## Site preparation (foundation/footing sizing guidance), "
        "## Assembly sequence (reference the spec's component names in order, with "
        "hardware: anchor bolts, nuts, washers, torque ranges), ## Connections "
        "(enumerate EVERY joint: which two components meet, the fastener type and "
        "size class, and exactly how it is executed on site — drilled, through-"
        "bolted, band-clamped, torqued, embedded), ## Code compliance "
        "checklist (cite the code_refs from the spec/standards, with the actual limits), "
        "## Inspection & maintenance. Use ONLY dimensions derivable from the spec; do "
        "not invent sizes. Include a short safety disclaimer that a licensed engineer "
        "must approve structural anchoring for public installations."
    )
    user = (
        f"INSTALL GUIDE request.\nAssetSpec:\n{json.dumps(spec, separators=(',', ':'))}\n\n"
        f"Applicable standards entry:\n{json.dumps(relevant, separators=(',', ':'))}"
    )
    return system, user


def generate_install_guide(spec: dict) -> str:
    """Plain-language installation instructions for the current asset,
    grounded in its actual dimensions, components, and code citations."""
    system, user = _install_guide_prompts(spec)
    return complete(system, user, temperature=0.3).strip()


# ---------------------------------------------------------------------------
# Standards refresh
# ---------------------------------------------------------------------------

def _diff_standards(old: dict, new: dict) -> list:
    changes = []
    for asset_type, entry in new.items():
        if asset_type.startswith("_"):
            continue
        if asset_type not in old:
            changes.append(f"added asset type '{asset_type}'")
            continue
        old_params = old[asset_type].get("parameters", {})
        for pid, rule in entry.get("parameters", {}).items():
            if pid not in old_params:
                changes.append(f"{asset_type}: added parameter '{pid}'")
            elif {k: rule.get(k) for k in ("min", "max", "default", "unit")} != {
                k: old_params[pid].get(k) for k in ("min", "max", "default", "unit")
            }:
                changes.append(f"{asset_type}.{pid}: limits changed")
    for asset_type in old:
        if not asset_type.startswith("_") and asset_type not in new:
            changes.append(f"removed asset type '{asset_type}'")
    return changes


STANDARDS_NOTE = (
    "Proposed from the AI's knowledge of published standards (no live web "
    "access) — review the cited sections before relying on them."
)


def _standards_prompts() -> tuple:
    current = load_standards()
    system = (
        "You maintain a JSON database of US dimensional code limits for site "
        "furnishings and streetscape assets (MUTCD, ADA/PROWAG, IBC, AASHTO, AWWA). "
        "Review the CURRENT database you are given: correct any limits that do not "
        "match the latest published editions, and add 3-8 commonly requested asset "
        "types that are missing (e.g. bike_rack, drinking_fountain, picnic_table, "
        "flagpole, transit_shelter, guardrail). Keep the exact same JSON structure: "
        "top-level keys are snake_case asset types plus '_meta'; each type has "
        "'source' and 'parameters'; each parameter rule has min, max (number or "
        "null), default, unit (ft|in|m|cm|mm), code_ref, and optionally note. "
        "Cite real, specific sections in code_ref/source. Bump _meta.version by 1 "
        "and keep the _meta.disclaimer. Return ONLY the complete updated JSON."
    )
    user = f"STANDARDS UPDATE request.\nCurrent database:\n{json.dumps(current, indent=1)}"
    return system, user


def _standards_finalize(raw: str) -> dict:
    try:
        proposal = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as exc:
        raise SpecGenerationError(f"Proposal was not valid JSON: {exc}") from None
    problems = validate_standards_db(proposal)
    if problems:
        raise SpecGenerationError("Structural problems: " + "; ".join(problems[:8]))
    return {
        "proposal": proposal,
        "changes": _diff_standards(load_standards(), proposal),
        "note": STANDARDS_NOTE,
    }


def propose_standards_update() -> dict:
    """Ask the LLM to review/extend the US-code standards DB. The proposal is
    structurally validated; the caller decides whether to commit it to
    GitHub or hand it back as a download.

    Honesty note (surfaced to the user by the UI): the proposal comes from
    the model's knowledge of published standards, not a live web crawl —
    review the cited sections before relying on it.
    """
    system, user = _standards_prompts()
    try:
        return _standards_finalize(complete(system, user, temperature=0.2, max_tokens=8000))
    except SpecGenerationError as err:  # T2.6-style single retry
        return _standards_finalize(
            complete(system, f"{user}\n\nYour previous answer failed: {err}\nReturn corrected JSON only.",
                     temperature=0.2, max_tokens=8000)
        )


# ---------------------------------------------------------------------------
# Streaming pipelines — yield raw LLM text as it arrives so the UI can show
# generation live, then a sentinel + JSON payload with the validated result.
# ---------------------------------------------------------------------------

def _stream_pipeline(system, user, finalize, retry: bool = True):
    payload = None
    try:
        parts = []
        for chunk in complete_stream(system, user):
            parts.append(chunk)
            yield chunk
        try:
            payload = {"ok": True, "result": finalize("".join(parts))}
        except SpecGenerationError as err:
            if not retry:
                payload = {"ok": False, "error": str(err)}
            else:
                yield f"\n\n[validation failed — retrying: {err}]\n\n"
                retry_user = (
                    f"{user}\n\nYour previous answer failed validation with this "
                    f"error:\n{err}\n\nReturn the corrected JSON only."
                )
                parts = []
                for chunk in complete_stream(system, retry_user):
                    parts.append(chunk)
                    yield chunk
                try:
                    payload = {"ok": True, "result": finalize("".join(parts))}
                except SpecGenerationError as err2:
                    payload = {"ok": False, "error": str(err2)}
    except LLMError as exc:
        payload = {"ok": False, "error": str(exc)}
    yield STREAM_SENTINEL + json.dumps(payload)


def stream_generate_spec(prompt: str, code_mode: str = "strict"):
    return _stream_pipeline(
        _system_prompt(code_mode), f"Request: {prompt}",
        lambda raw: _postprocess(raw, code_mode),
    )


def stream_refine_spec(spec: dict, message: str, code_mode: str = "strict"):
    user = (
        f"Here is the current AssetSpec:\n{json.dumps(spec, separators=(',', ':'))}\n\n"
        f"Apply this change and return the FULL updated AssetSpec JSON "
        f"(keep everything else identical, including ids):\n{message}"
    )
    return _stream_pipeline(
        _system_prompt(code_mode), user, lambda raw: _postprocess(raw, code_mode)
    )


def stream_install_guide(spec: dict):
    system, user = _install_guide_prompts(spec)
    return _stream_pipeline(system, user, lambda raw: {"guide": raw.strip()}, retry=False)


def stream_update_standards(commit_fn):
    """``commit_fn(proposal_dict) -> dict`` merges commit status into the result."""
    system, user = _standards_prompts()

    def finalize(raw: str) -> dict:
        result = _standards_finalize(raw)
        result.update(commit_fn(result["proposal"]))
        return result

    return _stream_pipeline(system, user, finalize)
