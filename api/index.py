"""Vercel Python function entrypoint.

Vercel detects the ASGI `app` and serves it for every /api/* request (see
the rewrite in vercel.json). The FastAPI app also mounts its routes under
/api, so paths line up with what the browser calls.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app.main import app  # noqa: E402,F401
