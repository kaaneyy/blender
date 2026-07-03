"""Commit files back to the GitHub repository via the REST contents API.

Used by the standards updater: when GITHUB_TOKEN is configured (a
fine-grained token with Contents read/write on this repo), the proposed
us_codes.json is committed directly; otherwise the caller falls back to
offering the file as a download.

Env vars:
    GITHUB_TOKEN    required for committing
    GITHUB_REPO     owner/repo (default kaaneyy/blender)
    GITHUB_BRANCH   target branch (default: the repo's default branch)
"""
from __future__ import annotations

import base64
import os

import httpx

API = "https://api.github.com"
TIMEOUT = 30.0


def commit_file(path: str, content: str, message: str) -> dict:
    """Returns {'committed': bool, 'detail': str, 'url': str|None}."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    repo = os.environ.get("GITHUB_REPO", "kaaneyy/blender").strip()
    if not token:
        return {
            "committed": False,
            "detail": "GITHUB_TOKEN is not set — download the proposed file instead, "
                      "or add a fine-grained token (Contents: read/write) to the "
                      "environment (on Vercel: Settings → Environment Variables).",
            "url": None,
        }

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

    branch = os.environ.get("GITHUB_BRANCH", "").strip()
    if not branch:
        r = httpx.get(f"{API}/repos/{repo}", headers=headers, timeout=TIMEOUT)
        if r.status_code != 200:
            return {"committed": False, "url": None,
                    "detail": f"Could not read repo metadata ({r.status_code}): {r.text[:200]}"}
        branch = r.json()["default_branch"]

    sha = None
    r = httpx.get(
        f"{API}/repos/{repo}/contents/{path}",
        params={"ref": branch}, headers=headers, timeout=TIMEOUT,
    )
    if r.status_code == 200:
        sha = r.json().get("sha")
    elif r.status_code != 404:
        return {"committed": False, "url": None,
                "detail": f"Could not read existing file ({r.status_code}): {r.text[:200]}"}

    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha
    r = httpx.put(
        f"{API}/repos/{repo}/contents/{path}",
        json=payload, headers=headers, timeout=TIMEOUT,
    )
    if r.status_code in (200, 201):
        commit = r.json().get("commit", {})
        return {"committed": True, "detail": f"Committed to {repo}@{branch}",
                "url": commit.get("html_url")}
    return {"committed": False, "url": None,
            "detail": f"GitHub rejected the commit ({r.status_code}): {r.text[:200]}"}
