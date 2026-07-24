"""AssetForge API (FastAPI).

Runs two ways with the same code:
  * uvicorn/Docker:  uvicorn backend.app.main:app
  * Vercel Python function via api/index.py (routes are mounted under /api
    as well, which is the path Vercel and the Vite dev proxy use).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from standards.validator import load_standards, validate_spec  # noqa: E402

from .llm import LLMError  # noqa: E402
from . import github_sync, spec_ai  # noqa: E402

app = FastAPI(title="AssetForge API", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

router = APIRouter()

ASSET_SPEC_SCHEMA = json.loads(
    (REPO_ROOT / "schemas" / "asset_spec.schema.json").read_text(encoding="utf-8")
)


#: DeepSeek model ids the dropdown may request (empty = server default).
_MODEL_PATTERN = "^(deepseek-v4-flash|deepseek-v4-pro)?$"


class Clarification(BaseModel):
    """One answered clarifying question riding into generation."""

    question: str = Field(min_length=1, max_length=300)
    answer: str = Field(min_length=1, max_length=300)


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=2000)
    code_mode: str = Field(default="strict", pattern="^(strict|advisory)$")
    model: str = Field(default="", pattern=_MODEL_PATTERN)
    #: answered clarifying questions (see /clarify-request); optional
    clarifications: list[Clarification] = Field(default_factory=list, max_length=6)


class ClarifyRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=2000)
    model: str = Field(default="", pattern=_MODEL_PATTERN)


class RefineRequest(BaseModel):
    spec: dict
    message: str = Field(min_length=1, max_length=2000)
    code_mode: str = Field(default="strict", pattern="^(strict|advisory)$")
    model: str = Field(default="", pattern=_MODEL_PATTERN)


class ImproveRequest(BaseModel):
    spec: dict
    code_mode: str = Field(default="strict", pattern="^(strict|advisory)$")
    model: str = Field(default="", pattern=_MODEL_PATTERN)


class FocusRequest(BaseModel):
    spec: dict
    area: str = Field(min_length=1, max_length=2000)
    code_mode: str = Field(default="strict", pattern="^(strict|advisory)$")
    model: str = Field(default="", pattern=_MODEL_PATTERN)


class VariationsRequest(BaseModel):
    spec: dict
    #: number of distinct variants to attempt (survivors may be fewer)
    count: int = Field(default=4, ge=2, le=6)
    code_mode: str = Field(default="strict", pattern="^(strict|advisory)$")
    model: str = Field(default="", pattern=_MODEL_PATTERN)


class WizardStepRequest(BaseModel):
    spec: dict
    step: str = Field(pattern="^(connections|materials|details)$")
    #: optional user steer for the step; empty runs the default pass
    message: str = Field(default="", max_length=2000)
    code_mode: str = Field(default="strict", pattern="^(strict|advisory)$")
    model: str = Field(default="", pattern=_MODEL_PATTERN)


class InstallGuideRequest(BaseModel):
    spec: dict


class ReviewConnectionsRequest(BaseModel):
    spec: dict
    model: str = Field(default="", pattern=_MODEL_PATTERN)


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.get("/standards")
def standards() -> dict:
    """Full US-code standards DB (the UI uses this for slider bounds/tooltips)."""
    return load_standards()


@router.get("/schemas/asset-spec")
def asset_spec_schema() -> dict:
    return ASSET_SPEC_SCHEMA


@router.post("/validate-spec")
def validate(spec: dict) -> dict:
    """Validate (and in strict mode clamp) an AssetSpec against US codes,
    plus the buildability contact-graph check (floating parts, below-grade
    geometry, declared connections with no real contact)."""
    try:
        import jsonschema

        jsonschema.validate(spec, ASSET_SPEC_SCHEMA)
    except ImportError:
        pass  # schema check is best-effort; the validator below still runs
    except jsonschema.ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid AssetSpec: {exc.message}")

    result = validate_spec(spec).to_dict()
    try:  # buildability findings are best-effort: an unbuildable spec just skips them
        from blender.builders.base import compute_primitives
        from blender.builders.connectivity import check_buildability

        result["violations"] = result["violations"] + check_buildability(
            compute_primitives(result["spec"]), result["spec"]
        )
    except Exception:
        pass
    return result


@router.post("/clarify-request")
def clarify(body: ClarifyRequest) -> dict:
    """3 AI-written clarifying questions x 3 offered answers for a raw
    request — the UI shows them as dropdowns (plus a type-your-own blank)
    before generating, so a basic request surfaces the real one behind it."""
    try:
        return spec_ai.clarify_request(body.prompt, model=body.model)
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except spec_ai.SpecGenerationError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"The AI could not produce usable questions: {exc}",
        )


@router.post("/generate-spec")
def generate(body: GenerateRequest) -> dict:
    """T2.1: prompt (+ answered clarifying questions) → validated AssetSpec
    + code violations."""
    try:
        return spec_ai.generate_spec(
            body.prompt, body.code_mode, model=body.model,
            clarifications=[c.model_dump() for c in body.clarifications],
        )
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except spec_ai.SpecGenerationError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"The AI returned an invalid spec twice in a row: {exc}. Try rephrasing.",
        )


@router.post("/refine-spec")
def refine(body: RefineRequest) -> dict:
    """T2.5: current spec + chat message → modified, re-validated spec."""
    try:
        return spec_ai.refine_spec(body.spec, body.message, body.code_mode, model=body.model)
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except spec_ai.SpecGenerationError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"The AI returned an invalid spec twice in a row: {exc}. Try rephrasing.",
        )


@router.post("/improve-spec")
def improve(body: ImproveRequest) -> dict:
    """Current spec + the deterministic Python-side checks (connection
    audit, buildability, US-code validation) → an AI-improved,
    re-validated spec that fixes every finding. The findings fed to the AI
    ride along in the response as "findings"."""
    try:
        return spec_ai.improve_spec(body.spec, body.code_mode, model=body.model)
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except spec_ai.SpecGenerationError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"The AI returned an invalid spec twice in a row: {exc}. Try rephrasing.",
        )


@router.post("/focus-spec")
def focus(body: FocusRequest) -> dict:
    """Deep-detail ONE area of the current spec, leaving the rest untouched."""
    try:
        return spec_ai.focus_spec(body.spec, body.area, body.code_mode, model=body.model)
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except spec_ai.SpecGenerationError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"The AI returned an invalid spec twice in a row: {exc}. Try rephrasing.",
        )


@router.post("/variations-spec")
def variations(body: VariationsRequest) -> dict:
    """N independent, distinct, code-clamped, test-built variants of the
    current spec (see ``spec_ai.variations_spec``): each is produced by its
    own single-spec edit call along a fixed perturbation axis (proportion,
    mass, ornament, stance, ...), so a stubborn variant is simply dropped —
    the response carries only the survivors."""
    try:
        return spec_ai.variations_spec(body.spec, body.count, body.code_mode, model=body.model)
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except spec_ai.SpecGenerationError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"No variant survived generation: {exc}. Try rephrasing.",
        )


@router.post("/wizard-step")
def wizard_step(body: WizardStepRequest) -> dict:
    """One guided-build step (connections | materials | details): a scoped
    refinement, optionally steered by the user's message."""
    try:
        return spec_ai.wizard_step(
            body.spec, body.step, body.message, body.code_mode, model=body.model
        )
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except spec_ai.SpecGenerationError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"The AI returned an invalid spec twice in a row: {exc}. Try rephrasing.",
        )


@router.post("/review-connections")
def review_connections(body: ReviewConnectionsRequest) -> dict:
    """AI fabrication review of the spec's connections. Findings arrive in
    the deterministic auditor's format (same nudge/declare/undeclare fix
    ops), sanitized and test-built server-side; the UI applies them only
    after the user confirms."""
    try:
        return spec_ai.review_connections(body.spec, model=body.model)
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except spec_ai.SpecGenerationError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"The AI review failed validation repeatedly: {exc}",
        )


@router.post("/install-guide")
def install_guide(body: InstallGuideRequest) -> dict:
    """AI-written installation instructions grounded in the current spec and
    the generated joint schedule (also returned for BOM use)."""
    try:
        guide, schedule = spec_ai.generate_install_guide(body.spec)
        return {"guide": guide, "joint_schedule": schedule}
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.post("/update-standards")
def update_standards() -> dict:
    """AI-proposed refresh of standards/us_codes.json (structurally
    validated). Committed straight to GitHub when GITHUB_TOKEN is
    configured; otherwise returned for manual download."""
    try:
        result = spec_ai.propose_standards_update()
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except spec_ai.SpecGenerationError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"The AI's proposal failed validation twice: {exc}",
        )

    commit = github_sync.commit_file(
        "standards/us_codes.json",
        json.dumps(result["proposal"], indent=2) + "\n",
        "Update US-code standards DB (AI-proposed via AssetForge)",
    )
    return {**result, **commit}


# Streaming twins: raw LLM text as it generates, then STREAM_SENTINEL + a
# JSON payload {ok, result|error}. The UI shows the live text in a small
# "generating" card and acts on the final payload.

def _stream(gen) -> StreamingResponse:
    return StreamingResponse(gen, media_type="text/plain; charset=utf-8")


@router.post("/generate-spec-stream")
def generate_stream(body: GenerateRequest) -> StreamingResponse:
    return _stream(spec_ai.stream_generate_spec(
        body.prompt, body.code_mode, model=body.model,
        clarifications=[c.model_dump() for c in body.clarifications],
    ))


@router.post("/refine-spec-stream")
def refine_stream(body: RefineRequest) -> StreamingResponse:
    return _stream(
        spec_ai.stream_refine_spec(body.spec, body.message, body.code_mode, model=body.model)
    )


@router.post("/improve-spec-stream")
def improve_stream(body: ImproveRequest) -> StreamingResponse:
    return _stream(
        spec_ai.stream_improve_spec(body.spec, body.code_mode, model=body.model)
    )


@router.post("/focus-spec-stream")
def focus_stream(body: FocusRequest) -> StreamingResponse:
    return _stream(
        spec_ai.stream_focus_spec(body.spec, body.area, body.code_mode, model=body.model)
    )


@router.post("/wizard-step-stream")
def wizard_step_stream(body: WizardStepRequest) -> StreamingResponse:
    return _stream(
        spec_ai.stream_wizard_step(
            body.spec, body.step, body.message, body.code_mode, model=body.model
        )
    )


@router.post("/review-connections-stream")
def review_connections_stream(body: ReviewConnectionsRequest) -> StreamingResponse:
    return _stream(spec_ai.stream_review_connections(body.spec, model=body.model))


@router.post("/install-guide-stream")
def install_guide_stream(body: InstallGuideRequest) -> StreamingResponse:
    return _stream(spec_ai.stream_install_guide(body.spec))


@router.post("/update-standards-stream")
def update_standards_stream() -> StreamingResponse:
    def commit_fn(proposal: dict) -> dict:
        return github_sync.commit_file(
            "standards/us_codes.json",
            json.dumps(proposal, indent=2) + "\n",
            "Update US-code standards DB (AI-proposed via AssetForge)",
        )

    return _stream(spec_ai.stream_update_standards(commit_fn))


app.include_router(router)
app.include_router(router, prefix="/api")
