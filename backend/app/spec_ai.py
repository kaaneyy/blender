"""Prompt→AssetSpec and refine pipelines (T2.1–T2.3, T2.5, T2.6).

Flow: build a system prompt grounded in the JSON Schema, the US-code
standards DB (T2.2 — ranges come from the DB, not model memory), the curated
builder catalog, and a few-shot custom example → call the provider → strip
fences → parse → strict JSON-Schema validation (T7.4, rejects unknown
fields) → geometry sanity check → US-code validation/clamping (T2.3).

Failure recovery (T2.6, hardened): every failure is CLASSIFIED — truncated
output, invalid JSON, schema violation (with the offending path/field),
broken expression (with the ids that ARE available), unbuildable geometry,
floating parts, dead controls (a slider/toggle the spec exposes that
provably drives no geometry), toggle orphans (a toggle that, switched off,
leaves a still-visible part floating because it wasn't co-gated with the
member it rests on), transient provider errors — and the pipeline
re-prompts with a targeted correction plus the full error history, up to
``MAX_ATTEMPTS`` (3) model calls total. The final attempt is lenient about
buildability so a stubborn-but-parseable spec ships with warnings instead
of failing the whole generation. Transient provider errors (429/5xx/
timeouts) retry the same prompt; configuration errors (missing API key)
abort immediately.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
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
    check_dead_controls,
    check_toggle_dependencies,
)
import blender.builders  # noqa: E402,F401  (registers curated builders)

from .llm import (  # noqa: E402
    DEFAULT_MAX_TOKENS,
    TRUNCATION_MAX_TOKENS_CEILING,
    LLMError,
    active_provider,
    complete,
    complete_stream,
    strip_reasoning,
)

#: Marks the end of the streamed raw text; the JSON payload after it carries
#: the validated result (or the error). The frontend splits on this.
STREAM_SENTINEL = "\n<<<ASSETFORGE_RESULT>>>\n"

ASSET_SPEC_SCHEMA = json.loads(
    (REPO_ROOT / "schemas" / "asset_spec.schema.json").read_text(encoding="utf-8")
)
FEW_SHOT_CUSTOM = (REPO_ROOT / "examples" / "park_bench.json").read_text(encoding="utf-8")
FEW_SHOT_BUILTIN = (REPO_ROOT / "examples" / "street_light.json").read_text(encoding="utf-8")
FEW_SHOT_ARRAYED = (REPO_ROOT / "examples" / "bike_rack.json").read_text(encoding="utf-8")

#: Curated builders and the EXACT parameter/toggle/select ids their geometry
#: understands — nothing else. A request that needs anything beyond this
#: list (styling, an extra feature, a part the builder doesn't model) is NOT
#: a match for the curated builder; see the GEOMETRY RULES curated bullet.
BUILTIN_BUILDERS = {
    "street_light": {
        "parameters": ["pole_height (ft)", "arm_length (ft)",
                       "pole_base_diameter (in)", "pole_top_diameter (in)"],
        "toggles": ["double_arm", "banner_bracket"],
        "selects": {"mounting": ["flange", "burial", "embedded"]},
        "material_slots": ["pole", "base", "luminaire", "lens"],
    },
    "accessible_table": {
        "parameters": ["surface_height (in)", "knee_clearance_height (in)",
                       "toe_clearance_depth (in)", "table_width (in)", "table_depth (in)"],
        "toggles": [],
        "material_slots": ["top", "frame"],
    },
}


#: Maximum model calls per generation (first attempt + corrective retries).
MAX_ATTEMPTS = 3


class SpecGenerationError(RuntimeError):
    """LLM produced output that could not be turned into a valid spec.

    ``kind`` labels the failure family (truncated / not_json / schema /
    build / buildability / dead_controls / toggle_orphan / scale / provider
    / unknown) and ``hint`` carries the targeted correction instruction the
    retry prompt hands back to the model — the difference between "error,
    try again" and telling it exactly what to change."""

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
- PRIMITIVES ALWAYS WIN: whenever the spec includes a "primitives" array, the app builds THAT and ignores any curated builder entirely — even if asset_type happens to match one. So use a curated builder ONLY when the request needs NOTHING beyond the EXACT controls it lists below (these parameter/toggle/select ids and material slots, no more, no less) — then use it with those exact ids and DO NOT include "primitives":
{json.dumps(BUILTIN_BUILDERS, indent=1)}
  Any styled or extended variant of a curated asset — Victorian styling, a lantern, a solar cap, motion sensors, or any other feature/detail the list above does not name — is NOT a match: keep a semantic asset_type (lowercase snake_case; reuse the SAME standards key when one fits, e.g. "street_light", so US-code dimensional limits still apply) and model ALL of its geometry yourself in "primitives", exactly like any other custom asset. Never invent a parameter/toggle/select id a builder doesn't consume just because it sounds plausible — a curated builder's geometry only reacts to the ids listed above; anything else is silently ignored, so unmodeled requests belong in "primitives" instead.
- For ANY other asset, set a semantic asset_type (lowercase snake_case; reuse a standards key below when one fits) and model the geometry yourself in the "primitives" array. Kinds: box, cylinder, cone, sphere, and the fabrication kinds — lathe (revolve a profile: lantern globes, finials, domes, planters, decorative bases), sweep (a smooth tapered tube along a path: mast arms, handrails, curved members — ONE sweep beats a stack of cylinders), loft (taper between two cross-sections profile_start→profile_end — each section shaped "ellipse" or "rect", so use "ellipse" with equal w/h for a round/circular section; there is NO "circle" shape: cobra heads, flared transitions), tube (hollow pipe with wall thickness — poles/bollards/arms are never solid). Use "cut": true to subtract a primitive (bolt holes, slots) and "array" {{count, step}} for even repetition (pickets, slats).
- Primitive dimensions are METERS. +Z is up. The asset stands on the ground plane z=0 (nothing below z=0). A cylinder/cone's axis is Z; "location" is its center, so a post of depth H sits at z=H/2. rotation is Euler XYZ radians.
- Every numeric field in a primitive may instead be a string expression over parameter/toggle ids, e.g. "pole_height/2" or "seat_height + 0.02". Allowed: numbers, ids, + - * / ( ), min(), max(), abs(). Toggle ids evaluate to 1/0. Parameter values are pre-converted to meters regardless of their display unit.
- EVERY major dimension a designer would tweak must be a parameter (slider) referenced from expressions — never hard-code it. Optional features (backrest, second arm, finial, ...) must be toggles gating primitives via "visible_if".
- TOGGLE COMPLETENESS: a toggle that adds an optional FEATURE must gate EVERY primitive of that feature with the SAME visible_if — including any part carried on top of or supported by a toggled member (the luminaire on a second arm, a finial on an added post, a shade on an added bracket). If an option adds or "doubles" a structural member, it must also add and gate that member's dependent functional parts, so switching the option off never leaves a part floating or a member bare with nothing gated to go with it.
- Give every primitive a component (nested grouping in exports) and a material_slot. Scale the primitive count to the request's complexity — a simple ask stays lean (roughly 10-25 primitives), while a genuinely multi-feature or intricate assembly may reasonably run well past 40; either way, favor simple, readable massing over micro-detail.
- COMPLETENESS: if the request names several parts or features ("a car roof with slanted solar panels"), EVERY named part MUST exist as its own component with its own primitives, parameters, and material slot. Re-read the request before answering and check nothing was dropped.
- SCOPE FIDELITY: build EXACTLY what the request and brief describe — model every part and feature they name in full dimensional detail, but do NOT invent extra parts, features, accessories, or optional add-ons they never asked for. Faithful, detailed execution of the actual request beats volunteered extras; the user refines and extends the asset in later passes, so aim for a lean, precise build of what was asked, not a maximal one. (This never overrides the load-path/support rules below: the structural members that physically hold the requested parts up — rails, brackets, a base plate — are part of building the request, not unrequested additions.)

TILT, SLOPE, CURVE (the model is not limited to upright boxes)
- "rotation" is Euler XYZ radians and accepts expressions. A panel tilted toward +X by an adjustable angle: expose a degree parameter (e.g. {{"id": "panel_tilt", "label": "Panel Tilt", "type": "slider", "min": 0, "max": 60, "step": 1, "value": 30, "unit": "deg"}}) and use "rotation": [0, "panel_tilt * 0.01745", 0] on a thin box (a "deg" value passes through unchanged, so keep the degree→radian factor 0.01745 in the expression).
- UNITS: length units (ft/in/m/cm/mm) arrive in expressions converted to meters. The dimensionless display units "deg" (angles/tilt), "W" (light power/wattage), and "x" (counts & ratios — e.g. number of seats/hoops, a taper ratio) reach expressions UNCHANGED. Give every slider the unit that fits what it controls: "deg" for angles, "W" for light output, "x" for a count or ratio, a length unit for a dimension. Omitting the unit also passes the value through unchanged — never leave a length dimension unit-less, and never put a length unit on an angle/count.
- A tilted part's supports must still reach INTO it: raise/extend the mounting posts so they interpenetrate the rotated panel near its low edge.
- Sloped roofs: one thin rotated box per plane (two for a gable). Curves/arcs (arched arms, hoops, curved backrests): approximate with 5–8 short cylinder/box segments, each positioned and rotated a step further along the path. Domes: sphere; tapers: cone with radius_bottom/radius_top.
- Shade/canopy/awning/roof panels over a use-area (a bench, table, doorway): compute EACH support post's height so its top reaches the panel's actual underside AT THAT POST'S OWN (x, y) — for a flat panel at height h this is just h; for a sloped/gabled panel express the post top as the panel's z at that post's x (e.g. "canopy_h + slope * post_x"), never a constant chosen independently of the panel. The panel's horizontal footprint (its w/h, or its loft/path extent) MUST extend past the edges of what it shades — the seated/used area's (x, y) plan-view must sit entirely under the panel, not merely near a support post.

FORM, PROPORTION & ARCHETYPES (make it read as the real fixture, not a box)
- Map the request to a known archetype and use its characteristic forms: cobra-head street light (tapered swept mast arm + lofted teardrop head); acorn/teardrop post-top lamp (lathe globe on a fluted post with a finial); shoebox area light (thin lofted housing); bishop's-crook lamp (curved swept arm); bollard (tube with a lathe dome cap); planter/urn (lathe vase profile); bench (slats on rails on legs). Prefer lathe/sweep/loft for anything round, curved, or decorative. Accessible fixtures: drinking fountain — pedestal or wall-hung bowl/basin with a spout (bi-level hi-lo units pair a tall and a low bowl side by side); ramp — a sloped deck plane (a rotated box) between level top/bottom landings, with side curbs or handrails; accessible table — a flat top on legs with clear open knee space at one end (no cross-brace or apron blocking a wheelchair approach); trash/recycling receptacle — a tube or slatted cylindrical body with a domed or lathe-turned lid cap.
- Give members REAL structural proportions, not equal sticks: express relationships as ratios in expressions — a pole base diameter ≈ 1.8× its top (taper), a cantilevered arm tapering to ~60% at the tip (sweep radius_end ≈ 0.6× radius), a post-top globe ≈ 1.2–1.6× the post diameter. Slender vertical members read as engineered; chunky uniform ones read as toy.
- WIRE EACH DIMENSION SLIDER TO THE GEOMETRY IT NAMES — the #1 cause of a wrong-sized member is a slider that doesn't actually drive its part. If you expose a diameter/width/thickness parameter (pole_base_diameter, post_width, ...), the member's radius/size expression MUST reference it — a round pole's radius is "pole_base_diameter/2", a tapered pole is a cone/tube with radii "pole_base_diameter/2" and "pole_top_diameter/2" — so moving the slider visibly changes the part and the BUILT cross-section equals the value the slider shows. Never hardcode a thin cross-section next to a diameter slider that says otherwise: a 3.66 m pole whose shaft builds ~6 mm wide while its diameter slider reads 7 in is exactly this bug — the shaft must build at the 0.08-0.20 m the slider declares, never a hairline. Sanity-check every structural member's built radius against its own diameter parameter before answering.
- BUILD IT LIKE A FABRICATOR, NOT A SCULPTOR — real street furniture is made from stock, and the spec can say so. Anything you would order from a mill, model as stock: kind "tube" with a real "wall" (a 2 in square rack member is radius 0.0254 with wall 0.0048 — that is 2.0 SQ x 3/16 wall), and "section": "square" whenever the real part is square stock (bike racks, sign posts, railings, most frames) rather than pipe. Hollow members that are NOT tubes — a tapered pole (cone), a sheet housing (loft/box) — take "shell" (their wall thickness) instead; a structural member left solid is a billet, and the app will weigh it as one. Typical steel walls are 0.003-0.006 m (1/8-1/4 in); plates are 0.006-0.013 m (1/4-1/2 in) thick, never paper-thin. Where a swept member turns a corner (a rack's inverted U, a bishop's crook, a handrail return), give it "bend_radius" — a real bent tube has a called-out radius, usually 2-4x the tube diameter, not an arbitrary curve.
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

DIMENSIONAL CONSISTENCY (every real-world feature keeps its real-world size)
- Each named feature is sized from ITS OWN real-world anchor dimension, regardless of what it's attached to — NEVER shrunk to decorate the primary structure or grown to dominate it. A solar panel bolted to a 3.7 m pergola is still a full-size solar panel, not a seated ornament and not a canopy-sized slab.
- Anchor sizes (use these as the real-world scale for that feature): solar panel ≈ 1.65 x 1.0 x 0.04 m; freestanding post/pole base diameter ≈ 3-8 in / 0.08-0.20 m (ornamental/Victorian lamp posts ≈ 4-6 in fluted shafts) — NEVER a hairline member, no matter how tall the post; luminaire head 0.6-0.8 m; bench seat height ≈ 0.45 m; planter box 0.4-1.2 m; bike hoop ≈ 0.8 x 0.75 m; drinking fountain accessible spout ≈ 0.9 m / 36 in above grade with a basin ~0.4 m wide; trash/recycling receptacle body ~0.5 m across and ~0.9-1.1 m tall; ramp ≥ 0.9 m / 36 in clear width with ≥ 1.5 m / 60 in landings and ≤ 1:12 running slope; accessible table top 0.71-0.86 m / 28-34 in high with ≥ 0.69 m / 27 in knee clearance.
- When a request combines multiple features ("pergola for 4 people with solar panel"), size EACH one independently from its own real-world anchor — the host structure's scale never overrides a feature's own real dimensions.

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
{FEW_SHOT_CUSTOM}

EXAMPLE — arrayed/swept custom primitives (sweep fabrication kind, array {{count, step}} repetition, weld connection):
{FEW_SHOT_ARRAYED}"""


# ---------------------------------------------------------------------------
# Persona identity — the SAME four discipline personas that back the
# Improve button's evaluator cards (blender/builders/perspectives.py ::
# PERSPECTIVES) now front-load the CREATION flow too: each one asks a
# clarifying question, then all four synthesize the design brief together.
# perspectives.py is an independently-evolving sibling module (like
# evaluate_perspectives already treats it below) — imported lazily, with a
# hardcoded fallback so a change over there can never break generation here.
# ---------------------------------------------------------------------------

#: canonical persona order — clarify questions, panel takes, and the
#: envelope's "panel"/"persona" entries always land in this order.
PERSONA_ORDER = ("architecture", "mechanical", "civil", "design")

#: hardcoded fallback if blender.builders.perspectives ever fails to import
#: or drops an id (kept in sync with PERSPECTIVES there by convention, not
#: by import, so this module never hard-depends on that one).
_PERSONA_FALLBACK = {
    "architecture": {"id": "architecture", "label": "Architecture", "icon": "\U0001f3db️"},
    "mechanical": {"id": "mechanical", "label": "Mechanical engineering", "icon": "\U0001f529"},
    "civil": {"id": "civil", "label": "Civil / structural engineering", "icon": "\U0001f3d7️"},
    "design": {"id": "design", "label": "Industrial design", "icon": "\U0001f3a8"},
}


def _personas() -> tuple:
    """The four persona identities (id/label/icon) in ``PERSONA_ORDER``, read
    from :mod:`blender.builders.perspectives` — imported lazily so this
    module never hard-depends on that independently-evolving sibling — with
    the hardcoded fallback above if the import fails or is missing an id."""
    try:
        from blender.builders.perspectives import PERSPECTIVES

        by_id = {p["id"]: p for p in PERSPECTIVES if isinstance(p, dict) and p.get("id")}
        if all(pid in by_id for pid in PERSONA_ORDER):
            return tuple(
                {"id": pid, "label": by_id[pid].get("label", pid.title()),
                 "icon": by_id[pid].get("icon", "")}
                for pid in PERSONA_ORDER
            )
    except Exception:
        pass
    return tuple(_PERSONA_FALLBACK[pid] for pid in PERSONA_ORDER)


# ---------------------------------------------------------------------------
# Prompt refiner — turns a vague request into a precise design brief before
# the spec generator sees it ("a lamp" → asset type, style, dimensions with
# units, materials, options, connections).
#
# ``enhance_prompt`` is the original single-voice pass, kept working for
# backward compatibility; ``generate_spec``/``stream_generate_spec`` no
# longer call it — they use the four-persona DESIGN PANEL pass below, which
# synthesizes the same kind of brief but through all four personas at once
# (see "Design panel" section).
# ---------------------------------------------------------------------------

ENHANCE_SYSTEM = (
    "You are the design-brief writer for a parametric 3D asset generator for "
    "street furniture, lighting, signage, and props. Rewrite the user's request "
    "into one precise, buildable brief for EXACTLY what they asked for — "
    "nothing more. Name the asset type and a style consistent with the "
    "request. Build only what was asked: cover every part and feature the user "
    "named, and add NO new part, feature, accessory, or optional add-on they "
    "did not ask for (they will refine and extend it in later passes, so keep "
    "it lean and faithful, not maximal). Put the detail into DIMENSIONS, and be "
    "thorough there: give the overall size WITH units AND a real, buildable "
    "dimension for EACH named part and feature — height, width, depth, "
    "diameter, wall thickness, count, spacing, and tilt/slope angle in degrees "
    "for slanted or curved elements — choosing sensible values within US code "
    "limits where they apply (AASHTO/MUTCD/ADA/IBC). Then give per-part "
    "materials and finishes and how the parts connect and mount to the ground "
    "— name the fabrication connection per joint (anchor base at grade, band "
    "clamp on the pole, slip-fit tenon, weld, carriage bolts into timber, "
    "through-bolts). Keep EVERY explicit detail the user gave; the ONLY things "
    "you add are the dimensions, materials, and connections needed to build "
    "precisely that. If the request names several parts ('a car roof with "
    "slanted solar panels'), the brief MUST spell out every one of them at its "
    "own real size. Plain prose, at most 200 words, no JSON, no lists, no "
    "preamble."
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


# ---------------------------------------------------------------------------
# Design panel — the four personas write the brief TOGETHER.
#
# Same idea as ``enhance_prompt`` (vague request in, precise brief out) but
# ONE AI call now plays all four Improve-flow personas at once: each one
# names what they specifically bring, and the reply synthesizes their takes
# into a single buildable brief. ``generate_spec``/``stream_generate_spec``
# use this instead of ``enhance_prompt`` — the brief AND the four takes ride
# into the spec-generation prompt so the panel's judgment actually steers
# the design, not just an unread aside. No extra AI call versus today: this
# is still exactly one pass.
# ---------------------------------------------------------------------------

def _panel_system() -> str:
    personas = _personas()
    roster = "\n".join(f'- {p["label"]} ({p["id"]})' for p in personas)
    ids = ", ".join(f'"{p["id"]}"' for p in personas)
    return (
        "You are the four-person design panel for a parametric 3D asset "
        "generator (street furniture, lighting, signage, props) — the same "
        "four professionals who later review the finished asset:\n"
        f"{roster}\n\n"
        "Read the user's request (any answered clarifying questions are "
        "folded into it already — honor every one) and, THINKING AS ALL "
        "FOUR TOGETHER:\n\n"
        "1. Write ONE precise, buildable design brief that SYNTHESIZES all "
        "four perspectives into a single coherent plan for EXACTLY what the "
        "user asked for — nothing more. Name the asset type and a style "
        "consistent with the request.\n"
        "   SCOPE — build only what was asked: cover every part and feature "
        "the user named, and add NO new part, feature, accessory, or "
        "optional add-on they did not ask for. Do not volunteer 'while "
        "we're at it' extras — the user will refine and extend the asset in "
        "later passes, so a lean, faithful build of the actual request is "
        "the goal, not a maximal one.\n"
        "   DIMENSIONS — this is where the detail goes, and where you should "
        "be thorough: give the overall size WITH units AND a real, buildable "
        "dimension for EACH named part and feature — height, width, depth, "
        "diameter, wall thickness, count, spacing, and tilt/slope angle in "
        "degrees for slanted or curved elements — choosing sensible values "
        "within US code limits where they apply (AASHTO/MUTCD/ADA/IBC).\n"
        "   Then give per-part materials and finishes, and how the parts "
        "connect and mount to the ground — name the fabrication connection "
        "per joint (anchor base at grade, band clamp on the pole, slip-fit "
        "tenon, weld, carriage bolts into timber, through-bolts). Keep EVERY "
        "explicit detail the user gave; the ONLY things you add are the "
        "dimensions, materials, and connections needed to build precisely "
        "that. If the request names several parts ('a car roof with slanted "
        "solar panels'), the brief MUST spell out every one of them at its "
        "own real size. Plain prose, at most 200 words, no JSON, no lists, "
        "no preamble.\n\n"
        "2. Give each panelist their OWN take: 1-2 sentences on what THEY "
        "specifically bring to this design and how the brief addresses "
        "it — architecture on who uses it, where it lives, and site "
        "context; mechanical on assembly, serviceability, and moving "
        "parts; civil on ground conditions, loads, and code/permit "
        "context; design on style, materials, mood, and which element the "
        "user already asked for should read as the focal point (never a new "
        "feature).\n\n"
        "Answer ONLY with this JSON object — no prose, no markdown fences:\n"
        '{"brief": "<the synthesized brief, plain prose>", "panel": '
        '[{"id": "<persona id>", "take": "<1-2 sentences>"}, ...]}\n'
        f"\"panel\" must have exactly {len(personas)} entries, one per "
        f"persona id above ({ids}), in that order."
    )


def _sanitize_panel(panel_raw) -> list | None:
    """The four ``{id, take}`` entries the model wrote, reordered into
    ``PERSONA_ORDER`` and stamped with each persona's label/icon — or
    ``None`` if any persona is missing/duplicated/empty. Deliberately
    strict (unlike the clarify tolerance): a partial panel is not worth
    surfacing as "the panel", so a bad reply just degrades to no panel at
    all, same as a provider failure."""
    if not isinstance(panel_raw, list):
        return None
    takes: dict = {}
    for entry in panel_raw:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("id")
        take = str(entry.get("take") or "").strip()
        if pid in PERSONA_ORDER and take and pid not in takes:
            takes[pid] = take[:400]
    if len(takes) != len(PERSONA_ORDER):
        return None
    return [
        {"id": p["id"], "label": p["label"], "icon": p["icon"], "take": takes[p["id"]]}
        for p in _personas()
    ]


def _finalize_panel(raw: str, fallback_prompt: str) -> tuple:
    """Parse the panel pass's reply into ``(brief, panel)``. Mirrors
    ``enhance_prompt``'s failure tolerance and extends it: if the reply
    isn't the expected JSON object (or has no usable "brief"), the raw text
    becomes a plain brief instead — exactly what ``enhance_prompt`` would
    have shipped — and "panel" is simply ``None``. A malformed/partial panel
    degrades the same way without touching the brief."""
    stripped = _strip_fences(raw)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return (raw.strip() or fallback_prompt), None
    if not isinstance(data, dict):
        return (raw.strip() or fallback_prompt), None
    brief = str(data.get("brief") or "").strip()
    if not brief:
        return (raw.strip() or fallback_prompt), None
    return brief, _sanitize_panel(data.get("panel"))


def _design_panel(prompt: str, model: str | None = None) -> tuple:
    """One AI pass that plays all four Improve-flow personas against the
    request before generation: ``(brief, panel)`` where ``panel`` is the 4
    ordered ``{id, label, icon, take}`` entries, or ``None`` when the pass
    failed or degraded. Never retried — like ``enhance_prompt``, this is an
    enhancement, not a requirement, so a provider error just proceeds from
    the raw prompt."""
    try:
        raw = complete(_panel_system(), _enhance_user(prompt),
                       temperature=0.5, max_tokens=700, model=model)
    except LLMError:
        return prompt, None
    return _finalize_panel(raw, prompt)


def _panel_request(brief: str, panel: list | None) -> str:
    """The brief plus each panelist's own take, as ONE request string for
    the spec generator — the panel's judgment only steers the design if the
    prompt actually carries it forward. Falls back to the brief alone when
    the panel pass failed or degraded."""
    if not panel:
        return brief
    lines = [brief, "\nThe design panel's own takes — honor each one:"]
    lines += [f"- {p['label']}: {p['take']}" for p in panel]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Clarifying questions — a four-persona design-panel intake.
#
# "A bench" hides who sits on it, where it lives, and what it should look
# like. Before generating, the UI asks the backend for EXACTLY 4 clarifying
# questions — ONE from each of the same four personas that review assets
# under the Improve button (architecture, mechanical, civil, design) — each
# with EXACTLY 3 concrete AI-written answers; the user picks from a dropdown
# or types their own (the blank space), and the answered pairs ride into
# generation via ``clarifications`` on /generate-spec[-stream] — folded into
# the request BEFORE the design-panel brief pass so every downstream stage
# honors them. Answering is always optional: the UI can skip straight to
# generation, and a failed clarify call must never block generating.
# ---------------------------------------------------------------------------

#: the fixed shape of a clarify round: 4 questions (one per persona) x 3
#: offered answers
CLARIFY_QUESTIONS = 4
CLARIFY_OPTIONS = 3
#: fewer than this many usable questions is a genuine failure worth
#: retrying; at or above it, finalize ACCEPTS what it has rather than
#: burning a retry over the model missing (or double-tagging) one persona —
#: the same tolerance philosophy as the original 3-for-3 requirement, just
#: no longer an all-or-nothing count now that persona tagging can wobble.
CLARIFY_MIN_QUESTIONS = 3
#: answered pairs folded into one generation (matches the questions asked,
#: with headroom for a UI that lets the user add a custom detail or two)
MAX_CLARIFICATIONS = 6

#: the question each persona would most want answered before their part of
#: the design starts.
_CLARIFY_FOCUS = {
    "architecture": "who will use it and where it lives — the site context",
    "mechanical": "how it goes together, what moves or adjusts, and how it gets serviced",
    "civil": "the ground conditions and loads it must stand up to, and any code/permit context",
    "design": "the style, materials, mood, and the one feature that should stand out",
}


def _clarify_system() -> str:
    personas = _personas()
    roster = "\n".join(
        f'{i}. {p["label"]} ({p["id"]}) — asks about '
        f'{_CLARIFY_FOCUS.get(p["id"], "their part of the design")}'
        for i, p in enumerate(personas, start=1)
    )
    ids = ", ".join(f'"{p["id"]}"' for p in personas)
    return f"""You help a parametric 3D asset generator (street furniture, lighting, signage, props) discover the REAL request behind a short one, before anything is generated — by putting together a small panel of the four professionals who later review the finished asset, each asking the ONE question whose answer would most change their part of the design:
{roster}

Given the user's request, write EXACTLY {CLARIFY_QUESTIONS} clarifying questions, ONE per panelist above IN THAT ORDER, tagged with that panelist's persona id, each with EXACTLY {CLARIFY_OPTIONS} concrete, mutually different example answers the user can pick from a dropdown (they may also type their own answer instead).

Ask the question THAT panelist would actually ask about THIS request — never ask about something the request already states, and never ask about units, code modes, or file formats. Keep each question under 90 characters and each answer under 60 — answers are design choices ("Classic cast iron with wood slats"), not sentences.

Answer ONLY with this JSON object — no prose, no fences:
{{"questions": [{{"question": "...", "options": ["...", "...", "..."], "persona": "<id>"}}, {{"question": "...", "options": ["...", "...", "..."], "persona": "<id>"}}, {{"question": "...", "options": ["...", "...", "..."], "persona": "<id>"}}, {{"question": "...", "options": ["...", "...", "..."], "persona": "<id>"}}]}}
"persona" must be exactly one of {ids}, matching the panelist order above."""


def _clarify_user(prompt: str) -> str:
    return f"CLARIFY REQUEST.\nRequest: {prompt}"


def _clarify_finalize(raw: str) -> dict:
    """Parse + sanitize the AI's questions into a persona-ordered shape
    (plus stable ids). Anything malformed raises a CLASSIFIED error so the
    retry loop can hand the model a targeted correction.

    Persona tags are sanitized independently of the question/options text:
    an unknown, missing, or duplicate "persona" id just drops the tag (the
    question itself is kept), then tagged questions are reordered into
    ``PERSONA_ORDER`` with untagged ones kept, in their original relative
    order, at the end. The count only needs to clear
    ``CLARIFY_MIN_QUESTIONS`` (3) to survive — a model that returns 3 or 5
    usable questions is accepted/trimmed rather than forcing a retry."""
    stripped = _strip_fences(raw)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        if _looks_truncated(stripped):
            raise SpecGenerationError(
                f"Questions were cut off before the JSON finished: {exc}",
                kind="truncated",
                hint="Return the COMPLETE questions JSON — shorter questions "
                     "and answers.",
            ) from None
        raise SpecGenerationError(
            f"Questions were not valid JSON: {exc}",
            kind="not_json",
            hint='Return ONLY the {"questions": [...]} JSON object — no '
                 "prose, no fences.",
        ) from None

    questions_raw = data.get("questions") if isinstance(data, dict) else data
    if not isinstance(questions_raw, list):
        raise SpecGenerationError(
            'Clarify JSON did not contain a "questions" array',
            kind="schema",
            hint='Answer with exactly {"questions": [...]}.',
        )

    persona_by_id = {p["id"]: p for p in _personas()}
    seen_personas: set = set()
    cleaned = []
    for q in questions_raw:
        if not isinstance(q, dict):
            continue
        text = str(q.get("question") or "").strip()
        options = []
        for opt in q.get("options") or []:
            clean = str(opt).strip()[:80]
            if clean and clean.lower() not in {o.lower() for o in options}:
                options.append(clean)
        if not text or len(options) < CLARIFY_OPTIONS:
            continue
        pid = q.get("persona")
        pid = pid if isinstance(pid, str) else None
        if pid not in persona_by_id or pid in seen_personas:
            pid = None  # unknown, missing, or a repeat -> untagged, question kept
        else:
            seen_personas.add(pid)
        cleaned.append({
            "question": text[:160],
            "options": options[:CLARIFY_OPTIONS],
            "persona_id": pid,
        })

    if len(cleaned) < CLARIFY_MIN_QUESTIONS:
        raise SpecGenerationError(
            f"Expected at least {CLARIFY_MIN_QUESTIONS} questions with "
            f"{CLARIFY_OPTIONS} distinct options each, got {len(cleaned)} usable",
            kind="schema",
            hint=f"Return {CLARIFY_QUESTIONS} questions, one per panelist "
                 f"({', '.join(PERSONA_ORDER)}), each with exactly "
                 f"{CLARIFY_OPTIONS} DISTINCT non-empty options and a "
                 f'matching "persona" id.',
        )

    tagged = {c["persona_id"]: c for c in cleaned if c["persona_id"]}
    untagged = [c for c in cleaned if not c["persona_id"]]
    ordered = [tagged[pid] for pid in PERSONA_ORDER if pid in tagged] + untagged
    ordered = ordered[:CLARIFY_QUESTIONS]

    questions = []
    for i, c in enumerate(ordered, start=1):
        entry = {"id": f"q{i}", "question": c["question"], "options": c["options"]}
        if c["persona_id"]:
            p = persona_by_id[c["persona_id"]]
            entry["persona"] = {"id": p["id"], "label": p["label"], "icon": p["icon"]}
        questions.append(entry)
    return {"questions": questions}


def clarify_request(prompt: str, model: str | None = None) -> dict:
    """4 persona-tagged clarifying questions (architecture, mechanical,
    civil, design) x 3 offered answers for a raw request, ready for the
    UI's dropdowns. Rides the classified retry engine like every other
    structured AI answer."""
    return _complete_with_retries(
        _clarify_system(), _clarify_user(prompt),
        lambda raw, lenient: _clarify_finalize(raw),
        model=model, temperature=0.6, max_tokens=700,
    )


def _clarified_prompt(prompt: str, clarifications: list | None) -> str:
    """The user's request plus their clarifying answers as ONE request
    string — the input to the design-brief pass, so the brief (and through
    it the spec) honors what the user actually meant. Unanswered rounds
    pass the prompt through untouched."""
    pairs = []
    for c in clarifications or []:
        if not isinstance(c, dict):
            continue
        q = str(c.get("question") or "").strip()
        a = str(c.get("answer") or "").strip()
        if q and a:
            pairs.append((q[:200], a[:200]))
        if len(pairs) == MAX_CLARIFICATIONS:
            break
    if not pairs:
        return prompt
    lines = [prompt,
             "\nThe user answered clarifying questions about this request — "
             "honor EVERY answer explicitly:"]
    lines += [f"- {q} → {a}" for q, a in pairs]
    return "\n".join(lines)


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


def _scale_findings(prims: list, spec: dict) -> list:
    """Relative/real-world scale check (``check_scale_sanity`` in the
    sibling ``blender.builders.connectivity`` module — built independently
    in a separate worktree). Imported LAZILY so this module never
    hard-depends on that change landing first: any import or attribute
    problem degrades to no scale findings at all (zero behavior change),
    never a crash. The checker itself is contracted to never raise for
    valid prims, but the call is still guarded defensively for the same
    deploy-order safety."""
    try:
        from blender.builders.connectivity import check_scale_sanity
    except (ImportError, AttributeError):
        return []
    try:
        return check_scale_sanity(prims, spec) or []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Edit-discipline enforcement — every AI edit (refine / focus / wizard step /
# improve) reports EXACTLY what changed, the materials wizard step is
# mechanically barred from touching geometry, and results with parts rammed
# through existing geometry get a targeted retry instead of shipping
# silently. See ``_run_edit``/``_edit_finalize`` below for where this wires
# into the retry engine.
# ---------------------------------------------------------------------------

def _spec_components(spec: dict) -> set:
    """Component names declared in ``spec``: each primitive's own
    "component" (default "body") when ``primitives`` is present, else the
    top-level ``components`` list (curated-builder specs, e.g. street_light,
    carry no primitives of their own)."""
    prims = spec.get("primitives") or []
    if prims:
        return {p.get("component", "body") for p in prims if isinstance(p, dict)}
    return {c for c in (spec.get("components") or []) if isinstance(c, str)}


def _prims_by_component(spec: dict) -> dict:
    by_component: dict = {}
    for p in spec.get("primitives") or []:
        if isinstance(p, dict):
            by_component.setdefault(p.get("component", "body"), []).append(p)
    return by_component


def _param_values(spec: dict) -> dict:
    """{id: value} for every parameter AND toggle — both can drive geometry
    (expressions, visible_if), so both count toward "params_changed"."""
    values: dict = {}
    for key in ("parameters", "toggles"):
        for entry in spec.get(key) or []:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                values[entry["id"]] = entry.get("value")
    return values


def spec_changes(before: dict, after: dict) -> dict:
    """Deterministic before→after diff of two AssetSpecs — the frozen
    envelope the frontend's edit tools consume: components ``added``/
    ``removed``, ``changed`` components (their primitive entries differ,
    compared as normalized JSON), ``params_changed`` (parameter/toggle ids
    whose value differs), and a one-line human ``summary`` composed from
    whichever of those are non-empty ("No structural changes" when none
    are). Pure and total: malformed input just yields empty sets rather
    than raising, though callers still wrap the call (``_safe_spec_changes``)
    since this is user-facing and must never break an edit response."""
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}

    before_components = _spec_components(before)
    after_components = _spec_components(after)
    added = sorted(after_components - before_components)
    removed = sorted(before_components - after_components)

    changed = []
    if before.get("primitives") or after.get("primitives"):
        before_by_component = _prims_by_component(before)
        after_by_component = _prims_by_component(after)
        for component in sorted(before_components & after_components):
            b = json.dumps(before_by_component.get(component, []), sort_keys=True)
            a = json.dumps(after_by_component.get(component, []), sort_keys=True)
            if b != a:
                changed.append(component)

    before_params = _param_values(before)
    after_params = _param_values(after)
    params_changed = sorted(
        pid for pid in (set(before_params) | set(after_params))
        if before_params.get(pid) != after_params.get(pid)
    )

    phrases = []
    if changed:
        phrases.append(f"changed {', '.join(changed)}")
    if added:
        phrases.append(f"added {', '.join(added)}")
    if removed:
        phrases.append(f"removed {', '.join(removed)}")
    if params_changed:
        phrases.append(f"adjusted {', '.join(params_changed)}")
    summary = "; ".join(phrases) if phrases else "No structural changes"
    if phrases:
        summary = summary[0].upper() + summary[1:]

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "params_changed": params_changed,
        "summary": summary,
    }


def _safe_spec_changes(before: dict, after: dict) -> dict | None:
    """``spec_changes``, but a diff failure never breaks the edit response —
    the "changes" key is simply omitted."""
    try:
        return spec_changes(before, after)
    except Exception:
        return None


def _embedded_part_findings(prims: list, spec: dict) -> list:
    """Interpenetration check for parts rammed through existing geometry
    (``check_embedded_parts`` in the sibling ``blender.builders.connectivity``
    module — built independently in a separate worktree; not yet present in
    this tree). Imported LAZILY, same deploy-order safety as
    ``_scale_findings``: an import/attribute problem, or an unexpected
    exception from the checker itself, degrades to no findings at all
    rather than a crash — the checker is contracted to never raise, but the
    call is still guarded defensively."""
    try:
        from blender.builders.connectivity import check_embedded_parts
    except (ImportError, AttributeError):
        return []
    try:
        return check_embedded_parts(prims, spec) or []
    except Exception:
        return []


def _enforce_materials_scope(changes: dict | None, lenient: bool) -> list:
    """The materials wizard step's directive claims geometry is untouched —
    mechanically enforce that instead of trusting the prompt. ``changes`` is
    the diff of the step's result against its input spec; any added/
    removed/changed component or changed parameter/toggle value means
    geometry moved, which is illegal for this step. Raises a classified
    "scope" error naming exactly what was touched, unless ``lenient`` (the
    final attempt), in which case it degrades to a warning finding instead
    of failing the whole step. A diff that could not be computed
    (``changes is None``) skips the gate rather than blocking on an
    unrelated failure."""
    if not changes:
        return []
    touched = changes["added"] + changes["removed"] + changes["changed"]
    if not touched and not changes["params_changed"]:
        return []
    parts = []
    if touched:
        parts.append(f"component(s) {', '.join(touched)}")
    if changes["params_changed"]:
        parts.append(f"parameter/toggle value(s) {', '.join(changes['params_changed'])}")
    what = " and ".join(parts)
    if lenient:
        return [{
            "severity": "warning",
            "kind": "scope",
            "message": f"Materials step changed {what}, but geometry is "
                       f"read-only for this step. {changes['summary']}.",
        }]
    raise SpecGenerationError(
        f"Materials step illegally changed {what}.",
        kind="scope",
        hint=(
            "The materials step's contract: geometry is READ-ONLY — you "
            "may change ONLY the top-level \"materials\" object (preset, "
            f"color, metalness, roughness, uv_scale, emission, weathering, "
            f"finish per slot). You illegally changed {what}. Return the "
            "spec again with every component, primitive, parameter, and "
            "toggle reverted to its EXACT previous value, keeping ONLY the "
            "materials edits."
        ),
    )


def _enforce_integration(findings: list, lenient: bool) -> list:
    """Embedded-part findings from ``_embedded_part_findings``. Raises a
    classified "integration" error naming the offending parts unless
    ``lenient`` (the final attempt), in which case the findings are
    surfaced as violations instead of failing the whole edit."""
    if not findings:
        return []
    if lenient:
        return findings
    messages = [f.get("message", "") for f in findings if isinstance(f, dict)]
    raise SpecGenerationError(
        "Added/changed parts are embedded in existing geometry: "
        + " ".join(messages[:4]),
        kind="integration",
        hint=(
            " ".join(messages[:4])
            + " Seat the part on a surface with a 10-20 mm embed and "
            "declare a connection in \"connections\" instead of "
            "intersecting it."
        ),
    )


#: Natural loft-profile shape words the model reaches for, mapped to the two
#: shapes the schema enum AND ``shapes.ring_points`` actually understand
#: (ellipse ⇒ round section; rect ⇒ softly-rounded rectangle). A circle/round/
#: oval section is an ellipse with equal w/h; a square/rectangle section is a
#: rect. The identity entries (ellipse→ellipse, rect→rect) are deliberate: they
#: also normalize case ("Ellipse", "RECT") to the case-sensitive enum value.
_PROFILE_SHAPE_SYNONYMS = {
    "circle": "ellipse", "circular": "ellipse", "round": "ellipse",
    "oval": "ellipse", "elliptical": "ellipse", "ellipse": "ellipse",
    "rectangle": "rect", "rectangular": "rect", "square": "rect",
    "box": "rect", "rect": "rect",
}


def _normalize_profile_shapes(spec: dict) -> None:
    """In-place: map a loft ``profile_start``/``profile_end`` ``shape`` written
    as a natural synonym (``circle``, ``round``, ``square``, ``Ellipse`` …) to
    the canonical ``ellipse``/``rect`` the schema enum and ``ring_points``
    understand. Runs BEFORE ``jsonschema.validate`` so a reasonable word the
    model reaches for neither fails an otherwise-valid loft spec (the observed
    "'circle' is not one of ['rect','ellipse']" failure) NOR — since
    ``ring_points`` treats any non-``ellipse`` shape as a rounded rect — silently
    builds a square-ish section where a round one was meant. Case/whitespace-
    insensitive; an unrecognized shape is left untouched for the schema to
    reject exactly as before. Total: never raises on malformed input."""
    prims = spec.get("primitives")
    if not isinstance(prims, list):
        return
    for prim in prims:
        params = prim.get("params") if isinstance(prim, dict) else None
        if not isinstance(params, dict):
            continue
        for key in ("profile_start", "profile_end"):
            profile = params.get(key)
            if not isinstance(profile, dict):
                continue
            shape = profile.get("shape")
            if isinstance(shape, str):
                canonical = _PROFILE_SHAPE_SYNONYMS.get(shape.strip().lower())
                if canonical is not None:
                    profile["shape"] = canonical


def _postprocess(raw: str, code_mode: str, lenient_buildability: bool = False) -> dict:
    """Parse, schema-validate (T7.4), geometry-check, code-clamp (T2.3),
    buildability-check (contact graph: floating parts, below-grade geometry,
    dead declarations), a dead-CONTROL check (a parameter/toggle the spec
    exposes but that provably drives no geometry — an invented slider or
    toggle the UI would show as live but that is actually inert), a
    toggle-completeness check (``check_toggle_dependencies`` — a toggle
    that, switched off from its own default, orphans a still-visible part
    because it wasn't co-gated with the member it rests on: "double the
    arm" adds the arm but leaves the light on it floating when the option
    is switched off), and a relative-scale sanity check (mis-sized features
    like a seated solar panel on a pergola). Every failure raises a
    CLASSIFIED :class:`SpecGenerationError` whose hint tells the model
    exactly what to fix. Floating parts, dead controls, toggle orphans, and
    scale outliers raise — the deterministic findings feed the retry —
    unless ``lenient_buildability`` (the final attempt), in which case
    they're accepted and surfaced as violations instead, so a stubborn
    generation never bricks."""
    out, _prims = _postprocess_core(raw, code_mode, lenient_buildability)
    return out


def _postprocess_core(raw: str, code_mode: str,
                      lenient_buildability: bool = False) -> tuple:
    """Same as :func:`_postprocess` but also returns the computed
    primitives, so edit-discipline gates that need them (the integration
    gate's ``check_embedded_parts``) can reuse this build instead of calling
    ``compute_primitives`` a second time."""
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
    # The requested code_mode always wins — a model that writes its own
    # "code_mode" into the spec (matching schema, so it would otherwise pass
    # validation) must not be able to silently disable the strict clamping
    # the caller asked for, or vice versa. Force this BEFORE jsonschema.
    # validate so the schema still sees (and accepts) a legal value.
    spec["code_mode"] = code_mode
    # Map natural loft-profile shape synonyms ("circle", "square", "Ellipse",
    # ...) to the canonical rect/ellipse the schema and ring_points understand,
    # BEFORE validation — a reasonable word the model reaches for shouldn't fail
    # an otherwise-good spec (nor silently build the wrong cross-section).
    _normalize_profile_shapes(spec)

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

    dead_findings = check_dead_controls(result.spec)
    if dead_findings and not lenient_buildability:
        dead_messages = [f["message"] for f in dead_findings[:4]]
        raise SpecGenerationError(
            "Dead control check failed: " + " ".join(dead_messages),
            kind="dead_controls",
            hint=(
                " ".join(dead_messages)
                + " Remove the control, or reference it from a primitive "
                "expression — a curated builder only reacts to its listed "
                "ids; extra features belong in primitives."
            ),
        )

    orphan_findings = check_toggle_dependencies(result.spec)
    if orphan_findings and not lenient_buildability:
        orphan_messages = [f["message"] for f in orphan_findings[:4]]
        raise SpecGenerationError(
            "Toggle completeness check failed: " + " ".join(orphan_messages),
            kind="toggle_orphan",
            hint=(
                " ".join(orphan_messages)
                + " Co-gate each named part with the SAME visible_if as the "
                "member it rests on (or give it its own support to the "
                "ground) so switching the option adds/removes the whole "
                "feature together."
            ),
        )

    scale_findings = _scale_findings(prims, result.spec)
    if scale_findings and not lenient_buildability:
        messages = [f.get("message", "") for f in scale_findings]
        raise SpecGenerationError(
            "Scale check failed: " + " ".join(messages[:4]),
            kind="scale",
            hint=(
                f"Fix component scale: {' '.join(messages[:4])}. Keep "
                "real-world dimensions for every feature. If a member reads "
                "as a hairline, drive its radius/width from its own diameter "
                'parameter (a pole\'s radius is "pole_base_diameter/2") so '
                "the shaft builds at the real 0.08-0.20 m it declares, never "
                "a sliver."
            ),
        )

    out = result.to_dict()
    if findings:
        out["violations"] = out["violations"] + findings
        if errors:
            out["ok"] = False
    if dead_findings:
        out["violations"] = out["violations"] + dead_findings
        out["ok"] = False
    if orphan_findings:
        out["violations"] = out["violations"] + orphan_findings
        out["ok"] = False
    if scale_findings:
        out["violations"] = out["violations"] + scale_findings
    return out, prims


#: LLMError texts that are worth retrying (rate limits, provider hiccups,
#: network trouble) — as opposed to configuration errors (missing API key,
#: unknown provider), which no retry can fix.
_TRANSIENT_LLM_RE = re.compile(
    r"returned (?:429|5\d\d)|timed?[ -]?out|timeout|request failed|"
    r"network|connection|temporarily", re.IGNORECASE,
)


def _transient_llm_error(exc: LLMError) -> bool:
    return bool(_TRANSIENT_LLM_RE.search(str(exc)))


#: Exponential backoff (seconds) between transient provider retries, and its
#: cap. A "small" provider rate limit needs a real pause to clear — retrying a
#: 429 the instant it arrives just spends the next attempt on the same limit,
#: so all attempts burn in milliseconds and the generation dies. Kept modest so
#: the added latency stays within a serverless request budget (at most two
#: waits across MAX_ATTEMPTS: ~2s then ~4s).
_RETRY_BACKOFF_BASE_S = 2.0
_RETRY_BACKOFF_CAP_S = 8.0


def _transient_backoff_seconds(attempt: int) -> float:
    """Seconds to wait before the retry that follows the ``attempt``-th
    transient provider failure: exponential (base ``_RETRY_BACKOFF_BASE_S``,
    doubling each attempt), capped at ``_RETRY_BACKOFF_CAP_S``. attempt 1 → 2s,
    2 → 4s, 3 → 8s, and flat at the cap thereafter."""
    return min(_RETRY_BACKOFF_BASE_S * (2 ** max(0, attempt - 1)), _RETRY_BACKOFF_CAP_S)


def _backoff_before_retry(attempt: int) -> None:
    """Pause before retrying a transient provider failure so a small rate limit
    actually clears. Skipped entirely for the keyless ``mock`` provider — it
    never rate-limits, and the whole test suite runs on it, so tests pay no
    real wall-clock sleep."""
    if active_provider() == "mock":
        return
    time.sleep(_transient_backoff_seconds(attempt))


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
        # Strip <think>...</think> reasoning before echoing: the streaming
        # pipeline's `raw` is the joined stream INCLUDING think blocks, and
        # echoing those instead of the actual answer can push the real JSON
        # past the 6000-char cap entirely, sabotaging the repair.
        answer = strip_reasoning(raw)
        lines.append(f"\nYour previous answer (repair it in place):\n{answer[:6000]}")
    lines.append("\nReturn the corrected COMPLETE AssetSpec JSON only.")
    return "\n".join(lines)


def _escalate_truncated_budget(current_tokens: int, history: list) -> int:
    """A reply classified "truncated" means the model ran out of room, not
    that it made a content mistake — retrying with the SAME max_tokens would
    truncate identically every time. So when the most recent failure was
    "truncated", double the output-token budget for the next attempt
    (bounded by TRUNCATION_MAX_TOKENS_CEILING, and never below whatever
    budget — caller-supplied or already-escalated — got us here). Any other
    failure kind (not_json/schema/build/...) is a content bug, not a budget
    bug, and leaves the budget untouched."""
    if history and history[-1].kind == "truncated":
        return min(current_tokens * 2, TRUNCATION_MAX_TOKENS_CEILING)
    return current_tokens


def _complete_with_retries(system: str, user: str, finalize, *,
                           model: str | None = None, **complete_kwargs) -> dict:
    """Non-streaming attempt loop: call the model, ``finalize(raw, lenient)``
    the reply, and on a classified failure re-prompt with a targeted
    correction — up to MAX_ATTEMPTS calls. The last attempt finalizes
    leniently (buildability warnings instead of failure). A "truncated"
    failure also escalates ``max_tokens`` for the next attempt (see
    ``_escalate_truncated_budget``) so a spec that genuinely needs more
    output room gets a real second chance instead of truncating identically
    on every attempt."""
    history: list = []
    raw = ""
    current_tokens = complete_kwargs.pop("max_tokens", DEFAULT_MAX_TOKENS)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        message = user if attempt == 1 else _correction_user(user, attempt, history, raw)
        current_tokens = _escalate_truncated_budget(current_tokens, history)
        try:
            raw = complete(system, message, model=model, max_tokens=current_tokens,
                           **complete_kwargs)
        except LLMError as exc:
            if attempt == MAX_ATTEMPTS or not _transient_llm_error(exc):
                raise
            history.append(SpecGenerationError(str(exc), kind="provider"))
            _backoff_before_retry(attempt)
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


def _edit_finalize(code_mode: str, input_spec: dict, *, wizard_step_key: str | None = None,
                   integration_gate: bool = False):
    """Build a ``finalize(raw, lenient=False)`` callable for an edit against
    an EXISTING spec (refine / focus / wizard step / improve — never
    ``generate_spec``, which has no "existing" geometry to diff against).
    Shared by the non-streaming retry loop (``_run_edit``) and the streaming
    pipeline so both enforce identically:

    - always attaches ``out["changes"]`` — the diff of the result against
      ``input_spec`` (``_safe_spec_changes``; omitted if the diff fails);
    - when ``wizard_step_key == "materials"``, mechanically gates that step
      on geometry staying untouched (``_enforce_materials_scope``);
    - when ``integration_gate``, checks the result's primitives for parts
      rammed through existing geometry (``_enforce_integration`` over
      ``_embedded_part_findings``), reusing the primitives ``_postprocess_core``
      already computed rather than rebuilding them."""
    def finalize(raw: str, lenient: bool = False) -> dict:
        out, prims = _postprocess_core(raw, code_mode, lenient_buildability=lenient)
        changes = _safe_spec_changes(input_spec, out.get("spec"))
        if changes is not None:
            out["changes"] = changes
        if wizard_step_key == "materials":
            out["violations"] = out["violations"] + _enforce_materials_scope(changes, lenient)
        if integration_gate:
            findings = _embedded_part_findings(prims, out.get("spec") or {})
            out["violations"] = out["violations"] + _enforce_integration(findings, lenient)
        return out
    return finalize


def _run_edit(system: str, user: str, code_mode: str, input_spec: dict, *,
             model: str | None = None, wizard_step_key: str | None = None,
             integration_gate: bool = False) -> dict:
    """``_complete_with_retries`` for an edit against ``input_spec`` — see
    ``_edit_finalize`` for what that adds. Runs inside the classified retry
    loop so a scope/integration violation triggers a targeted correction
    prompt, not a silent ship."""
    return _complete_with_retries(
        system, user,
        _edit_finalize(code_mode, input_spec, wizard_step_key=wizard_step_key,
                       integration_gate=integration_gate),
        model=model,
    )


# ---------------------------------------------------------------------------
# AI pre-delivery QA reviewer — GENERATE flows only (generate_spec /
# stream_generate_spec). Every generated spec is read back by ONE AI
# reviewer call before it ships: the original request, the AssetSpec, a
# bounded geometry digest (the REAL numbers the asset builds to, in mm), the
# deterministic violations, and the standards DB entry for its asset_type.
# The reviewer answers approve/reject; a sanitized "reject" on a non-lenient
# attempt raises a classified "qa_review" SpecGenerationError so the EXISTING
# classified retry engine (_complete_with_retries/_stream_pipeline) re-prompts
# with the reviewer's fixes as the hint — no extra attempts beyond
# MAX_ATTEMPTS, this still costs one iteration of the same loop. Refine /
# focus / wizard / improve / variations never call this — they ride
# _run_edit/_edit_finalize, untouched by this section.
#
# A QA infrastructure failure (provider error, malformed/unparseable JSON, a
# missing verdict) must NEVER surface as a retry-consuming error — it
# degrades to a "skip", and the generation ships exactly as it would have
# without QA, just with qa.verdict == "skipped".
# ---------------------------------------------------------------------------

from blender.builders.hardware import _aabb  # noqa: E402

#: primitive entries kept in a QA geometry digest — bounds the prompt so a
#: large spec doesn't blow the QA call's token budget.
QA_DIGEST_MAX_PRIMS = 40
#: problems/fixes kept per QA verdict, and the length each string is capped
#: to — same sanitize-and-bound discipline as _sanitize_review_finding.
QA_MAX_ITEMS = 6
QA_MAX_TEXT_LEN = 300
#: the QA call is a quick, cheap judgment pass, not a rewrite.
QA_TEMPERATURE = 0.2
QA_MAX_TOKENS = 800

QA_REVIEW_SYSTEM = """You are the pre-delivery QA inspector for a parametric 3D asset generator (street furniture, lighting, signage, props) — the last check before a generated asset ships to the person who asked for it. Read it the way a fabrication shop reads a customer's order against the finished piece.

You receive:
1. the ORIGINAL REQUEST the user made,
2. the AssetSpec JSON that was generated for it,
3. a geometry digest — per-component envelope dimensions and primitive count (mm), plus a bounded list of individual primitives with their kind and sorted AABB dimensions (mm): the REAL numbers the asset builds to, not just the spec's own labels,
4. the deterministic violations already found for this spec (already measured — judge whether they matter, do not just repeat them),
5. the US-code standards DB entry for this asset_type, or a note that none exists.

REJECT ONLY FOR REAL DEFECTS — never for stylistic taste. Valid reasons to reject:
- a feature or part the request explicitly asked for is missing from the spec/digest,
- a dimension in the digest is physically absurd for what it claims to be, judged from the digest's actual numbers (a "seat" 3 m off the ground, a "bolt" the size of a manhole cover, a pole a few millimeters tall, ...),
- the spec or a listed violation genuinely conflicts with the standards entry or a real code limit (not a cosmetic warning),
- an optional feature is incomplete: a toggled/added structural member (a second arm, an extra post, a bracket) whose dependent functional parts (the light on that arm, a finial on that post, a shade on that bracket) are missing from the spec, or present but not gated together with it by the same visible_if — the spec's primitives already show each part's visible_if, so check that a feature's parts share one.
A plain, simple, or lightly-detailed design that satisfies the request and has none of the above is SOUND — approve it.

OUTPUT — return ONLY this JSON object, no prose, no markdown fences:
{"verdict": "approve" | "reject",
 "problems": ["<specific, measurable problem — name the part and the number>", ...],
 "fixes": ["<concrete directive with target numbers the model can act on>", ...]}
"problems" and "fixes" may be empty arrays when the verdict is "approve"."""


def _geometry_digest(prims: list, spec: dict, violations: list) -> dict:
    """Deterministic, bounded geometry summary for the QA reviewer: per
    component the envelope dimensions (mm) and primitive count, then up to
    ``QA_DIGEST_MAX_PRIMS`` individual non-cut, non-hardware primitives
    (name, kind, sorted AABB dims in mm) — the REAL numbers the asset builds
    to, so the reviewer judges physical plausibility from measurements
    instead of the spec's own labels. Also carries the spec's violations and
    its standards DB entry (or a "no standards entry" note) so code
    conflicts are judged from the same facts the rest of the app already
    computed. Pure and total: never raises for a valid primitive list."""
    real = [p for p in prims if not p.cut and p.component != "hardware"]

    by_component: dict = {}
    for p in real:
        by_component.setdefault(p.component, []).append(p)

    components = {}
    for comp, comp_prims in sorted(by_component.items()):
        boxes = [_aabb(p) for p in comp_prims]
        lo = [min(c[k] - h[k] for c, h in boxes) for k in range(3)]
        hi = [max(c[k] + h[k] for c, h in boxes) for k in range(3)]
        components[comp] = {
            "envelope_mm": [round((hi[k] - lo[k]) * 1000) for k in range(3)],
            "prim_count": len(comp_prims),
        }

    primitives = []
    for p in real[:QA_DIGEST_MAX_PRIMS]:
        _, half = _aabb(p)
        dims_mm = sorted((round(2 * h * 1000) for h in half), reverse=True)
        primitives.append({"name": f"{p.component}/{p.name}", "kind": p.kind,
                           "dims_mm": dims_mm})

    asset_type = spec.get("asset_type") if isinstance(spec, dict) else None
    standards_entry = load_standards().get(asset_type) if isinstance(asset_type, str) else None

    return {
        "components": components,
        "primitives": primitives,
        "violations": violations or [],
        "standards": standards_entry if standards_entry is not None
                     else "no standards entry for this asset_type",
    }


def _qa_user(request: str, spec: dict, digest: dict) -> str:
    return (
        f"QA REVIEW.\n"
        f"ORIGINAL REQUEST:\n{request}\n\n"
        f"AssetSpec:\n{json.dumps(spec, separators=(',', ':'))}\n\n"
        f"Geometry digest (the REAL numbers the asset builds to):\n"
        f"{json.dumps(digest, separators=(',', ':'))}"
    )


def _sanitize_qa_texts(raw) -> list:
    """Up to ``QA_MAX_ITEMS`` non-empty strings, each capped to
    ``QA_MAX_TEXT_LEN`` chars — the same cap-counts/cap-lengths discipline
    ``_sanitize_review_finding`` applies elsewhere."""
    out = []
    if isinstance(raw, list):
        for item in raw:
            text = str(item or "").strip()
            if text:
                out.append(text[:QA_MAX_TEXT_LEN])
            if len(out) >= QA_MAX_ITEMS:
                break
    return out


def _qa_finalize(raw: str) -> dict:
    """Parse + sanitize the QA reviewer's reply into ``{"verdict",
    "problems", "fixes"}``. Raises ``ValueError``/``json.JSONDecodeError``
    on anything unusable — the caller (``_run_qa_review``) turns ANY such
    failure into a "skip" instead of letting it propagate."""
    stripped = _strip_fences(raw)
    data = json.loads(stripped)
    if not isinstance(data, dict):
        raise ValueError("QA reply was not a JSON object")
    verdict = data.get("verdict")
    if verdict not in ("approve", "reject"):
        raise ValueError(f"QA reply had no valid verdict: {verdict!r}")
    return {
        "verdict": verdict,
        "problems": _sanitize_qa_texts(data.get("problems")),
        "fixes": _sanitize_qa_texts(data.get("fixes")),
    }


#: result["qa"]["verdict"] labels, keyed by _qa_finalize's raw verdict (plus
#: the infrastructure-failure "skip" _run_qa_review substitutes for one).
_QA_VERDICT_LABEL = {"approve": "approved", "reject": "rejected", "skip": "skipped"}


def _run_qa_review(request: str, spec: dict, violations: list, prims: list,
                   model: str | None) -> dict:
    """One QA reviewer ``complete()`` call over an already-postprocessed
    generation result: ``{"verdict": "approve"|"reject"|"skip", "problems":
    [...], "fixes": [...]}``. ANY QA infrastructure failure — a provider
    error, unparseable/malformed JSON, a missing/invalid verdict, or
    anything else going wrong while building the digest or parsing the
    reply — degrades to "skip" instead of raising: a QA outage must never
    block or brick generation."""
    try:
        digest = _geometry_digest(prims, spec, violations)
        raw = complete(QA_REVIEW_SYSTEM, _qa_user(request, spec, digest),
                       model=model, temperature=QA_TEMPERATURE, max_tokens=QA_MAX_TOKENS)
        return _qa_finalize(raw)
    except Exception:
        return {"verdict": "skip", "problems": [], "fixes": []}


# ---------------------------------------------------------------------------
# 4-layer generation pipeline (the default generate). Rather than one spec-
# writing call, the asset is built by four specialists in dependency order —
# STRUCTURE (the load-bearing skeleton) → FUNCTION (the working parts it
# exists for) → CONNECTIONS (how it holds together + mounts) → MATERIALS &
# FINISH (the skin). An AssetSpec is a coupled whole (primitives reference
# parameters, connections reference component names, materials reference
# slots), so the layers run SEQUENTIALLY, each building on the accumulated
# spec — coherence by construction. Layer 1 is a generate; layers 2-4 are
# scoped edits on the growing spec (the same _run_edit path refine/focus/
# wizard use). "Combine" is the accumulation; a final QA reviewer judges the
# whole (advisory — it does not re-run the layers). Layers 2-4 degrade
# gracefully: a layer that can't be built after retries is skipped, the
# accumulated spec carries on, and the miss is recorded in the "layers" trace.
# ---------------------------------------------------------------------------

#: the four layers, in the order they build.
LAYER_ORDER = ("structure", "function", "connections", "materials")
#: human labels for the streaming stage banners and the "layers" trace.
_LAYER_LABEL = {
    "structure": "structure",
    "function": "function",
    "connections": "connections",
    "materials": "materials & finish",
}

_LAYER_DIRECTIVES = {
    "structure": (
        "LAYER 1 of 4 — STRUCTURE (the skeleton). Build ONLY the load-bearing "
        "frame of this asset: set the asset_type, expose the major dimensions as "
        "parameters (sliders), and model the primary structural members — the "
        "pole/mast/column, frame, legs, rails/stretchers, and the base or footing "
        "at grade — as primitives at real, buildable dimensions with real "
        "cross-sections (each member's radius/width driven by its diameter "
        "parameter; never a hairline). Do NOT model the functional parts yet (no "
        "luminaire head, seat, sign face, basket, spout), do NOT add joint "
        "hardware, and keep each member to a single sensible default material — "
        "later layers add function, connections, and finish. The result must be a "
        "schema-valid, buildable structural massing everything else will hang on."
    ),
    "function": (
        "LAYER 2 of 4 — FUNCTION (the working parts). The structural skeleton is "
        "already built (below). Now add the parts the asset EXISTS FOR — the "
        "luminaire head + lens, the seat and back, the sign face, the basket, the "
        "spout, the planter vessel — each as its own component with primitives, "
        "positioned where it really sits on the structure; give lenses a "
        "lamp_lens material with realistic emission. Expose what a user would "
        "tune (optional features as visible_if toggles, key dimensions/angles as "
        "sliders), reusing existing ids and adding new ones only for genuinely "
        "new controls. Do NOT re-style existing parts, add joint hardware, or "
        "change the structure's dimensions — connections and materials are later "
        "layers. Return the FULL updated AssetSpec JSON, keeping the structure's "
        "components, parameters, and values stable."
    ),
    "connections": (
        "LAYER 3 of 4 — CONNECTIONS (how it holds together). The structure and "
        "function are built (below). Work joint by joint from the ground up: for "
        "each pair of touching components choose the fabrication type a crew would "
        "use and DECLARE it in the top-level \"connections\" array — anchor_base "
        "(a structural vertical at grade, b:\"ground\"), band_clamp (arm on a "
        "round pole), slip_fit (telescoping post-top), carriage_bolt (wood on "
        "metal), through_bolt (bolted lap), flange_splice (collinear end-to-end), "
        "weld (shop-welded steel), lag_screw, or none (concealed / cast-integral). "
        "Add connecting members (rails, brackets, gussets, collars, base plates) "
        "ONLY where a part would otherwise float or have nothing to fasten to; "
        "joined parts MUST interpenetrate 10-20 mm. Do NOT restyle the asset or "
        "change its materials. Return the FULL updated AssetSpec JSON, keeping "
        "asset_type, geometry, materials, and all existing ids/values stable "
        "except the connecting members a real joint requires."
    ),
    "materials": (
        "LAYER 4 of 4 — MATERIALS & FINISH (the skin). The whole asset is built "
        "and connected (below). Give every material slot the right preset and "
        "surface properties for the part it covers and the asset's style: set the "
        "fitting preset and, where it helps, color, metalness, roughness, "
        "uv_scale, emission (2-6 for lit lenses), and finish (cast for cast-iron "
        "bases/finials, machined for turned fittings, sheet for housings/panels, "
        "rough for galvanized poles and concrete). Add weathering ONLY if the "
        "request implies age or setting. Honor any style/material words in the "
        "original request. GEOMETRY IS READ-ONLY FOR THIS LAYER: do NOT change "
        "geometry, connections, toggles, or parameters — every primitive, "
        "component, parameter, and toggle value must come back byte-identical. "
        "Return the FULL updated AssetSpec JSON, changing only the materials."
    ),
}


def _structure_user(panel_request: str) -> str:
    """Layer 1 is a GENERATE from the brief, scoped to the structural skeleton."""
    return f"Request: {panel_request}\n\n{_LAYER_DIRECTIVES['structure']}"


def _layer_user(layer: str, spec: dict, brief: str) -> str:
    """Layers 2-4 are EDITS on the accumulated spec: the current spec + the
    design brief (overall intent) + this layer's directive."""
    return (
        f"Here is the current AssetSpec, built by the previous layer(s):\n"
        f"{json.dumps(spec, separators=(',', ':'))}\n\n"
        f"Design brief (the overall intent to honor):\n{brief}\n\n"
        f"{_LAYER_DIRECTIVES[layer]}"
    )


def _finalize_layered(spec: dict, request: str, brief: str, panel: list | None,
                      trace: list, code_mode: str, model: str | None,
                      run_qa: bool = True) -> dict:
    """Combine step: re-validate/build the accumulated spec once (leniently —
    each layer already validated as it went), optionally run ONE advisory QA
    review over the whole, and attach brief/panel/qa + the per-layer ``layers``
    trace.

    ``run_qa=False`` skips the review. The STREAMING path passes it: the QA
    verdict is advisory and nothing in the UI reads it, so on that path it was
    a whole extra provider call — more tokens, and ~a call's worth of extra
    wall clock at the very END of an already-long 5-call run, which is exactly
    where a serverless/proxy timeout cuts the connection and costs the user
    the entire generation. The non-streaming :func:`generate_spec` keeps it.
    """
    out, prims = _postprocess_core(json.dumps(spec), code_mode, lenient_buildability=True)
    if run_qa:
        qa = _run_qa_review(request, out.get("spec") or {}, out.get("violations") or [],
                            prims, model)
        out["qa"] = {"verdict": _QA_VERDICT_LABEL[qa["verdict"]],
                     "problems": qa["problems"], "fixes": qa["fixes"]}
    out["brief"] = brief
    if panel:
        out["panel"] = panel
    out["layers"] = trace
    return out


def _generate_layered(request: str, code_mode: str, model: str | None,
                      brief: str, panel: list | None) -> dict:
    """Build the asset with the four-layer pipeline and combine. Layer 1
    (structure) is a generate; layers 2-4 (function/connections/materials) are
    scoped edits on the accumulated spec that degrade gracefully — a layer that
    fails after retries is skipped so the rest of the build still ships."""
    panel_request = _panel_request(brief, panel)
    trace: list = []
    # Layer 1 — STRUCTURE: generate a buildable skeleton (rides the classified
    # retry engine exactly like the old single-shot generate).
    out = _complete_with_retries(
        _system_prompt(code_mode), _structure_user(panel_request),
        lambda raw, lenient=False: _postprocess(raw, code_mode, lenient_buildability=lenient),
        model=model)
    spec = out["spec"]
    trace.append({"layer": "structure", "status": "built"})
    # Layers 2-4 — scoped edits on the growing spec; a stubborn layer is
    # skipped (accumulated spec carries on) rather than bricking the build.
    for layer in ("function", "connections", "materials"):
        try:
            out = _run_edit(
                _system_prompt(code_mode), _layer_user(layer, spec, brief),
                code_mode, spec, model=model,
                wizard_step_key="materials" if layer == "materials" else None,
                integration_gate=True)
            spec = out["spec"]
            trace.append({"layer": layer, "status": "built"})
        except (SpecGenerationError, LLMError) as err:
            trace.append({"layer": layer, "status": "skipped", "error": str(err)[:200]})
    return _finalize_layered(spec, panel_request, brief, panel, trace, code_mode, model)


def generate_spec(prompt: str, code_mode: str = "strict", model: str | None = None,
                  clarifications: list | None = None) -> dict:
    """T2.1: natural-language prompt (+ answered clarifying questions) →
    four-persona design-panel brief → the 4-LAYER generation pipeline
    (STRUCTURE → FUNCTION → CONNECTIONS → MATERIALS, ``_generate_layered``) →
    a final advisory QA review. Each layer specializes in one system and
    builds on the accumulated spec; layers 2-4 degrade gracefully. The brief
    (and the 4 persona takes as "panel") ride along in the result, as does a
    per-layer "layers" trace and the QA verdict."""
    request = _clarified_prompt(prompt, clarifications)
    brief, panel = _design_panel(request, model=model)
    return _generate_layered(request, code_mode, model, brief, panel)


def _refine_user(spec: dict, message: str) -> str:
    return (
        f"Here is the current AssetSpec:\n{json.dumps(spec, separators=(',', ':'))}\n\n"
        f"Apply this change and return the FULL updated AssetSpec JSON "
        f"(keep everything else identical, including ids):\n{message}\n\n"
        "EDIT DISCIPLINE — this is a surgical edit, not a redesign:\n"
        "- NEVER modify components, primitives, parameters, toggles, or "
        "materials unrelated to the request above — every unrelated change "
        "gets caught and costs a retry.\n"
        "- When ADDING something, seat it on a REAL surface of the named "
        "host with a 10-20 mm embed (not floating, not merely touching, and "
        "never driven through the host's interior) AND declare its "
        "connection in the top-level \"connections\" array.\n"
        "- If the requested change needs geometry the current curated "
        "builder (if any) does not model, PRIMITIVES ALWAYS WIN: return the "
        "spec with a FULL \"primitives\" array modeling the WHOLE asset (it "
        "takes precedence over the curated builder), keeping the existing "
        "parameter/toggle ids and values where they still carry over."
    )


def refine_spec(spec: dict, message: str, code_mode: str = "strict",
                model: str | None = None) -> dict:
    """T2.5: current spec + chat message → modified, re-validated spec."""
    return _run_edit(_system_prompt(code_mode), _refine_user(spec, message), code_mode, spec,
                     model=model, integration_gate=True)


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
    return _run_edit(_system_prompt(code_mode), _focus_user(spec, area), code_mode, spec,
                     model=model)


# ---------------------------------------------------------------------------
# Variations — N independent perturbations of a base spec along fixed,
# distinct design axes (proportion, mass, ornament, stance, ...), each run
# through the SAME single-spec edit pipeline as refine/focus/improve
# (``_run_edit``, integration_gate on) rather than one call asked to return
# several specs at once. That keeps the classified retry engine and the
# strict-clamp/test-build gate (``_postprocess_core``) applied per variant
# verbatim, with the lowest possible blast radius: a stubborn variant is
# simply dropped (see ``variations_spec``), never allowed to weaken the gate
# or sink the whole request.
# ---------------------------------------------------------------------------

#: fixed, ordered perturbation directives — (label, directive) — cycled when
#: ``count`` exceeds the tuple length. At least 6 entries so the API's
#: ``count`` upper bound (6) is always covered by distinct directives, no
#: repeats needed at the max.
_VARIATION_DIRECTIVES: tuple[tuple[str, str], ...] = (
    ("Slender & Tall",
     "PROPORTION/SLENDERNESS — make this variant read as taller and more "
     "slender: increase the height-to-width proportions of the principal "
     "vertical members, taper them more aggressively, and thin out "
     "secondary members. Do not shrink real-world feature sizes (a seat, a "
     "panel, a luminaire head keep their own functional dimensions) — only "
     "the overall stance and member proportions change."),
    ("Heavy & Robust",
     "MASS/ROBUSTNESS — make this variant read as heavier and more robust: "
     "thicken the principal structural members (larger diameters/cross-"
     "sections), enlarge base plates and footings, and reduce tapering so "
     "the asset reads as over-built rather than delicate."),
    ("Ornamented",
     "ORNAMENT/DETAIL — add ornamental and decorative detail appropriate to "
     "the asset's style: mouldings, finials, fluting, brackets, or trim "
     "pieces as new primitives on existing components. Keep the overall "
     "massing and footprint the same; this variant is about surface and "
     "silhouette richness, not a different structure."),
    ("Minimalist",
     "ORNAMENT/DETAIL (opposite direction) — strip this variant down to "
     "clean, minimal lines: remove or simplify decorative primitives "
     "(mouldings, finials, trim), square off tapered forms where doing so "
     "keeps the design buildable, and favor flat/plain surfaces. Keep every "
     "structural and functional part intact — only decoration is reduced."),
    ("Wide Stance",
     "STANCE/FOOTPRINT — widen this variant's footprint: splay or space "
     "the ground-contact members (legs, base plate, anchor bolt circle) "
     "further apart for a broader, more planted stance, extending any "
     "connecting rails/stretchers to match. Overall height and the size of "
     "functional features stay the same."),
    ("Compact Footprint",
     "STANCE/FOOTPRINT (opposite direction) — narrow this variant's "
     "footprint: bring the ground-contact members (legs, base plate, "
     "anchor bolt circle) closer together for a tighter, more vertical "
     "stance, shortening any connecting rails/stretchers to match. Overall "
     "height and the size of functional features stay the same."),
)


def _variation_user(spec: dict, directive: str) -> str:
    return (
        f"Here is the current AssetSpec:\n{json.dumps(spec, separators=(',', ':'))}\n\n"
        f"Produce ONE DISTINCT DESIGN VARIANT of this asset along this "
        f"specific axis:\n{directive}\n\n"
        "Return the FULL updated AssetSpec JSON (keep the asset_type, name, "
        "and units unchanged; keep existing ids where the part is "
        "unchanged).\n\n"
        "EDIT DISCIPLINE — this is a targeted variation, not a redesign:\n"
        "- Change ONLY what the variation axis above calls for; every "
        "component, primitive, parameter, toggle, and material unrelated to "
        "that axis stays as close to the original as sensible.\n"
        "- When adding or resizing parts, seat them on a REAL surface of "
        "the named host with a 10-20 mm embed (not floating, not merely "
        "touching, and never driven through the host's interior) AND "
        "declare the connection in the top-level \"connections\" array."
    )


def variations_spec(base_spec: dict, count: int = 4, code_mode: str = "strict",
                    model: str | None = None) -> dict:
    """N INDEPENDENT AI edit passes over ``base_spec``, each steered by one
    of the fixed ``_VARIATION_DIRECTIVES`` perturbation axes, cycling
    through the tuple if ``count`` exceeds it. Each variant is produced by
    its OWN call to ``_run_edit`` (the same classified retry engine and
    strict-clamp/test-build gate as refine/focus/improve, integration gate
    on) — not a single call asked to return several specs — so a variant
    that exhausts ``MAX_ATTEMPTS`` is simply DROPPED rather than failing the
    whole request. Returns ``{"variants": [...]}`` where each entry is the
    ordinary edit result (``spec``, ``violations``, ``changes`` when the
    diff succeeds) plus a stable human ``label``. Raises
    :class:`SpecGenerationError` only when every variant failed."""
    variants = []
    last_error: SpecGenerationError | None = None
    for i in range(count):
        label, directive = _VARIATION_DIRECTIVES[i % len(_VARIATION_DIRECTIVES)]
        try:
            result = _run_edit(
                _system_prompt(code_mode), _variation_user(base_spec, directive),
                code_mode, base_spec, model=model, integration_gate=True,
            )
        except SpecGenerationError as exc:
            last_error = exc
            continue
        result["label"] = label
        variants.append(result)
    if not variants:
        raise last_error or SpecGenerationError(
            "No variant survived generation", kind="unknown",
        )
    return {"variants": variants}


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
        "style/material words in the original request. GEOMETRY IS READ-ONLY FOR "
        "THIS STEP: do NOT change geometry, connections, toggles, or parameters "
        "— every primitive, component, parameter, and toggle value must come "
        "back byte-identical to what you were given, no exceptions. This pass "
        "is only about how the asset is finished. Return the FULL updated "
        "AssetSpec JSON, keeping every id, value, and part identical outside "
        "the materials."
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
    return _run_edit(_system_prompt(code_mode), _wizard_user(spec, step, message),
                     code_mode, spec, model=model, wizard_step_key=step,
                     integration_gate=True)


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
# AI persona evaluators — four discipline reviews that back the "Improve"
# button's evaluator cards (architecture, mechanical engineering, civil/
# structural engineering, industrial design). Each persona reads the
# deterministic Python findings blender/builders/perspectives.py already
# computed for its discipline (that module is a separate, independently-
# evolving piece — imported lazily so this module never hard-depends on it)
# and adds ONE round of professional judgment on top, in the same
# {severity, kind, message} findings shape "Check connections" already uses
# elsewhere in the app. A single persona's AI call failing NEVER fails the
# whole evaluation: that entry just keeps its deterministic findings and
# carries a short "error" message instead of an AI opinion — the other three
# personas, and the improve pass itself, are unaffected.
# ---------------------------------------------------------------------------

#: severities a persona's AI findings may use — anything else is clamped to
#: "warning" (matches the vocabulary the rest of the app already uses).
PERSPECTIVE_SEVERITIES = ("error", "warning", "info")
#: at most this many AI findings kept per persona (on top of its checks findings)
MAX_PERSPECTIVE_FINDINGS = 5


def _persona_prompt(role: str, focus: str) -> str:
    return (
        f"{role} You are given the AssetSpec JSON for a parametric "
        f"site-furnishing asset and the deterministic findings your "
        f"discipline's checks already produced for it — those are measured "
        f"facts, do not just repeat them. Add YOUR professional judgment on "
        f"top, focused on {focus}.\n\n"
        "Return ONLY this JSON object — no prose, no markdown fences, "
        "nothing outside it:\n"
        '{"summary": "<one sentence>", "findings": '
        '[{"severity": "error"|"warning"|"info", "kind": "<snake_case>", '
        '"message": "<specific, actionable>"}]}\n'
        f"At most {MAX_PERSPECTIVE_FINDINGS} findings, most important first."
    )


#: one system prompt per blender/builders/perspectives.py::PERSPECTIVES id.
PERSONA_SYSTEM = {
    "architecture": _persona_prompt(
        "You are a licensed architect reviewing a parametric site-furnishing asset.",
        "massing and proportion, human factors (scale, clearances, reach/"
        "sightlines), and how well the piece fits its intended site context",
    ),
    "mechanical": _persona_prompt(
        "You are a mechanical engineer reviewing a parametric site-furnishing asset.",
        "fasteners and joint types, torque/hardware adequacy, and "
        "serviceability (could a crew actually assemble, inspect, and "
        "maintain every connection)",
    ),
    "civil": _persona_prompt(
        "You are a licensed civil/structural engineer reviewing a parametric "
        "site-furnishing asset.",
        "load paths to grade, foundation/anchorage adequacy, and US code "
        "compliance (AASHTO/IBC/ADA/MUTCD as applicable)",
    ),
    "design": _persona_prompt(
        "You are an industrial designer reviewing a parametric site-furnishing asset.",
        "materials and finishes, color, and detail coherence across the whole piece",
    ),
}


def _perspective_user(spec: dict, entry: dict) -> str:
    checks_brief = [
        {k: f.get(k) for k in ("severity", "kind", "message")}
        for f in (entry.get("findings") or [])
    ]
    return (
        f"PERSPECTIVE REVIEW — {entry.get('label') or entry.get('id')}.\n"
        f"AssetSpec:\n{json.dumps(spec, separators=(',', ':'))}\n\n"
        f"Metrics from your discipline's checks:\n"
        f"{json.dumps(entry.get('metrics') or {}, separators=(',', ':'))}\n\n"
        f"Deterministic findings from your discipline's checks:\n"
        f"{json.dumps(checks_brief, separators=(',', ':'))}"
    )


def _sanitize_perspective_finding(f):
    """One AI finding, source-tagged and bounded — or None to drop it.
    Severity outside the 3 allowed values is clamped to "warning"; kind and
    message are coerced to (capped) strings."""
    if not isinstance(f, dict):
        return None
    message = str(f.get("message") or "").strip()
    if not message:
        return None
    severity = f.get("severity")
    if severity not in PERSPECTIVE_SEVERITIES:
        severity = "warning"
    kind = str(f.get("kind") or "general").strip()[:60] or "general"
    return {"severity": severity, "kind": kind, "message": message[:400], "source": "ai"}


def _persona_finalize(raw: str) -> tuple:
    """Parse + sanitize one persona's reply into ``(summary, ai_findings)``.
    Raises ``ValueError`` with a short human-readable reason on anything
    malformed; the caller turns that into the entry's "error" field instead
    of letting it propagate — a persona's tolerant-parse failure must never
    take down the whole evaluation."""
    stripped = _strip_fences(raw)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"reply was not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError("reply was not a JSON object")
    findings_raw = data.get("findings")
    if not isinstance(findings_raw, list):
        raise ValueError('reply did not contain a "findings" array')
    summary = str(data.get("summary") or "").strip()[:300]
    findings = []
    for f in findings_raw:
        clean = _sanitize_perspective_finding(f)
        if clean:
            findings.append(clean)
        if len(findings) >= MAX_PERSPECTIVE_FINDINGS:
            break
    return summary, findings


def evaluate_perspectives(spec: dict, model: str | None = None) -> list:
    """Four named evaluators (architecture, mechanical, civil/structural,
    industrial design): each combines that discipline's deterministic Python
    findings (``blender.builders.perspectives.evaluate_all`` — imported
    lazily, an independently-evolving sibling module) with ONE AI persona
    review (a single ``complete()`` call per persona; never retried — a
    persona opinion is advisory, not load-bearing). Returns the 4 envelope
    entries in ``PERSPECTIVES`` order, each
    ``{id, label, icon, summary, findings, error}`` — "findings" is that
    persona's checks findings (source "checks") followed by its AI findings
    (source "ai"). A persona's AI call failing never fails the whole
    evaluation: that entry keeps its checks findings, an empty AI
    contribution, summary "", and a short "error" message instead."""
    from blender.builders.perspectives import evaluate_all

    out = []
    for entry in evaluate_all(spec):
        pid = entry.get("id")
        checks_findings = [dict(f, source="checks") for f in (entry.get("findings") or [])]
        system = PERSONA_SYSTEM.get(pid)
        summary, ai_findings, error = "", [], None
        if system is None:
            error = f"no persona prompt registered for {pid!r}"
        else:
            try:
                raw = complete(system, _perspective_user(spec, entry), model=model,
                               temperature=0.3, max_tokens=800)
                summary, ai_findings = _persona_finalize(raw)
            except (LLMError, ValueError) as exc:
                error = str(exc)[:300]
        out.append({
            "id": pid,
            "label": entry.get("label"),
            "icon": entry.get("icon"),
            "summary": summary,
            "findings": checks_findings + ai_findings,
            "error": error,
        })
    return out


# ---------------------------------------------------------------------------
# Cross-review — the four consultants read each other's cards and react.
#
# ONE extra AI call (never retried — advisory, not load-bearing, exactly
# like each persona's own evaluation above) plays the SAME four personas
# convening as a panel: each reacts to the other three's findings (concur /
# dispute / refine), then the panel jointly agrees on priorities. This never
# fails the improve pass — any parse/provider failure just degrades to no
# peer_notes/consensus, identical to today's envelope. Only called from the
# improve wiring below, and only when there is something to react to.
# ---------------------------------------------------------------------------

#: the 3 reaction stances a peer note may take — anything else clamps to
#: "refine" (matches PERSPECTIVE_SEVERITIES's clamp-to-default pattern).
PEER_NOTE_STANCES = ("concur", "dispute", "refine")
#: at most this many peer reactions kept per perspective entry.
MAX_PEER_NOTES = 3
#: at most this many joint priorities in the consensus.
MAX_CONSENSUS_PRIORITIES = 3


def _cross_review_system() -> str:
    personas = _personas()
    roster = "\n".join(f'- {p["label"]} ({p["id"]})' for p in personas)
    ids = ", ".join(f'"{p["id"]}"' for p in personas)
    return (
        "You are the same four-person discipline panel that already wrote "
        "individual evaluations of this asset, now convening TOGETHER to "
        "peer-review each other's written cards:\n"
        f"{roster}\n\n"
        "You are given each colleague's summary and findings. For EACH "
        "colleague's card, write at most a couple of pointed reactions "
        "FROM THE OTHER THREE PANELISTS (never a reaction from a card's "
        "own persona to itself) — concur when a finding is confirmed from "
        "your discipline's angle, dispute when you believe it is wrong or "
        "overblown (say why), refine when it is right but mis-scoped (say "
        "how it should be scoped instead). Then, as a panel, jointly agree "
        "on the 1-3 things that matter most across all four cards.\n\n"
        "Return ONLY this JSON object — no prose, no markdown fences, "
        "nothing outside it:\n"
        '{"peer_notes": {"<persona id>": [{"from": "<a DIFFERENT persona '
        'id>", "stance": "concur"|"dispute"|"refine", "note": "<one '
        'pointed sentence>"}, ...]}, "consensus": {"summary": "<one or two '
        'sentences>", "priorities": ["<agreed priority>", ...]}}\n'
        f"Persona ids are {ids}. A card's peer_notes list must never "
        "contain a note whose \"from\" equals that same card's own persona "
        f"id. At most {MAX_PEER_NOTES} notes per card. \"priorities\" must "
        f"have 1 to {MAX_CONSENSUS_PRIORITIES} items."
    )


def _cross_review_user(spec: dict, perspectives: list) -> str:
    brief = _perspectives_brief(perspectives)
    return (
        f"AssetSpec:\n{json.dumps(spec, separators=(',', ':'))}\n\n"
        f"The four evaluations to cross-review:\n"
        f"{json.dumps(brief, separators=(',', ':'))}"
    )


def _sanitize_peer_note(raw, target_id: str, valid_ids: set) -> dict | None:
    """One reaction from another persona to ``target_id``'s card, or
    ``None`` to drop it. Dropped when "from" is missing/unknown or equals
    the target's own id (a card may never react to itself); stance outside
    the 3 allowed values clamps to "refine"; note is coerced to a
    (length-capped) string."""
    if not isinstance(raw, dict):
        return None
    frm = raw.get("from")
    if frm not in valid_ids or frm == target_id:
        return None
    note = str(raw.get("note") or "").strip()
    if not note:
        return None
    stance = raw.get("stance")
    if stance not in PEER_NOTE_STANCES:
        stance = "refine"
    return {"from": frm, "stance": stance, "note": note[:300]}


def _sanitize_consensus(raw) -> dict | None:
    """The panel's joint summary + priorities, or ``None`` when missing/
    malformed (an absent/bad consensus never drops valid peer_notes — the
    two sanitize independently)."""
    if not isinstance(raw, dict):
        return None
    summary = str(raw.get("summary") or "").strip()[:400]
    priorities_raw = raw.get("priorities")
    priorities: list = []
    if isinstance(priorities_raw, list):
        for p in priorities_raw:
            text = str(p or "").strip()[:200]
            if text:
                priorities.append(text)
            if len(priorities) >= MAX_CONSENSUS_PRIORITIES:
                break
    if not summary or not priorities:
        return None
    return {"summary": summary, "priorities": priorities}


def _cross_review_finalize(raw: str, valid_ids: set) -> tuple:
    """Parse + sanitize the cross-review reply into ``(peer_notes_by_id,
    consensus)``. Raises ``ValueError``/``json.JSONDecodeError`` on
    unusable JSON — the caller turns any of that (plus a provider error)
    into total degradation, same as a persona evaluation failing."""
    stripped = _strip_fences(raw)
    data = json.loads(stripped)
    if not isinstance(data, dict):
        raise ValueError("reply was not a JSON object")
    peer_notes_raw = data.get("peer_notes")
    peer_notes: dict = {}
    if isinstance(peer_notes_raw, dict):
        for target_id, notes in peer_notes_raw.items():
            if target_id not in valid_ids or not isinstance(notes, list):
                continue
            clean = []
            for n in notes:
                note = _sanitize_peer_note(n, target_id, valid_ids)
                if note:
                    clean.append(note)
                if len(clean) >= MAX_PEER_NOTES:
                    break
            if clean:
                peer_notes[target_id] = clean
    consensus = _sanitize_consensus(data.get("consensus"))
    return peer_notes, consensus


def cross_review_perspectives(spec: dict, perspectives: list, model: str | None = None) -> tuple:
    """The four personas peer-review each other's evaluation cards: ONE
    ``complete()`` call (never retried — advisory, not load-bearing, same
    one-shot pattern as each persona's own evaluation) that plays the whole
    panel at once. Returns ``(peer_notes_by_id, consensus)`` where
    ``peer_notes_by_id`` maps a perspective id to its (possibly empty) list
    of sanitized reactions from the OTHER personas, and ``consensus`` is the
    panel's joint ``{summary, priorities}`` or ``None``. ANY failure — a
    provider error, invalid JSON, an unusable reply, or anything else going
    wrong while parsing it — degrades to ``({}, None)`` instead of raising,
    so a cross-review problem never takes down the improve pass it advises."""
    valid_ids = {p["id"] for p in _personas()}
    try:
        raw = complete(_cross_review_system(), _cross_review_user(spec, perspectives),
                       model=model, temperature=0.3, max_tokens=900)
        peer_notes, consensus = _cross_review_finalize(raw, valid_ids)
    except Exception:
        return {}, None
    return peer_notes, consensus


# ---------------------------------------------------------------------------
# AI spec improvement — "here is my creation and everything the Python
# checks flagged, return a better one."
#
# Composes the SAME deterministic checks the rest of the app already runs
# (the connection auditor, the buildability contact-graph check, and the
# US-code validator) into one flat findings list, hands the spec AND those
# findings (plus the four persona evaluations above) to the AI with an
# instruction to fix every one of them and modestly improve realism, then
# routes the reply through the ordinary edit pipeline
# (_run_edit/_stream_pipeline) so the result is schema/build/buildability
# checked exactly like every other AI-produced spec. The findings and the four
# evaluator cards that were fed in ride along in the result so the UI can
# show what was fixed.
# ---------------------------------------------------------------------------

def _gather_findings(spec: dict) -> list:
    """Deterministic Python-side checks — connection audit
    (``audit_connections``), buildability contact-graph
    (``check_buildability``), relative-scale sanity (``check_scale_sanity``,
    lazily imported the same way as in ``_postprocess``), and US-code
    validation (``validate_spec``) — normalized into a flat list of
    ``{severity, kind, message, ...}`` dicts ready to embed in the AI
    prompt. A spec broken badly enough that it can't even
    ``compute_primitives`` still yields exactly ONE ``build_failure``
    finding instead of crashing, so the AI has something concrete to
    repair."""
    from blender.builders.audit import audit_connections

    findings: list = []
    try:
        det = audit_connections(spec)
        for f in det.get("findings", []):
            findings.append({
                "severity": f.get("severity", "warning"),
                "kind": f.get("kind", "audit"),
                "message": f.get("detail") or f.get("title") or "",
                "component": f.get("component"),
            })

        prims = compute_primitives(spec)
        for f in check_buildability(prims, spec):
            findings.append({
                "severity": f.get("severity", "error"),
                "kind": f.get("limit_type", "buildability"),
                "message": f.get("message", ""),
            })

        for f in _scale_findings(prims, spec):
            findings.append({
                "severity": f.get("severity", "warning"),
                "kind": f.get("kind", "scale_outlier"),
                "message": f.get("message", ""),
                "component": f.get("component"),
            })

        result = validate_spec(spec)
        for v in result.violations:
            d = v.to_dict()
            findings.append({
                "severity": "warning",
                "kind": f"code_{d['limit_type']}",
                "message": d["message"],
                "parameter_id": d["parameter_id"],
            })
    except Exception as exc:
        # a spec this broken can't be checked further — hand the AI the one
        # fact it needs (what broke) instead of a partial/misleading list
        return [{"severity": "error", "kind": "build_failure", "message": str(exc)}]

    return findings


def _perspectives_brief(perspectives: list) -> list:
    """The four evaluator cards' summaries + findings (kind/severity/message/
    source), compact enough to embed in a prompt. Shared by the improve
    prompt and the cross-review pass so both read the exact same shape."""
    return [
        {
            "id": p.get("id"),
            "label": p.get("label"),
            "summary": p.get("summary"),
            "findings": [
                {k: f.get(k) for k in ("severity", "kind", "message", "source")}
                for f in (p.get("findings") or [])
            ],
        }
        for p in perspectives
    ]


def _perspectives_block(perspectives: list | None) -> str:
    """Compact JSON of the four evaluator cards plus the directive to
    address their error/warning findings, or "" when there are none (a
    persona-evaluation failure upstream still yields entries, so this is
    normally always populated)."""
    if not perspectives:
        return ""
    brief = _perspectives_brief(perspectives)
    return (
        "\n\nFour discipline experts each reviewed this asset — architecture, "
        "mechanical engineering, civil/structural engineering, and industrial "
        "design — combining deterministic checks with a persona's "
        "professional judgment:\n"
        f"{json.dumps(brief, separators=(',', ':'))}\n\n"
        "Address every error- and warning-severity finding from these four "
        "evaluations where geometrically sensible, in addition to the "
        "deterministic checks above."
    )


def _consensus_block(consensus: dict | None) -> str:
    """The panel's agreed priorities, folded into the improve prompt so the
    cross-review pass actually steers the fix — or "" when there is no
    (valid) consensus."""
    if not consensus or not consensus.get("priorities"):
        return ""
    priorities = "; ".join(consensus["priorities"])
    return f"\n\nThe panel's agreed priorities — address these first: {priorities}"


def _improve_user(spec: dict, findings: list, perspectives: list | None = None,
                  consensus: dict | None = None) -> str:
    return (
        f"Here is the current AssetSpec:\n{json.dumps(spec, separators=(',', ':'))}\n\n"
        f"The app's deterministic checks found these issues in it — FIX EVERY "
        f"ONE:\n{json.dumps(findings, separators=(',', ':'))}"
        f"{_perspectives_block(perspectives)}"
        f"{_consensus_block(consensus)}\n\n"
        "Fix every finding listed above. Keep existing parameter, toggle, and "
        "material ids and their current values stable except where a finding "
        "requires a change. You may add missing \"connections\" declarations, "
        "adjust primitive dimensions/positions, and add small missing "
        "structural members (rails, gussets, collars, base plates) so every "
        "part has a real load path to the ground. Do NOT rename existing "
        "components. Beyond fixing the findings, make modest realism "
        "improvements (better proportions, small missing details) without "
        "changing the asset's overall design intent or asset_type. Return "
        "the FULL updated AssetSpec JSON."
    )


def _cross_review_and_attach(spec: dict, perspectives: list, model: str | None) -> dict | None:
    """Runs the cross-review pass and stamps its peer_notes onto the
    matching perspective entries IN PLACE, returning the consensus (or
    ``None``). Skips the call entirely when every perspective evaluation
    came back empty — nothing for the panel to react to, a valid
    degradation rather than a wasted call. ANY cross-review failure — a
    provider error, malformed JSON, anything — degrades to no peer_notes/
    no consensus (``cross_review_perspectives`` never raises), so this can
    never fail the improve pass it advises."""
    if not any((p.get("findings") or []) for p in perspectives):
        return None
    peer_notes, consensus = cross_review_perspectives(spec, perspectives, model=model)
    for entry in perspectives:
        notes = peer_notes.get(entry.get("id"))
        if notes:
            entry["peer_notes"] = notes
    return consensus


def improve_spec(spec: dict, code_mode: str = "strict", model: str | None = None) -> dict:
    """Current spec + the deterministic Python-side findings + the four
    persona evaluations, cross-reviewed by the same four personas as one
    panel → an improved, re-validated spec that fixes every finding and
    weighs what the panel agreed matters most. Rides the same classified
    retry engine as every other AI-produced spec (``_run_edit``); the findings,
    the four evaluator cards (now optionally carrying peer_notes from the
    cross-review), and the panel consensus (when the cross-review
    succeeded) ride along in the result as "findings", "perspectives", and
    "consensus"."""
    findings = _gather_findings(spec)
    perspectives = evaluate_perspectives(spec, model=model)
    consensus = _cross_review_and_attach(spec, perspectives, model)
    result = _run_edit(_system_prompt(code_mode),
                       _improve_user(spec, findings, perspectives, consensus),
                       code_mode, spec, model=model, integration_gate=True)
    result["findings"] = findings
    result["perspectives"] = perspectives
    if consensus:
        result["consensus"] = consensus
    return result


def stream_improve_spec(spec: dict, code_mode: str = "strict", model: str | None = None):
    """Streaming twin of :func:`improve_spec` — the result envelope carries
    "findings", "perspectives" (with peer_notes when cross-review
    succeeded), and "consensus" (when present) the same way."""
    findings = _gather_findings(spec)
    perspectives = evaluate_perspectives(spec, model=model)
    consensus = _cross_review_and_attach(spec, perspectives, model)
    edit_finalize = _edit_finalize(code_mode, spec, integration_gate=True)

    def finalize(raw: str, lenient: bool = False) -> dict:
        result = edit_finalize(raw, lenient)
        result["findings"] = findings
        result["perspectives"] = perspectives
        if consensus:
            result["consensus"] = consensus
        return result

    return _stream_pipeline(
        _system_prompt(code_mode),
        _improve_user(spec, findings, perspectives, consensus),
        finalize, model=model,
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
    "dead_controls": "removing a control that drives nothing",
    "toggle_orphan": "co-gating an option's dependent parts",
    "scale": "fixing component scale",
    "scope": "undoing a change outside this step's scope",
    "integration": "fixing parts embedded in existing geometry",
    "qa_review": "addressing the AI reviewer's rejections",
    "provider": "the AI provider hiccuped — retrying",
}


def _stream_attempts(system, user, finalize, retry: bool = True, model: str | None = None,
                     max_tokens: int | None = None):
    """Stream ONE classified-retry call: yield the model's text live and
    RETURN the terminal result envelope — ``{"ok": True, "result", "attempts"}``
    on success, or ``{"ok": False, "error", "kind", "attempts"}`` on a
    classified failure. Never yields a sentinel; the caller decides when to
    terminate the stream. Shared by :func:`_stream_pipeline` (single call → one
    sentinel) and :func:`stream_generate_spec`'s layered pipeline (a chain of
    calls → one sentinel).

    ``finalize(raw, lenient=False)`` turns the streamed text into the result.
    On a classified failure it announces what went wrong and re-prompts with a
    targeted correction — up to MAX_ATTEMPTS model calls; the final attempt
    finalizes leniently, a "truncated" failure escalates ``max_tokens``, and a
    transient provider error backs off before retrying."""
    payload = None
    max_attempts = MAX_ATTEMPTS if retry else 1
    history: list = []
    raw = ""
    current_tokens = max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS
    try:
        for attempt in range(1, max_attempts + 1):
            message = user if attempt == 1 else _correction_user(user, attempt, history, raw)
            current_tokens = _escalate_truncated_budget(current_tokens, history)
            parts = []
            try:
                for chunk in complete_stream(system, message, model=model,
                                             max_tokens=current_tokens):
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
                _backoff_before_retry(attempt)
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
    except Exception as exc:  # last-resort guard — see below
        # LLMError and SpecGenerationError are the EXPECTED failures, handled
        # per-attempt above. Anything else (a finalize bug, a non-LLMError
        # transport error, ...) would otherwise escape this generator and tear
        # the SSE stream WITHOUT a terminal sentinel — which is exactly the
        # cryptic "The stream ended without a result" the frontend shows. Catch
        # it here so the client always receives a result envelope to act on.
        # (GeneratorExit is a BaseException, not Exception, so a client
        # disconnect still closes the generator cleanly and is not swallowed.)
        payload = {"ok": False,
                   "error": f"Generation failed unexpectedly: {exc}",
                   "kind": "unknown"}
    if payload is None:  # defensive: loop somehow produced no verdict at all
        payload = {"ok": False, "error": "Generation produced no result",
                   "kind": "unknown"}
    return payload


def _with_terminal_sentinel(inner):
    """Wrap a streaming generator so the client ALWAYS receives exactly ONE
    terminal sentinel + result envelope.

    Every streaming endpoint ends with ``STREAM_SENTINEL + json`` — the
    frontend raises "The stream ended without a result" when it never arrives,
    throwing away every token the run already spent. Individual passes guard
    themselves, but an exception raised BETWEEN them (a combine step, a
    finalizer, a bug) escapes the generator and tears the stream mid-flight.
    This wrapper is the backstop: it forwards the inner stream untouched, and
    if that stream ends — or dies — without having emitted a sentinel, it
    emits one describing the failure. A run can then always be acted on
    instead of vanishing.

    Only ``Exception`` is caught: ``GeneratorExit`` is a ``BaseException``, so
    a client disconnect still closes the generator cleanly rather than trying
    to write to a socket nobody is reading.
    """
    emitted = False
    try:
        for chunk in inner:
            if not emitted and isinstance(chunk, str) and STREAM_SENTINEL in chunk:
                emitted = True
            yield chunk
    except Exception as exc:
        if not emitted:
            yield STREAM_SENTINEL + json.dumps(
                {"ok": False, "error": f"Generation failed unexpectedly: {exc}",
                 "kind": "unknown"})
        return
    if not emitted:
        yield STREAM_SENTINEL + json.dumps(
            {"ok": False, "error": "Generation produced no result", "kind": "unknown"})


def _stream_pipeline(system, user, finalize, retry: bool = True, model: str | None = None,
                     max_tokens: int | None = None):
    """A single streaming call → live text + ONE terminal sentinel payload
    (thin wrapper over :func:`_stream_attempts`). Used by refine / focus /
    wizard / improve / review / standards — behavior is identical to before
    the ``_stream_attempts`` extraction."""
    def inner():
        payload = yield from _stream_attempts(system, user, finalize, retry=retry,
                                              model=model, max_tokens=max_tokens)
        yield STREAM_SENTINEL + json.dumps(payload)

    return _with_terminal_sentinel(inner())


def stream_generate_spec(prompt: str, code_mode: str = "strict", model: str | None = None,
                         clarifications: list | None = None):
    """Streaming twin of :func:`generate_spec`: the four-persona design brief,
    then the 4-LAYER pipeline built live — one visible stage per layer
    (structure → function → connections → materials) — then an advisory QA
    review, then a single terminal payload. Layers 2-4 degrade gracefully (a
    skipped layer keeps the build so far). The result carries brief/panel/qa
    and the per-layer "layers" trace. Answered clarifying questions fold into
    the request before the brief pass."""
    request = _clarified_prompt(prompt, clarifications)

    def gen():
        yield "[refining your request into a design brief]\n\n"
        parts = []
        try:
            for chunk in complete_stream(_panel_system(), _enhance_user(request),
                                         temperature=0.5, max_tokens=700, model=model):
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
        except Exception:
            # an UNEXPECTED brief-pass failure must never tear the stream —
            # fall through to designing from the raw request, exactly like a
            # transient hiccup. The spec pass below still emits the one
            # terminal sentinel, so the client always gets a result.
            parts = []
            yield "\n[brief pass unavailable — designing from your request as-is]\n"
        brief, panel = _finalize_panel("".join(parts), request)
        panel_request = _panel_request(brief, panel)

        # Layer 1 — STRUCTURE: generate the buildable skeleton.
        yield f"\n\n[layer 1/4 — {_LAYER_LABEL['structure']}]\n\n"
        payload = yield from _stream_attempts(
            _system_prompt(code_mode), _structure_user(panel_request),
            lambda raw, lenient=False: _postprocess(raw, code_mode, lenient_buildability=lenient),
            model=model)
        if not payload["ok"]:
            # nothing structural to build → ship the classified error as-is
            yield STREAM_SENTINEL + json.dumps(payload)
            return
        spec = payload["result"]["spec"]
        trace: list = [{"layer": "structure", "status": "built"}]

        # Layers 2-4 — FUNCTION, CONNECTIONS, MATERIALS: scoped edits on the
        # accumulated spec; a stubborn layer is skipped and the build carries on.
        for n, layer in enumerate(("function", "connections", "materials"), start=2):
            yield f"\n\n[layer {n}/4 — {_LAYER_LABEL[layer]}]\n\n"
            layer_payload = yield from _stream_attempts(
                _system_prompt(code_mode), _layer_user(layer, spec, brief),
                _edit_finalize(code_mode, spec,
                               wizard_step_key="materials" if layer == "materials" else None,
                               integration_gate=True),
                model=model)
            if layer_payload["ok"]:
                spec = layer_payload["result"]["spec"]
                trace.append({"layer": layer, "status": "built"})
            else:
                yield f"\n[{_LAYER_LABEL[layer]} layer skipped — keeping the build so far]\n"
                trace.append({"layer": layer, "status": "skipped",
                              "error": str(layer_payload.get("error", ""))[:200]})

        # Combine into the terminal payload. The layers already validated and
        # BUILT this spec, so a failure here (a lenient re-validate that trips,
        # a finalizer bug) must never throw that work — and its tokens — away:
        # fall back to shipping the accumulated spec. The client recomputes
        # code violations locally anyway.
        yield "\n\n[combining the layers]\n\n"
        try:
            final = _finalize_layered(spec, panel_request, brief, panel, trace,
                                      code_mode, model, run_qa=False)
        except Exception as exc:
            final = {"spec": spec, "violations": [], "brief": brief, "layers": trace,
                     "combine_error": str(exc)[:200]}
            if panel:
                final["panel"] = panel
        yield STREAM_SENTINEL + json.dumps({"ok": True, "result": final})

    return _with_terminal_sentinel(gen())


def stream_refine_spec(spec: dict, message: str, code_mode: str = "strict",
                       model: str | None = None):
    return _stream_pipeline(
        _system_prompt(code_mode), _refine_user(spec, message),
        _edit_finalize(code_mode, spec, integration_gate=True),
        model=model,
    )


def stream_focus_spec(spec: dict, area: str, code_mode: str = "strict",
                      model: str | None = None):
    return _stream_pipeline(
        _system_prompt(code_mode), _focus_user(spec, area),
        _edit_finalize(code_mode, spec),
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
        _edit_finalize(code_mode, spec, wizard_step_key=step, integration_gate=True),
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
