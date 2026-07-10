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
import re
import time
from pathlib import Path
from typing import Iterator

import httpx

#: Default request timeout for a plain (non-reasoning) completion.
TIMEOUT = 90.0
REPO_ROOT = Path(__file__).resolve().parents[2]

#: DeepSeek models the UI dropdown may request. Anything outside this set is
#: ignored (falls back to the env default) so a client can never inject an
#: arbitrary model string.
DEEPSEEK_MODELS = ("deepseek-chat", "deepseek-v4-flash", "deepseek-v4-pro")
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"

#: "Reasoning"/"thinking" models spend a chain of thought *before* the answer.
#: They stream that thought in a separate ``reasoning_content`` field (or, some
#: builds, inline in ``<think>…</think>`` tags), burn extra output tokens on it,
#: and take much longer to first emit the JSON. So they get a longer timeout and
#: a bigger token budget, and their reasoning is fenced off from the answer (see
#: :func:`strip_reasoning`) so JSON parsing is never fooled by braces the model
#: wrote while thinking.
REASONING_MODELS = frozenset({"deepseek-v4-pro"})
#: Reasoning can run for minutes; don't cut it off at the plain-model timeout.
REASONING_TIMEOUT = 300.0
#: The reasoning trace eats into the output budget — leave plenty of room so the
#: JSON answer that follows it is never truncated.
REASONING_MIN_TOKENS = 8000

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def is_reasoning_model(model: str) -> bool:
    return (model or "").strip() in REASONING_MODELS


def _budget(model: str, max_tokens: int) -> tuple[float, int]:
    """Per-call (timeout, max_tokens): reasoning models get the longer timeout
    and at least ``REASONING_MIN_TOKENS`` so their answer survives the thinking."""
    if is_reasoning_model(model):
        return REASONING_TIMEOUT, max(max_tokens, REASONING_MIN_TOKENS)
    return TIMEOUT, max_tokens


def strip_reasoning(text: str) -> str:
    """Drop ``<think>…</think>`` reasoning blocks a thinking model emits before
    its answer, so downstream JSON parsing sees only the answer. A block left
    unclosed (the model was cut off mid-thought) is dropped from its opening tag
    on — there is no answer after it, so parsing then fails loudly, which is the
    behavior we want for a truncated response."""
    text = _THINK_RE.sub("", text)
    lower = text.lower()
    open_idx = lower.rfind("<think>")
    if open_idx != -1 and "</think>" not in lower[open_idx:]:
        text = text[:open_idx]
    return text


def resolve_model(provider: str, requested: str | None) -> str:
    """Pick the model id for a provider: an allowlisted per-request override
    (DeepSeek only) beats the LLM_MODEL env default, which beats the built-in
    default. Returns "" for providers whose caller supplies its own default."""
    env_model = os.environ.get("LLM_MODEL", "").strip()
    if provider == "deepseek":
        if requested and requested.strip() in DEEPSEEK_MODELS:
            return requested.strip()
        return env_model or DEFAULT_DEEPSEEK_MODEL
    return env_model


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
                       temperature: float, max_tokens: int,
                       timeout: float = TIMEOUT) -> str:
    try:
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
            timeout=timeout,
        )
    except httpx.HTTPError as exc:  # timeouts / connection trouble: retryable
        raise LLMError(f"LLM request failed: {type(exc).__name__}: {exc}") from None
    if resp.status_code != 200:
        raise LLMError(f"LLM provider returned {resp.status_code}: {resp.text[:300]}")
    # Reasoning models keep their chain of thought in a separate field; the
    # answer is in "content". Strip any inline <think> blocks defensively.
    return strip_reasoning(resp.json()["choices"][0]["message"].get("content") or "")


def _anthropic(api_key: str, model: str, system: str, user: str,
               temperature: float, max_tokens: int) -> str:
    try:
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
    except httpx.HTTPError as exc:  # timeouts / connection trouble: retryable
        raise LLMError(f"LLM request failed: {type(exc).__name__}: {exc}") from None
    if resp.status_code != 200:
        raise LLMError(f"LLM provider returned {resp.status_code}: {resp.text[:300]}")
    return resp.json()["content"][0]["text"]


def _mock(user: str) -> str:
    """Keyless demo/test provider: canned responses per request kind."""
    if user.startswith("ENHANCE PROMPT"):
        request = user.split("Request:", 1)[-1].strip()
        return (
            f"Design brief: {request}. Code-compliant, galvanized-steel or "
            "cast-iron structure with a clear load path to a base plate at "
            "grade; members joined by through-bolts or band clamps; sliders "
            "for the principal dimensions and toggles for optional features."
        )
    if user.startswith("INSTALL GUIDE"):
        return (
            "## Overview\nA parametric asset generated by AssetForge (mock guide — "
            "configure a real LLM provider for a tailored one).\n\n"
            "## Tools & materials\n- Anchor bolts, nuts, washers\n- Torque wrench, level\n\n"
            "## Site preparation\n1. Verify utility clearances.\n2. Pour footing per local code.\n\n"
            "## Assembly sequence\n1. Set base plate over anchor bolts.\n2. Erect and plumb the asset.\n"
            "3. Torque nuts evenly.\n\n"
            "## Code compliance checklist\n- Confirm dimensions against the citations in the spec.\n\n"
            "## Inspection & maintenance\nInspect fasteners annually.\n\n"
            "*Safety: a licensed engineer must approve structural anchoring for public installations.*"
        )
    if user.startswith("STANDARDS UPDATE"):
        db = json.loads((REPO_ROOT / "standards" / "us_codes.json").read_text(encoding="utf-8"))
        db["_meta"]["version"] = int(db["_meta"].get("version", 1)) + 1
        db["bike_rack"] = {
            "source": "APBP Essentials of Bike Parking (mock demo entry)",
            "parameters": {
                "height": {"min": 30, "max": 36, "default": 33, "unit": "in",
                            "code_ref": "APBP-2.2", "note": "Mock demo entry."},
            },
        }
        return json.dumps(db)
    name = "park_bench.json" if "bench" in user.lower() else "street_light.json"
    return (REPO_ROOT / "examples" / name).read_text(encoding="utf-8")


def complete(system: str, user: str, *, temperature: float = 0.4,
             max_tokens: int = 6000, model: str | None = None) -> str:
    provider = os.environ.get("LLM_PROVIDER", "deepseek").strip().lower()
    picked = resolve_model(provider, model)

    if provider == "mock":
        return _mock(user)
    if provider == "deepseek":
        deepseek_model = picked or DEFAULT_DEEPSEEK_MODEL
        timeout, max_tokens = _budget(deepseek_model, max_tokens)
        return _openai_compatible(
            "https://api.deepseek.com/v1/chat/completions",
            _require_key("DEEPSEEK_API_KEY"), deepseek_model,
            system, user, temperature, max_tokens, timeout,
        )
    if provider == "openai":
        return _openai_compatible(
            "https://api.openai.com/v1/chat/completions",
            _require_key("OPENAI_API_KEY"), picked or "gpt-4o-mini",
            system, user, temperature, max_tokens,
        )
    if provider == "anthropic":
        return _anthropic(
            _require_key("ANTHROPIC_API_KEY"), picked or "claude-sonnet-5",
            system, user, temperature, max_tokens,
        )
    raise LLMError(
        f"Unknown LLM_PROVIDER {provider!r} (expected deepseek, openai, anthropic, or mock)"
    )


# ---------------------------------------------------------------------------
# Streaming variants — yield text chunks as the provider produces them.
# ---------------------------------------------------------------------------

def _openai_compatible_stream(url: str, api_key: str, model: str, system: str,
                              user: str, temperature: float, max_tokens: int,
                              timeout: float = TIMEOUT) -> Iterator[str]:
    try:
        yield from _openai_compatible_stream_inner(
            url, api_key, model, system, user, temperature, max_tokens, timeout)
    except httpx.HTTPError as exc:  # timeouts / connection trouble: retryable
        raise LLMError(f"LLM request failed: {type(exc).__name__}: {exc}") from None


def _openai_compatible_stream_inner(url: str, api_key: str, model: str, system: str,
                                    user: str, temperature: float, max_tokens: int,
                                    timeout: float) -> Iterator[str]:
    with httpx.stream(
        "POST", url,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        },
        timeout=timeout,
    ) as resp:
        if resp.status_code != 200:
            resp.read()
            raise LLMError(f"LLM provider returned {resp.status_code}: {resp.text[:300]}")
        # A reasoning model streams its thinking in "reasoning_content" before
        # the "content" answer. We forward the thinking wrapped in <think>…</think>
        # so the live UI shows progress, but downstream parsing strips those
        # blocks (strip_reasoning) — otherwise braces the model wrote while
        # thinking would break the JSON extraction.
        in_reasoning = False
        for line in resp.iter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                delta = json.loads(data)["choices"][0]["delta"]
            except (KeyError, IndexError, json.JSONDecodeError):
                continue
            reasoning = delta.get("reasoning_content")
            if reasoning:
                if not in_reasoning:
                    in_reasoning = True
                    yield "<think>"
                yield reasoning
            content = delta.get("content")
            if content:
                if in_reasoning:
                    in_reasoning = False
                    yield "</think>\n"
                yield content
        if in_reasoning:  # stream ended still inside the thought (truncated)
            yield "</think>"


def _anthropic_stream(api_key: str, model: str, system: str, user: str,
                      temperature: float, max_tokens: int) -> Iterator[str]:
    try:
        with httpx.stream(
            "POST", "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            json={
                "model": model,
                "system": system,
                "messages": [{"role": "user", "content": user}],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True,
            },
            timeout=TIMEOUT,
        ) as resp:
            if resp.status_code != 200:
                resp.read()
                raise LLMError(f"LLM provider returned {resp.status_code}: {resp.text[:300]}")
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "content_block_delta":
                    text = event.get("delta", {}).get("text")
                    if text:
                        yield text
    except httpx.HTTPError as exc:  # timeouts / connection trouble: retryable
        raise LLMError(f"LLM request failed: {type(exc).__name__}: {exc}") from None


def complete_stream(system: str, user: str, *, temperature: float = 0.4,
                    max_tokens: int = 6000, model: str | None = None) -> Iterator[str]:
    """Streaming twin of :func:`complete`."""
    provider = os.environ.get("LLM_PROVIDER", "deepseek").strip().lower()
    picked = resolve_model(provider, model)

    if provider == "mock":
        text = _mock(user)
        for i in range(0, len(text), 64):  # simulate token flow for the UI
            yield text[i : i + 64]
            time.sleep(0.004)
        return
    if provider == "deepseek":
        deepseek_model = picked or DEFAULT_DEEPSEEK_MODEL
        timeout, max_tokens = _budget(deepseek_model, max_tokens)
        yield from _openai_compatible_stream(
            "https://api.deepseek.com/v1/chat/completions",
            _require_key("DEEPSEEK_API_KEY"), deepseek_model,
            system, user, temperature, max_tokens, timeout,
        )
        return
    if provider == "openai":
        yield from _openai_compatible_stream(
            "https://api.openai.com/v1/chat/completions",
            _require_key("OPENAI_API_KEY"), picked or "gpt-4o-mini",
            system, user, temperature, max_tokens,
        )
        return
    if provider == "anthropic":
        yield from _anthropic_stream(
            _require_key("ANTHROPIC_API_KEY"), picked or "claude-sonnet-5",
            system, user, temperature, max_tokens,
        )
        return
    raise LLMError(
        f"Unknown LLM_PROVIDER {provider!r} (expected deepseek, openai, anthropic, or mock)"
    )
