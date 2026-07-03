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


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=3, max_length=2000)
    code_mode: str = Field(default="strict", pattern="^(strict|advisory)$")


class RefineRequest(BaseModel):
    spec: dict
    message: str = Field(min_length=1, max_length=2000)
    code_mode: str = Field(default="strict", pattern="^(strict|advisory)$")


class InstallGuideRequest(BaseModel):
    spec: dict


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
    """Validate (and in strict mode clamp) an AssetSpec against US codes."""
    try:
        import jsonschema

        jsonschema.validate(spec, ASSET_SPEC_SCHEMA)
    except ImportError:
        pass  # schema check is best-effort; the validator below still runs
    except jsonschema.ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid AssetSpec: {exc.message}")

    return validate_spec(spec).to_dict()


@router.post("/generate-spec")
def generate(body: GenerateRequest) -> dict:
    """T2.1: prompt → validated AssetSpec + code violations."""
    try:
        return spec_ai.generate_spec(body.prompt, body.code_mode)
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
        return spec_ai.refine_spec(body.spec, body.message, body.code_mode)
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except spec_ai.SpecGenerationError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"The AI returned an invalid spec twice in a row: {exc}. Try rephrasing.",
        )


@router.post("/install-guide")
def install_guide(body: InstallGuideRequest) -> dict:
    """AI-written installation instructions grounded in the current spec."""
    try:
        return {"guide": spec_ai.generate_install_guide(body.spec)}
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


app.include_router(router)
app.include_router(router, prefix="/api")
