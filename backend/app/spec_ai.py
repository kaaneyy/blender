"""Prompt→AssetSpec and refine pipelines (T2.1–T2.3, T2.5, T2.6).

Flow: build a system prompt grounded in the JSON Schema, the US-code
standards DB (T2.2 — ranges come from the DB, not model memory), the curated
builder catalog, and a few-shot custom example → call the provider → strip
fences → parse → strict JSON-Schema validation (T7.4, rejects unknown
fields) → geometry sanity check → US-code validation/clamping (T2.3).

Failure recovery (T2.6, hardened): every failure is CLASSIFIED — truncated
output, invalid JSON, schema violation (with the offending path/field),
broken expression (with the ids that ARE available), unbuildable geometry,
floating parts, transient provider errors — and the pipeline re-prompts
with a targeted correction plus the full error history, up to
``MAX_ATTEMPTS`` (3) model calls total. The final attempt is lenient about
buildability so a stubborn-but-parseable spec ships with warnings instead
of failing the whole generation. Transient provider errors (429/5xx/
timeouts) retry the same prompt; configuration errors (missing API key)
abort immediately.
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
from blender.builders.connectivity import (  # noqa: E402
    buildability_errors,
    check_buildability,
)
import blender.builders  # noqa: E402,F401  (registers curated builders)

from .llm import LLMError, complete, complete_stream, strip_reasoning  # noqa: E402

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


#: Maximum model calls per generation (first attempt + corrective retries).
MAX_ATTEMPTS = 3


class SpecGenerationError(RuntimeError):
    """LLM produced output that could not be turned into a valid spec.

    ``kind`` labels the failure family (truncated / not_json / schema /
    build / buildability / provider / unknown) and ``hint`` carries the
    targeted correction instruction the retry prompt hands back to the
    model — the difference between "error, try again" and telling it
    exactly what to change."""

    def __init__(self, message: str, kind: str = "unknown", hint: str = ""):
        super().__init__(message)
        self.kind = kind
        self.hint = hint


def _system_prompt(code_mode: str) -> str:
    standards = load_standards()
    standards.pop("_meta", None)
    # fastener tables are for the hardware generator, not the LLM — don't
    # spend prompt tokens on them
    standards.pop("_connections", None)
    return f"""You convert user requests into AssetSpec JSON for a parametric 3D asset generator (street furniture, lighting, signage, props of any kind).

OUTPUT RULES
- Return ONLY the AssetSpec JSON object. No prose, no markdown fences.
- It must validate against this JSON Schema (unknown fields are rejected):
{json.dumps(ASSET_SPEC_SCHEMA, separators=(",", ":"))}

GEOMETRY RULES
- Curated builders exist for these asset_types; when the request matches one, use it with EXACTLY these parameter/toggle ids and material slots, and DO NOT include "primitives":
{json.dumps(BUILTIN_BUILDERS, indent=1)}
- For ANY other asset, set a semantic asset_type (lowercase snake_case; reuse a standards key below when one fits) and model the geometry yourself in the "primitives" array. Kinds: box, cylinder, cone, sphere, and the fabrication kinds — lathe (revolve a profile: lantern globes, finials, domes, planters, decorative bases), sweep (a smooth tapered tube along a path: mast arms, handrails, curved members — ONE sweep beats a stack of cylinders), loft (taper between two cross-sections: cobra heads, flared transitions), tube (hollow pipe with wall thickness — poles/bollards/arms are never solid). Use "cut": true to subtract a primitive (bolt holes, slots) and "array" {{count, step}} for even repetition (pickets, slats).
- Primitive dimensions are METERS. +Z is up. The asset stands on the ground plane z=0 (nothing below z=0). A cylinder/cone's axis is Z; "location" is its center, so a post of depth H sits at z=H/2. rotation is Euler XYZ radians.
- Every numeric field in a primitive may instead be a string expression over parameter/toggle ids, e.g. "pole_height/2" or "seat_height + 0.02". Allowed: numbers, ids, + - * / ( ), min(), max(), abs(). Toggle ids evaluate to 1/0. Parameter values are pre-converted to meters regardless of their display unit.
- EVERY major dimension a designer would tweak must be a parameter (slider) referenced from expressions — never hard-code it. Optional features (backrest, second arm, finial, ...) must be toggles gating primitives via "visible_if".
- Give every primitive a component (nested grouping in exports) and a material_slot. 10–40 primitives is the sweet spot; favor simple, readable massing over micro-detail.
- COMPLETENESS: if the request names several parts or features ("a car roof with slanted solar panels"), EVERY named part MUST exist as its own component with its own primitives, parameters, and material slot. Re-read the request before answering and check nothing was dropped.

TILT, SLOPE, CURVE (the model is not limited to upright boxes)
- "rotation" is Euler XYZ radians and accepts expressions. A panel tilted toward +X by an adjustable angle: expose a unit-less parameter (e.g. {{"id": "panel_tilt", "label": "Panel Tilt", "type": "slider", "min": 0, "max": 60, "step": 1, "value": 30}} — degrees, NO unit field) and use "rotation": [0, "panel_tilt * 0.01745", 0] on a thin box.
- Parameters WITHOUT a "unit" field are dimensionless and reach expressions unchanged (use for angles in degrees, counts, ratios). Parameters WITH a length unit arrive in meters.
- A tilted part's supports must still reach INTO it: raise/extend the mounting posts so they interpenetrate the rotated panel near its low edge.
- Sloped roofs: one thin rotated box per plane (two for a gable). Curves/arcs (arched arms, hoops, curved backrests): approximate with 5–8 short cylinder/box segments, each positioned and rotated a step further along the path. Domes: sphere; tapers: cone with radius_bottom/radius_top.

FORM, PROPORTION & ARCHETYPES (make it read as the real fixture, not a box)
- Map the request to a known archetype and use its characteristic forms: cobra-head street light (tapered swept mast arm + lofted teardrop head); acorn/teardrop post-top lamp (lathe globe on a fluted post with a finial); shoebox area light (thin lofted housing); bishop's-crook lamp (curved swept arm); bollard (tube with a lathe dome cap); planter/urn (lathe vase profile); bench (slats on rails on legs). Prefer lathe/sweep/loft for anything round, curved, or decorative.
- Give members REAL structural proportions, not equal sticks: express relationships as ratios in expressions — a pole base diameter ≈ 1.8× its top (taper), a cantilevered arm tapering to ~60% at the tip (sweep radius_end ≈ 0.6× radius), a post-top globe ≈ 1.2–1.6× the post diameter. Slender vertical members read as engineered; chunky uniform ones read as toy.
- For each component, pick the connection type to its neighbor and RECORD it in the top-level "connections" array (see CONNECTION RULES) — that declaration drives the joint hardware the app generates. Model larger visible details (telescoping sleeves, brackets) as primitives where a real one would be seen.

CONNECTION RULES (think like a fabricator — every joint must be buildable in real life)
- Every part must be physically supported through a real load path down to the ground. Before finishing, walk through your primitives joint by joint and ask: what holds this part, and how would a crew actually fasten it on site?
- Parts that join MUST interpenetrate by 10-20 mm at the joint (e.g. a leg whose top is inside the rail it supports, a rail whose top is inside the slats it carries). Never leave parts floating or merely touching at a zero-thickness face — the app places hardware exactly where parts truly overlap.
- DECLARE every real joint in the top-level "connections" array: {{"a": "component" (or "component/part"), "b": "component" (or "ground"), "type": ...}}. Types and when to use them:
  * "anchor_base" — a vertical STRUCTURAL member landing at grade (pole, sign post, heavy frame leg): the app generates the full base plate + anchor-bolt circle + grout + gussets there. Use b: "ground".
  * "band_clamp" — a horizontal arm/bracket clamping a round pole (split saddle band, bolted ears).
  * "slip_fit" — round-over-round telescoping fits (post-top luminaire over a pole tenon): collar + set screws.
  * "carriage_bolt" — wood decking/slats on a metal frame: dome heads proud of the timber, nuts below the steel.
  * "through_bolt" — the general bolted lap joint (washers + hex head/nut).
  * "flange_splice" — collinear members joined end-to-end (two mating discs + a bolt circle).
  * "weld" — shop-welded steel: a weld bead is shown and NO bolts appear.
  * "lag_screw" — a metal fitting screwed into timber (hex head one side, no nut).
  * "none" — concealed joinery or cast-integral (wood-to-wood furniture joints, decorative caps): no visible hardware.
  Undeclared joints get inferred hardware from geometry, so declare intent wherever inference could guess wrong — especially welded joints and anything that must NOT show bolts.
- Light non-structural furniture must NOT get industrial anchors: declare {{"a": "<leg component>", "b": "ground", "type": "none"}}. Only structural verticals at grade get "anchor_base".
- Still model load-path geometry as primitives: cross rails or stretchers between legs so seat/deck boards have something to bolt to; brackets or collars where members meet at right angles. A slat can NOT attach to a leg it never touches — add the rail.
- Nothing may extend below z=0; grade-level anchorage comes from an "anchor_base" connection (or a modeled plate/footing AT z=0).

MATERIALS
- Presets: {", ".join(MATERIAL_PRESETS)}.
- Per slot you may override: color (hex), metalness 0-1 (reflectivity), roughness 0-1, uv_scale 0.05-20 (texture tiling), emission 0-20 (glow — use 2-6 for lamp lenses), weathering 0-1 (0 factory-new, 1 heavily aged — darkens toward grime and dulls the surface), and finish (cast | machined | sheet | rough) describing the fabrication surface of that slot's parts.
- Give each material slot a finish that matches its parts: cast for cast-iron bases/finials, machined for turned or milled fittings, sheet for luminaire housings and panels, rough for galvanized poles and raw concrete.
- Honor style/material requests from the prompt: "weathered bronze" → cast_iron preset + color #6e5b3f + finish "cast" + weathering 0.5; "brand-new powder-coat" → weathering 0. Only add weathering when the request implies age or the setting (movie/animation dressing, an old park).

US CODE STANDARDS (T2.2 — use these ranges and code_refs for slider min/max/default whenever a parameter maps to one; do not invent limits):
{json.dumps(standards, separators=(",", ":"))}

- Set "code_mode": "{code_mode}". Set "units" to what the user implies (default imperial). Parameters keep human units (ft/in for imperial).

EXAMPLE — curated builder:
{FEW_SHOT_BUILTIN}

EXAMPLE — custom primitives (the "anything" path):
{FEW_SHOT_CUSTOM}"""


# ---------------------------------------------------------------------------
# Prompt refiner — turns a vague request into a precise design brief before
# the spec generator sees it ("a lamp" → asset type, style, dimensions with
# units, materials, options, connections).
# ---------------------------------------------------------------------------

ENHANCE_SYSTEM = (
    "You are the design-brief writer for a parametric 3D asset generator for "
    "street furniture, lighting, signage, and props. Rewrite the user's request "
    "into one precise, buildable brief. Name the asset type; a coherent style; "
    "overall dimensions WITH units, choosing sensible values within US code "
    "limits where they apply (AASHTO/MUTCD/ADA/IBC); per-part materials and "
    "finishes; 2-4 optional features worth exposing as toggles; and how the "
    "parts connect and mount to the ground (base plate, rails, clamps). Keep "
    "EVERY explicit detail the user gave — only add what is missing. If the "
    "request names several parts or features ('a car roof with slanted solar "
    "panels'), the brief MUST explicitly cover every one of them, with tilt/"
    "slope angles in degrees for slanted or curved elements and how each part "
    "mounts to the others — name the fabrication connection per joint (anchor "
    "base at grade, band clamp on the pole, slip-fit tenon, weld, carriage "
    "bolts into timber, through-bolts). Plain prose, at most 120 words, no "
    "JSON, no lists, no preamble."
)


def _enhance_user(prompt: str) -> str:
    return f"ENHANCE PROMPT.\nRequest: {prompt}"


def enhance_prompt(prompt: str, model: str | None = None) -> str:
    """One extra AI pass: vague request in, well-written design brief out.
    The brief is an enhancement, not a requirement — if the provider fails
    here, generation proceeds from the raw prompt instead of dying."""
    try:
        brief = complete(ENHANCE_SYSTEM, _enhance_user(prompt),
                         temperature=0.5, max_tokens=400, model=model).strip()
    except LLMError:
        return prompt
    return brief or prompt


def _last_balanced_object(text: str) -> str | None:
    """Return the last top-level ``{...}`` in ``text`` that parses as JSON, or
    None. Unlike a first-``{``/last-``}`` slice, this is not fooled by stray
    braces a reasoning model leaves in the prose around its real answer (e.g. a
    worked example it typed while thinking)."""
    result = None
    depth = start = 0
    in_str = esc = False
    start = -1
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                chunk = text[start : i + 1]
                try:
                    json.loads(chunk)
                    result = chunk  # keep the last one that actually parses
                except json.JSONDecodeError:
                    pass
                start = -1
    return result


def _strip_fences(raw: str) -> str:
    # A thinking model prepends a chain of thought (possibly full of braces)
    # before the JSON — drop it first so it can't confuse extraction.
    text = strip_reasoning(raw).strip()
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # prefer the last balanced object that parses; fall back to an outer slice
    candidate = _last_balanced_object(text)
    if candidate is not None:
        return candidate
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    return text


# ---------------------------------------------------------------------------
# Error understanding — classify each failure and write the targeted
# correction the retry prompt hands back to the model.
# ---------------------------------------------------------------------------

def _looks_truncated(text: str) -> bool:
    """True when the reply contains JSON that never closes — the model ran
    out of output budget mid-spec (the classic failure 'after creating lots
    of code')."""
    depth = 0
    in_str = esc = False
    opened = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
            opened = True
        elif ch == "}" and depth > 0:
            depth -= 1
    return opened and (depth > 0 or in_str)


def _schema_hint(exc: jsonschema.ValidationError) -> str:
    """Turn a jsonschema error into an instruction the model can act on."""
    path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
    if exc.validator == "additionalProperties":
        return (
            f"At {path}: {exc.message}. The schema REJECTS unknown fields — "
            f"remove the unexpected field(s) or move the information into a "
            f"field the schema defines."
        )
    if exc.validator == "required":
        return f"At {path}: {exc.message}. Add the missing required field(s)."
    if exc.validator == "enum":
        return (
            f"At {path}: {exc.message}. Use exactly one of the allowed "
            f"values: {json.dumps(exc.validator_value)}."
        )
    if exc.validator in ("type", "pattern", "minItems", "maxItems"):
        return f"At {path}: {exc.message}. Correct that field's shape."
    return f"At {path}: {exc.message}."


def _build_hint(exc: Exception, spec: dict) -> str:
    """Hint for a spec that parsed but failed to build, grounded in what the
    spec actually declares (available ids, primitive requirements)."""
    msg = str(exc)
    ids = [p.get("id") for p in spec.get("parameters") or [] if isinstance(p, dict)]
    ids += [t.get("id") for t in spec.get("toggles") or [] if isinstance(t, dict)]
    known = ", ".join(str(i) for i in ids if i) or "<none — define parameters first>"
    if "Unknown name" in msg or "expression" in msg.lower():
        return (
            f"{msg}. Expressions may ONLY reference these parameter/toggle "
            f"ids: {known}. Fix the expression to use an existing id, add "
            f"the missing parameter, or inline a plain number."
        )
    if "missing params" in msg or "Unknown primitive kind" in msg:
        return (
            f"{msg}. Required params per kind: cylinder(radius, depth), "
            f"cone(radius_bottom, radius_top, depth), box(size), "
            f"sphere(radius), lathe(profile), sweep(path, radius), "
            f"loft(profile_start, profile_end, depth), tube(radius, wall, depth)."
        )
    if "No builder for asset_type" in msg:
        return (
            f"{msg}. Either use a curated asset_type (street_light) with its "
            f"exact parameter ids, or keep your asset_type and include the "
            f"full 'primitives' array."
        )
    return msg


def _postprocess(raw: str, code_mode: str, lenient_buildability: bool = False) -> dict:
    """Parse, schema-validate (T7.4), geometry-check, code-clamp (T2.3), and
    buildability-check (contact graph: floating parts, below-grade geometry,
    dead declarations). Every failure raises a CLASSIFIED
    :class:`SpecGenerationError` whose hint tells the model exactly what to
    fix. Floating parts raise — the deterministic findings feed the retry —
    unless ``lenient_buildability`` (the final attempt), in which case
    they're accepted and surfaced as violations instead, so a stubborn
    generation never bricks."""
    stripped = _strip_fences(raw)
    try:
        spec = json.loads(stripped)
    except json.JSONDecodeError as exc:
        if _looks_truncated(stripped):
            raise SpecGenerationError(
                f"Output was cut off before the JSON finished: {exc}",
                kind="truncated",
                hint=(
                    "Your previous answer ran out of room mid-JSON. Regenerate "
                    "the COMPLETE spec from scratch and make it more compact: "
                    "fewer primitives (aim under 25), use \"array\" "
                    "{count, step} instead of repeating similar primitives, "
                    "shorter names, no comments. Finish the entire JSON object."
                ),
            ) from None
        raise SpecGenerationError(
            f"Output was not valid JSON: {exc}",
            kind="not_json",
            hint=(
                "Return ONLY the AssetSpec JSON object — no prose, no "
                "apologies, no markdown fences, nothing before or after it."
            ),
        ) from None

    if not isinstance(spec, dict):
        raise SpecGenerationError(
            "Output was not a JSON object",
            kind="not_json",
            hint="Return a single AssetSpec JSON object at the top level.",
        )
    spec.setdefault("code_mode", code_mode)

    try:
        jsonschema.validate(spec, ASSET_SPEC_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise SpecGenerationError(
            f"Schema violation at "
            f"{'/'.join(str(p) for p in exc.absolute_path) or '<root>'}: "
            f"{exc.message}",
            kind="schema",
            hint=_schema_hint(exc),
        ) from None

    result = validate_spec(spec)

    try:  # prove the spec actually builds (catches bad expressions/params)
        prims = compute_primitives(result.spec)
    except Exception as exc:
        raise SpecGenerationError(
            f"Spec does not build: {exc}",
            kind="build",
            hint=_build_hint(exc, spec),
        ) from None

    findings = check_buildability(prims, result.spec)
    errors = buildability_errors(findings)
    if errors and not lenient_buildability:
        raise SpecGenerationError(
            "Buildability check failed: "
            + " ".join(f["message"] for f in errors[:4]),
            kind="buildability",
            hint=(
                " ".join(f["message"] for f in errors[:4])
                + " Every part needs a load path to the ground (z=0); joined "
                "parts must interpenetrate 10-20mm. Move/extend the named "
                "parts (or add a connecting member) so they truly overlap."
            ),
        )

    out = result.to_dict()
    if findings:
        out["violations"] = out["violations"] + findings
        if errors:
            out["ok"] = False
    return out


#: LLMError texts that are worth retrying (rate limits, provider hiccups,
#: network trouble) — as opposed to configuration errors (missing API key,
#: unknown provider), which no retry can fix.
_TRANSIENT_LLM_RE = re.compile(
    r"returned (?:429|5\d\d)|timed?[ -]?out|timeout|request failed|"
    r"network|connection|temporarily", re.IGNORECASE,
)


def _transient_llm_error(exc: LLMError) -> bool:
    return bool(_TRANSIENT_LLM_RE.search(str(exc)))


def _correction_user(user: str, attempt: int, history: list, raw: str) -> str:
    """The corrective prompt for attempt N: the original request, the full
    error history (so the model never cycles back to a mistake it already
    made), the targeted fix instruction for the latest failure, and — when
    fixing in place is possible — the previous answer to repair. Truncated
    answers are NOT echoed back: re-feeding cut-off JSON just burns the
    budget that caused the truncation."""
    spec_errors = [e for e in history if e.kind != "provider"]
    if not spec_errors:
        return user  # only provider hiccups so far — same prompt again
    lines = [user, f"\nATTEMPT {attempt} of {MAX_ATTEMPTS}."]
    if len(spec_errors) > 1:
        lines.append("Your previous answers failed, in order:")
        lines += [f"{i}. [{e.kind}] {e}" for i, e in enumerate(spec_errors, start=1)]
        lines.append("Do not repeat ANY of these mistakes.")
    latest = spec_errors[-1]
    lines.append(f"\nLatest failure [{latest.kind}]: {latest}")
    lines.append(f"HOW TO FIX IT: {latest.hint or 'Correct the error above.'}")
    if latest.kind != "truncated" and raw:
        lines.append(f"\nYour previous answer (repair it in place):\n{raw[:6000]}")
    lines.append("\nReturn the corrected COMPLETE AssetSpec JSON only.")
    return "\n".join(lines)


def _complete_with_retries(system: str, user: str, finalize, *,
                           model: str | None = None, **complete_kwargs) -> dict:
    """Non-streaming attempt loop: call the model, ``finalize(raw, lenient)``
    the reply, and on a classified failure re-prompt with a targeted
    correction — up to MAX_ATTEMPTS calls. The last attempt finalizes
    leniently (buildability warnings instead of failure)."""
    history: list = []
    raw = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        message = user if attempt == 1 else _correction_user(user, attempt, history, raw)
        try:
            raw = complete(system, message, model=model, **complete_kwargs)
        except LLMError as exc:
            if attempt == MAX_ATTEMPTS or not _transient_llm_error(exc):
                raise
            history.append(SpecGenerationError(str(exc), kind="provider"))
            continue
        try:
            return finalize(raw, attempt == MAX_ATTEMPTS)
        except SpecGenerationError as err:
            history.append(err)
            if attempt == MAX_ATTEMPTS:
                raise SpecGenerationError(
                    f"Generation failed after {MAX_ATTEMPTS} attempts. "
                    f"Last error: {err}",
                    kind=err.kind, hint=err.hint,
                ) from None
    raise SpecGenerationError("Generation failed", kind="unknown")  # unreachable


def _run(system: str, user: str, code_mode: str, model: str | None = None) -> dict:
    return _complete_with_retries(
        system, user,
        lambda raw, lenient: _postprocess(raw, code_mode, lenient_buildability=lenient),
        model=model,
    )


def generate_spec(prompt: str, code_mode: str = "strict", model: str | None = None) -> dict:
    """T2.1: natural-language prompt → design brief (extra AI pass) →
    validated AssetSpec (+ violations). The brief rides along in the result
    so the UI can show how the request was interpreted."""
    brief = enhance_prompt(prompt, model=model)
    result = _run(_system_prompt(code_mode), f"Request: {brief}", code_mode, model=model)
    result["brief"] = brief
    return result


def refine_spec(spec: dict, message: str, code_mode: str = "strict",
                model: str | None = None) -> dict:
    """T2.5: current spec + chat message → modified, re-validated spec."""
    user = (
        f"Here is the current AssetSpec:\n{json.dumps(spec, separators=(',', ':'))}\n\n"
        f"Apply this change and return the FULL updated AssetSpec JSON "
        f"(keep everything else identical, including ids):\n{message}"
    )
    return _run(_system_prompt(code_mode), user, code_mode, model=model)


#: Focus refinement: deep-detail ONE named area, leave the rest byte-identical.
def _focus_user(spec: dict, area: str) -> str:
    return (
        f"Here is the current AssetSpec:\n{json.dumps(spec, separators=(',', ':'))}\n\n"
        f"FOCUS AREA: {area}\n\n"
        "Work ONLY on the component(s)/part(s) the focus area names. Make that "
        "area substantially more detailed and realistic: split it into finer "
        "sub-parts, add appropriate primitives, and add parameters/toggles that "
        "control JUST that area (new ids only — do not rename or renumber "
        "existing ones). You may add new material slots for the new parts. "
        "CRITICAL: every other component, primitive, parameter, toggle, and "
        "material must stay byte-identical — same ids, same values, same order, "
        "nothing added, removed, or reordered outside the focus area. Keep the "
        "asset_type, name, and units unchanged. Return the FULL updated "
        "AssetSpec JSON."
    )


def focus_spec(spec: dict, area: str, code_mode: str = "strict",
               model: str | None = None) -> dict:
    """Deep-detail one area of the current spec, leaving the rest untouched."""
    return _run(_system_prompt(code_mode), _focus_user(spec, area), code_mode, model=model)


# ---------------------------------------------------------------------------
# Guided 4-step build (Form → Connections → Materials → Working parts)
#
# Step 1 (Form) is the ordinary generate. Steps 2-4 are scoped refinement
# passes, each concerned with exactly ONE aspect and told to leave the others
# alone, so the user can review — and change via prompt or accept — one
# concern at a time. The passes never auto-chain; the UI runs the next one
# only when the user accepts the current step.
# ---------------------------------------------------------------------------

WIZARD_STEP_KEYS = ("connections", "materials", "details")

_WIZARD_DIRECTIVES = {
    "connections": (
        "STEP — CONNECTIONS. Work through this asset joint by joint, from the "
        "ground up, and make every connection real and buildable. For each pair "
        "of touching components, first decide whether a connection is even "
        "needed, then choose the fabrication type a crew would actually use and "
        "DECLARE it in the top-level \"connections\" array with the right type: "
        "anchor_base (a structural vertical meeting grade, b:\"ground\"), "
        "band_clamp (arm on a round pole), slip_fit (a telescoping post-top "
        "fit), carriage_bolt (wood on a metal frame), through_bolt (general "
        "bolted lap), flange_splice (collinear members end-to-end), weld "
        "(shop-welded steel — no bolts shown), lag_screw, or none (concealed "
        "joinery / cast-integral, no visible hardware). Ensure every part has a "
        "real load path down to z=0: add connecting members (rails, stretchers, "
        "brackets, gussets, collars, base plates) ONLY where a part would "
        "otherwise float or have nothing to fasten to. Parts that join MUST "
        "interpenetrate 10-20 mm. Do NOT restyle the asset, change its "
        "materials, or add decorative detail — this pass is only about how it "
        "holds together. Return the FULL updated AssetSpec JSON, keeping the "
        "asset_type, geometry, materials, and all existing ids/values stable "
        "except for the connecting members a real joint requires."
    ),
    "materials": (
        "STEP — MATERIALS. Give every material slot the right preset and surface "
        "properties for the part it covers and the asset's style. For each slot "
        "set: the fitting preset, and where it helps color, metalness, roughness, "
        "uv_scale, emission (2-6 for lit lenses), and finish (cast for cast-iron "
        "bases/finials, machined for turned fittings, sheet for housings/panels, "
        "rough for galvanized poles and concrete). Add weathering ONLY if the "
        "request implies age or setting (an old park, movie dressing). Honor any "
        "style/material words in the original request. Do NOT change geometry, "
        "connections, toggles, or parameters — this pass is only about how the "
        "asset is finished. Return the FULL updated AssetSpec JSON, keeping "
        "every id, value, and part identical outside the materials."
    ),
    "details": (
        "STEP — WORKING PARTS. Detail the functional and adjustable parts of "
        "this asset, one by one and thoroughly. Identify every part that does a "
        "job or moves/adjusts: lights and lenses (give lenses a lamp_lens "
        "material with realistic emission and model the reflector/housing/gasket "
        "if missing), and adjustable features (a street light's banner bracket "
        "or second arm, a sign's changeable panel, a bollard's removable "
        "sleeve). For EACH such part: model its detail geometry properly, and "
        "expose what a user would tune — optional features as toggles gated by "
        "visible_if, and dimensions/angles as sliders — reusing existing ids and "
        "adding new ones only for genuinely new controls. Keep the overall form, "
        "connections, and materials stable except where a newly detailed part "
        "needs them. Return the FULL updated AssetSpec JSON."
    ),
}


def _wizard_user(spec: dict, step: str, message: str) -> str:
    directive = _WIZARD_DIRECTIVES[step]
    note = ""
    if message and message.strip():
        note = (
            "\n\nThe user reviewed this step and asks for this specific change "
            "(honor it within this step's scope):\n" + message.strip()
        )
    return (
        f"Here is the current AssetSpec:\n{json.dumps(spec, separators=(',', ':'))}"
        f"\n\n{directive}{note}"
    )


def wizard_step(spec: dict, step: str, message: str = "",
                code_mode: str = "strict", model: str | None = None) -> dict:
    """One guided-build step: a scoped refinement of ``spec`` (connections /
    materials / details), optionally steered by the user's ``message``."""
    if step not in _WIZARD_DIRECTIVES:
        raise SpecGenerationError(
            f"Unknown build step {step!r} (expected one of {', '.join(WIZARD_STEP_KEYS)})"
        )
    return _run(_system_prompt(code_mode), _wizard_user(spec, step, message),
                code_mode, model=model)


# ---------------------------------------------------------------------------
# AI connection review — the "Check connections with AI" button.
#
# The deterministic auditor (blender/builders/audit.py) measures what it can
# prove; this pass adds fabricator JUDGMENT on top: joint types that don't
# suit the materials or geometry, missing declarations where inference could
# guess wrong, hardware that shouldn't exist, assembly-access problems. The
# AI must answer in the auditor's own findings format — the same nudge /
# declare / undeclare fix ops — so the frontend previews and applies its
# proposals through the exact same confirmation machinery: hover the Apply
# button to see the result in 3D, and NOTHING changes until the user clicks.
# Every proposal is sanitized against the real component names, bounded, and
# test-built before it ever reaches the browser.
# ---------------------------------------------------------------------------

from blender.builders.hardware import CONNECTION_TYPES  # noqa: E402

#: findings kept per review, most important first
MAX_REVIEW_FINDINGS = 10
#: largest move (m) an AI fix may propose per axis
MAX_NUDGE = 0.5

REVIEW_SYSTEM = """You are a senior fabrication reviewer for street furniture, lighting, and site structures — the person who signs off a shop drawing before it goes to the floor. Review the connections of the AssetSpec you are given like you would on a real job.

You receive:
1. the AssetSpec JSON,
2. the joint schedule the app generated (the hardware that will really appear),
3. the deterministic checker's findings — these are already measured facts; do NOT repeat them. Your value is judgment beyond them.

REVIEW FOR
- Joint TYPE suitability: does each declared/inferred connection match the materials and geometry? (wood on metal → carriage_bolt; a pipe standing on a plate → weld or anchor_base, never a bolt down its own axis; round-on-round telescoping → slip_fit; arm on round pole → band_clamp; decorative caps/trim → none)
- Missing declarations where geometric inference could guess wrong, and joints that should be suppressed (declare type "none").
- Real-life assembly: could a crew actually reach and torque every fastener? Is anything trapped, inaccessible, or fastened to a part that can't take it?
- Small placement problems a modest move would fix (parts that should seat 10-20mm deeper, hardware clashing with a neighbor).

OUTPUT — return ONLY this JSON object, no prose, no fences:
{"findings": [
  {"severity": "error" | "warning",
   "title": "<one short sentence naming the problem>",
   "detail": "<why it isn't buildable / right, in plain fabrication terms>",
   "component": "<component name to highlight, optional>",
   "joint": <joint id from the schedule, optional>,
   "fix": {  // OPTIONAL — omit when there is no safe mechanical fix
     "summary": "<imperative one-liner of the change>",
     "before": "<state now>",
     "after": "<state after the fix>",
     "ops": [  // the ONLY allowed operations:
       {"op": "nudge", "key": "<component or component/part>", "delta": [x, y, z]},   // meters, each axis <= 0.5
       {"op": "declare", "a": "<component or component/part>", "b": "<component or 'ground'>", "type": "<connection type>"},
       {"op": "undeclare", "a": "<component>", "b": "<component>"}
     ]}}
]}

RULES
- Connection types for "declare": anchor_base, through_bolt, flange_splice, band_clamp, slip_fit, weld, carriage_bolt, lag_screw, none.
- Reference ONLY component/part names that exist (the user message lists them). Anything else is discarded.
- At most 8 findings, most important first. If the connections are sound, return {"findings": []} — do not invent problems.
- Fixes must be conservative: prefer a declaration change over moving parts; keep nudges small (typically under 0.1)."""


def _review_user(spec: dict, det: dict) -> str:
    from blender.builders.base import compute_primitives as _cp
    from blender.builders.schedule import joint_schedule

    prims = [p for p in _cp(spec) if p.component != "hardware" and not p.cut]
    comps = sorted({p.component for p in prims})
    parts = sorted({f"{p.component}/{p.name}" for p in prims})
    det_brief = [
        {k: f.get(k) for k in ("severity", "kind", "title", "detail")}
        for f in det["findings"]
    ]
    return (
        f"REVIEW CONNECTIONS.\n"
        f"AssetSpec:\n{json.dumps(spec, separators=(',', ':'))}\n\n"
        f"Joint schedule (the hardware the app generated):\n"
        f"{json.dumps(joint_schedule(spec), separators=(',', ':'))}\n\n"
        f"Deterministic checker findings (already measured — do NOT repeat "
        f"them):\n{json.dumps(det_brief, separators=(',', ':'))}\n\n"
        f"Component names you may reference: {json.dumps(comps)}\n"
        f"Part paths you may reference: {json.dumps(parts)}"
    )


def _sanitize_op(op, comps: set, parts: set):
    """One validated fix op, or None. Ops may only touch names that exist,
    with bounded moves and known connection types — an AI proposal can never
    reach outside the vocabulary the deterministic auditor already uses."""
    if not isinstance(op, dict):
        return None
    kind = op.get("op")
    refs = comps | parts
    if kind == "nudge":
        key, delta = op.get("key"), op.get("delta")
        if key not in refs or not isinstance(delta, list) or len(delta) != 3:
            return None
        try:
            d = [float(v) for v in delta]
        except (TypeError, ValueError):
            return None
        if any(abs(v) > MAX_NUDGE for v in d) or all(abs(v) < 1e-9 for v in d):
            return None
        return {"op": "nudge", "key": key, "delta": d}
    if kind in ("declare", "undeclare"):
        a, b = op.get("a"), op.get("b")
        if not isinstance(a, str) or not isinstance(b, str):
            return None
        if a not in refs or (b not in refs and b != "ground"):
            return None
        if kind == "undeclare":
            return {"op": "undeclare", "a": a, "b": b}
        if op.get("type") not in CONNECTION_TYPES:
            return None
        return {"op": "declare", "a": a, "b": b, "type": op["type"]}
    return None


def _sanitize_review_finding(f, i: int, comps: set, parts: set):
    if not isinstance(f, dict):
        return None
    title = str(f.get("title") or "").strip()
    if not title:
        return None
    severity = f.get("severity") if f.get("severity") in ("error", "warning") else "warning"
    component = f.get("component")
    if not isinstance(component, str) or (component not in comps and component not in parts):
        component = None
    joint = f.get("joint") if isinstance(f.get("joint"), int) else None
    fix = None
    rf = f.get("fix")
    if isinstance(rf, dict):
        ops = []
        for op in rf.get("ops") or []:
            clean = _sanitize_op(op, comps, parts)
            if clean:
                ops.append(clean)
        if ops:
            fix = {
                "summary": str(rf.get("summary") or title)[:200],
                "before": str(rf.get("before") or "")[:200],
                "after": str(rf.get("after") or "")[:200],
                "ops": ops,
            }
    return {"id": f"ai:{i}", "severity": severity, "kind": "ai_review",
            "title": title[:160], "detail": str(f.get("detail") or "")[:600],
            "component": component, "joint": joint, "fix": fix}


def _review_finalize(raw: str, spec: dict, det: dict) -> dict:
    """Parse + sanitize the AI's findings and PROVE the proposals are safe:
    apply every surviving fix to a copy of the spec and test-build it. Any
    failure raises a classified error the retry loop feeds back."""
    from blender.builders.audit import apply_audit_fixes
    from blender.builders.base import compute_primitives as _cp

    stripped = _strip_fences(raw)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        if _looks_truncated(stripped):
            raise SpecGenerationError(
                f"Review was cut off before the JSON finished: {exc}",
                kind="truncated",
                hint="Return the COMPLETE findings JSON — fewer findings, "
                     "shorter detail text.",
            ) from None
        raise SpecGenerationError(
            f"Review was not valid JSON: {exc}",
            kind="not_json",
            hint='Return ONLY the {"findings": [...]} JSON object — no '
                 "prose, no fences.",
        ) from None

    findings_raw = data.get("findings") if isinstance(data, dict) else data
    if not isinstance(findings_raw, list):
        raise SpecGenerationError(
            'Review JSON did not contain a "findings" array',
            kind="schema",
            hint='Answer with exactly {"findings": [...]} (an empty array '
                 "when nothing is wrong).",
        )

    prims = [p for p in _cp(spec) if p.component != "hardware" and not p.cut]
    comps = {p.component for p in prims}
    parts = {f"{p.component}/{p.name}" for p in prims}
    findings = []
    for f in findings_raw:
        clean = _sanitize_review_finding(f, len(findings) + 1, comps, parts)
        if clean:
            findings.append(clean)
        if len(findings) >= MAX_REVIEW_FINDINGS:
            break

    fixable = [f for f in findings if f["fix"]]
    if fixable:
        try:  # the proposals must leave a spec that still builds
            _cp(apply_audit_fixes(spec, fixable))
        except Exception as exc:
            raise SpecGenerationError(
                f"Applying the proposed fixes breaks the build: {exc}",
                kind="build",
                hint="Propose smaller/simpler ops (or drop the fix and "
                     "report the finding without one).",
            ) from None

    return {"findings": findings, "joints": det["joints"],
            "components": det["components"]}


def review_connections(spec: dict, model: str | None = None) -> dict:
    """AI fabrication review of the spec's connections, answered in the
    deterministic auditor's findings format (same fix-op vocabulary), so the
    UI can preview and apply proposals through the same confirmation flow."""
    from blender.builders.audit import audit_connections

    det = audit_connections(spec)
    return _complete_with_retries(
        REVIEW_SYSTEM, _review_user(spec, det),
        lambda raw, lenient: _review_finalize(raw, spec, det),
        model=model,
    )


def stream_review_connections(spec: dict, model: str | None = None):
    """Streaming twin of :func:`review_connections`."""
    from blender.builders.audit import audit_connections

    det = audit_connections(spec)
    return _stream_pipeline(
        REVIEW_SYSTEM, _review_user(spec, det),
        lambda raw, lenient=False: _review_finalize(raw, spec, det),
        model=model,
    )


# ---------------------------------------------------------------------------
# Installation guide
# ---------------------------------------------------------------------------

def _install_guide_prompts(spec: dict) -> tuple:
    """(system, user, joint_schedule): the user message embeds the generated
    joint schedule so the guide documents the REAL fasteners — the fix for
    guides that invented bolt sizes the geometry never had."""
    from blender.builders.schedule import joint_schedule

    standards = load_standards()
    relevant = standards.get(spec.get("asset_type", ""), {})
    schedule = joint_schedule(spec)
    system = (
        "You are a licensed site-furnishing installation specialist writing for a "
        "homeowner/contractor audience. Produce a clear, numbered installation guide "
        "in Markdown for the asset described by the AssetSpec JSON you are given. "
        "Structure: ## Overview (what it is, overall dimensions in ft/in AND meters), "
        "## Tools & materials, ## Site preparation (foundation/footing sizing guidance), "
        "## Assembly sequence (reference the spec's component names in order, with "
        "hardware: anchor bolts, nuts, washers, torque ranges), ## Joint schedule "
        "(a Markdown table of the generated joint schedule you were given: joint id, "
        "connection type, the two members, fastener, count, torque), ## Connections "
        "(walk through EVERY joint in that schedule: which two components meet, the "
        "exact fastener from the schedule, and exactly how it is executed on site — "
        "drilled, through-bolted, band-clamped, slip-fitted, welded, torqued, "
        "embedded), ## Code compliance "
        "checklist (cite the code_refs from the spec/standards, with the actual limits), "
        "## Inspection & maintenance. Use ONLY dimensions derivable from the spec and "
        "ONLY the fasteners in the joint schedule; do not invent sizes. Include a "
        "short safety disclaimer that a licensed engineer must approve structural "
        "anchoring for public installations."
    )
    user = (
        f"INSTALL GUIDE request.\nAssetSpec:\n{json.dumps(spec, separators=(',', ':'))}\n\n"
        f"Applicable standards entry:\n{json.dumps(relevant, separators=(',', ':'))}\n\n"
        f"Joint schedule (the hardware the app actually generated — cite EXACTLY "
        f"these fasteners, counts, and torque values):\n"
        f"{json.dumps(schedule, separators=(',', ':'))}"
    )
    return system, user, schedule


def generate_install_guide(spec: dict) -> tuple:
    """(guide_markdown, joint_schedule): plain-language installation
    instructions grounded in the asset's actual dimensions, components, code
    citations, and the generated connection hardware."""
    system, user, schedule = _install_guide_prompts(spec)
    return complete(system, user, temperature=0.3).strip(), schedule


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
        "and keep the _meta.disclaimer. Keep any '_connections' section EXACTLY "
        "as-is (it is maintained by hand). Return ONLY the complete updated JSON."
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
    # the fastener tables are maintained by hand, not the AI refresh — carry
    # them over verbatim if the proposal dropped them
    if isinstance(proposal, dict) and "_connections" not in proposal:
        current = load_standards().get("_connections")
        if current:
            proposal["_connections"] = current
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
    return _complete_with_retries(
        system, user, lambda raw, lenient: _standards_finalize(raw),
        temperature=0.2, max_tokens=8000,
    )


# ---------------------------------------------------------------------------
# Streaming pipelines — yield raw LLM text as it arrives so the UI can show
# generation live, then a sentinel + JSON payload with the validated result.
# ---------------------------------------------------------------------------

#: Human labels for the attempt banner the stream shows between retries.
_KIND_LABEL = {
    "truncated": "the answer was cut off — regenerating more compactly",
    "not_json": "the answer wasn't clean JSON",
    "schema": "fixing a schema violation",
    "build": "fixing geometry that doesn't build",
    "buildability": "fixing floating/unsupported parts",
    "provider": "the AI provider hiccuped — retrying",
}


def _stream_pipeline(system, user, finalize, retry: bool = True, model: str | None = None):
    """``finalize(raw, lenient=False)`` turns the streamed text into the
    result payload. On a classified failure the pipeline announces what went
    wrong and what it's fixing, then re-prompts with the targeted correction
    — up to MAX_ATTEMPTS model calls. The final attempt finalizes leniently
    so a spec that still fails only the buildability check ships with
    warnings instead of dying."""
    payload = None
    max_attempts = MAX_ATTEMPTS if retry else 1
    history: list = []
    raw = ""
    for attempt in range(1, max_attempts + 1):
        message = user if attempt == 1 else _correction_user(user, attempt, history, raw)
        parts = []
        try:
            for chunk in complete_stream(system, message, model=model):
                parts.append(chunk)
                yield chunk
        except LLMError as exc:
            if attempt == max_attempts or not _transient_llm_error(exc):
                payload = {"ok": False, "error": str(exc), "kind": "provider",
                           "attempts": attempt}
                break
            history.append(SpecGenerationError(str(exc), kind="provider"))
            yield (f"\n\n[attempt {attempt} of {max_attempts} failed — "
                   f"{_KIND_LABEL['provider']}]\n\n")
            continue
        raw = "".join(parts)
        try:
            payload = {"ok": True,
                       "result": finalize(raw, attempt == max_attempts),
                       "attempts": attempt}
            break
        except SpecGenerationError as err:
            history.append(err)
            if attempt == max_attempts:
                failure = (
                    f"Generation failed after {max_attempts} attempts. "
                    f"Last error: {err}" if retry else str(err)
                )
                payload = {"ok": False, "error": failure, "kind": err.kind,
                           "attempts": attempt}
                break
            label = _KIND_LABEL.get(err.kind, "fixing the reported error")
            yield (f"\n\n[attempt {attempt} of {max_attempts} failed — "
                   f"{label}: {err}]\n\n[attempt {attempt + 1} of "
                   f"{max_attempts}]\n\n")
    yield STREAM_SENTINEL + json.dumps(payload)


def stream_generate_spec(prompt: str, code_mode: str = "strict", model: str | None = None):
    """Two visible stages in one stream: the brief being written, then the
    spec being designed from it."""

    def gen():
        yield "[refining your request into a design brief]\n\n"
        parts = []
        try:
            for chunk in complete_stream(ENHANCE_SYSTEM, _enhance_user(prompt),
                                         temperature=0.5, max_tokens=400, model=model):
                parts.append(chunk)
                yield chunk
        except LLMError as exc:
            # the brief is optional — configuration errors still abort (the
            # spec pass would hit them too), but a transient hiccup here just
            # means designing straight from the raw request
            if not _transient_llm_error(exc):
                yield STREAM_SENTINEL + json.dumps(
                    {"ok": False, "error": str(exc), "kind": "provider"})
                return
            parts = []
            yield "\n[brief pass unavailable — designing from your request as-is]\n"
        brief = "".join(parts).strip() or prompt
        yield "\n\n[designing the asset from the brief]\n\n"

        def finalize(raw: str, lenient: bool = False) -> dict:
            result = _postprocess(raw, code_mode, lenient_buildability=lenient)
            result["brief"] = brief
            return result

        yield from _stream_pipeline(
            _system_prompt(code_mode), f"Request: {brief}", finalize, model=model
        )

    return gen()


def stream_refine_spec(spec: dict, message: str, code_mode: str = "strict",
                       model: str | None = None):
    user = (
        f"Here is the current AssetSpec:\n{json.dumps(spec, separators=(',', ':'))}\n\n"
        f"Apply this change and return the FULL updated AssetSpec JSON "
        f"(keep everything else identical, including ids):\n{message}"
    )
    return _stream_pipeline(
        _system_prompt(code_mode), user,
        lambda raw, lenient=False: _postprocess(raw, code_mode, lenient_buildability=lenient),
        model=model,
    )


def stream_focus_spec(spec: dict, area: str, code_mode: str = "strict",
                      model: str | None = None):
    return _stream_pipeline(
        _system_prompt(code_mode), _focus_user(spec, area),
        lambda raw, lenient=False: _postprocess(raw, code_mode, lenient_buildability=lenient),
        model=model,
    )


def stream_wizard_step(spec: dict, step: str, message: str = "",
                       code_mode: str = "strict", model: str | None = None):
    """Streaming twin of :func:`wizard_step` — one guided-build pass."""
    if step not in _WIZARD_DIRECTIVES:
        def bad():
            yield STREAM_SENTINEL + json.dumps({
                "ok": False,
                "error": f"Unknown build step {step!r} "
                         f"(expected one of {', '.join(WIZARD_STEP_KEYS)})",
            })
        return bad()
    return _stream_pipeline(
        _system_prompt(code_mode), _wizard_user(spec, step, message),
        lambda raw, lenient=False: _postprocess(raw, code_mode, lenient_buildability=lenient),
        model=model,
    )


def stream_install_guide(spec: dict):
    system, user, schedule = _install_guide_prompts(spec)
    return _stream_pipeline(
        system, user,
        lambda raw, lenient=False: {"guide": strip_reasoning(raw).strip(),
                                    "joint_schedule": schedule},
        retry=False,
    )


def stream_update_standards(commit_fn):
    """``commit_fn(proposal_dict) -> dict`` merges commit status into the result."""
    system, user = _standards_prompts()

    def finalize(raw: str, lenient: bool = False) -> dict:
        result = _standards_finalize(raw)
        result.update(commit_fn(result["proposal"]))
        return result

    return _stream_pipeline(system, user, finalize)
