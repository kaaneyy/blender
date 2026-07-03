"""Provider abstraction for prompt→spec generation (T2.4).

One function — ``complete(system, user)`` — behind which DeepSeek, OpenAI,
and Anthropic adapters live. Environment variables select and configure the
provider:

    LLM_PROVIDER   deepseek (default) | openai | anthropic | mock
    LLM_MODEL      optional model override
    DEEPSEEK_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY

``mock`` needs no key and returns a canned spec — used by the test suite and
for trying the UI before you have an API key.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

TIMEOUT = 90.0
REPO_ROOT = Path(__file__).resolve().parents[2]


class LLMError(RuntimeError):
    """Configuration or transport failure talking to the LLM provider."""


def _require_key(env_var: str) -> str:
    key = os.environ.get(env_var, "").strip()
    if not key:
        raise LLMError(
            f"{env_var} is not set. Add it to your environment (on Vercel: "
            f"Project → Settings → Environment Variables), then restart/redeploy."
        )
    return key


def _openai_compatible(url: str, api_key: str, model: str, system: str, user: str,
                       temperature: float, max_tokens: int) -> str:
    resp = httpx.post(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise LLMError(f"LLM provider returned {resp.status_code}: {resp.text[:300]}")
    return resp.json()["choices"][0]["message"]["content"]


def _anthropic(api_key: str, model: str, system: str, user: str,
               temperature: float, max_tokens: int) -> str:
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        json={
            "model": model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise LLMError(f"LLM provider returned {resp.status_code}: {resp.text[:300]}")
    return resp.json()["content"][0]["text"]


def _mock(user: str) -> str:
    """Keyless demo/test provider: always returns the street-light example
    (or the custom park bench when the request mentions a bench)."""
    name = "park_bench.json" if "bench" in user.lower() else "street_light.json"
    return (REPO_ROOT / "examples" / name).read_text(encoding="utf-8")


def complete(system: str, user: str, *, temperature: float = 0.4,
             max_tokens: int = 6000) -> str:
    provider = os.environ.get("LLM_PROVIDER", "deepseek").strip().lower()
    model = os.environ.get("LLM_MODEL", "").strip()

    if provider == "mock":
        return _mock(user)
    if provider == "deepseek":
        return _openai_compatible(
            "https://api.deepseek.com/v1/chat/completions",
            _require_key("DEEPSEEK_API_KEY"), model or "deepseek-chat",
            system, user, temperature, max_tokens,
        )
    if provider == "openai":
        return _openai_compatible(
            "https://api.openai.com/v1/chat/completions",
            _require_key("OPENAI_API_KEY"), model or "gpt-4o-mini",
            system, user, temperature, max_tokens,
        )
    if provider == "anthropic":
        return _anthropic(
            _require_key("ANTHROPIC_API_KEY"), model or "claude-sonnet-5",
            system, user, temperature, max_tokens,
        )
    raise LLMError(
        f"Unknown LLM_PROVIDER {provider!r} (expected deepseek, openai, anthropic, or mock)"
    )
