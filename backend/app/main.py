"""AssetForge API (FastAPI).

Milestone-1 surface: health, standards DB, schema, and spec validation.
The LLM endpoints (/generate-spec, /refine-spec — T2.1/T2.5) and the export
queue (/export — T5.1) land in later milestones per the build order.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from standards.validator import load_standards, validate_spec  # noqa: E402

app = FastAPI(title="AssetForge API", version="0.1.0")

ASSET_SPEC_SCHEMA = json.loads(
    (REPO_ROOT / "schemas" / "asset_spec.schema.json").read_text(encoding="utf-8")
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/standards")
def standards() -> dict:
    """Full US-code standards DB (the UI uses this for slider bounds/tooltips)."""
    return load_standards()


@app.get("/schemas/asset-spec")
def asset_spec_schema() -> dict:
    return ASSET_SPEC_SCHEMA


@app.post("/validate-spec")
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
