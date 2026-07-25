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

#: ``complete``/``complete_stream``'s default output budget, named so callers
#: (the retry loop's truncation-escalation logic) can reason about "the
#: default" without hard-coding the magic number a second time. The
#: parameter default below is intentionally still a literal 6000 — this
#: constant mirrors it, it does not replace the signature.
DEFAULT_MAX_TOKENS = 6000

#: Upper bound a truncated-retry may escalate ``max_tokens`` to, regardless
#: of how many times a reply keeps truncating. Keeps a runaway spec from
#: burning an unbounded budget on repeated retries. Sized so a heavy
#: reasoning trace AND a full spec both fit: a thinking model can spend
#: >10k tokens deliberating before the JSON, and 16k left too little room
#: after that for a large spec, so the answer kept truncating even on the
#: final retry (the "cut off before the JSON finished" failure).
TRUNCATION_MAX_TOKENS_CEILING = 32000

#: DeepSeek models the UI dropdown may request. Anything outside this set is
#: ignored (falls back to the env default) so a client can never inject an
#: arbitrary model string.
#: deepseek-chat was retired (out of support), so the balanced/default slot
#: is now the flash variant; the pro variant is the reasoning model.
DEEPSEEK_MODELS = ("deepseek-v4-flash", "deepseek-v4-pro")
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"

#: "Reasoning"/"thinking" models spend a chain of thought *before* the answer.
#: They stream that thought in a separate ``reasoning_content`` field (or, some
#: builds, inline in ``<think>…</think>`` tags), burn extra output tokens on it,
#: and take much longer to first emit the JSON. So they get a longer timeout and
#: a bigger token budget, and their reasoning is fenced off from the answer (see
#: :func:`strip_reasoning`) so JSON parsing is never fooled by braces the model
#: wrote while thinking.
#: BOTH current DeepSeek V4 models emit a reasoning trace — flash was observed
#: spending most of a plain 6k budget on thinking and truncating the spec JSON,
#: so it needs the same reasoning-sized timeout/budget as pro, not the plain-
#: model defaults. (Harmless headroom even for a model that happens not to
#: think much.)
REASONING_MODELS = frozenset({"deepseek-v4-flash", "deepseek-v4-pro"})
#: Reasoning can run for minutes; don't cut it off at the plain-model timeout.
REASONING_TIMEOUT = 300.0
#: The reasoning trace eats into the output budget — leave plenty of room so the
#: JSON answer that follows it is never truncated.
REASONING_MIN_TOKENS = 8000

#: reasoning/thinking wrappers different providers emit around (before, or
#: interleaved with) the real answer — DeepSeek's <think>, and the <thinking>/
#: <reasoning>/<thought>/<scratchpad> variants other models use. Matched
#: case-insensitively and tolerant of attributes (e.g. <think signature="…">).
_REASONING_TAGS = ("think", "thinking", "reasoning", "thought", "scratchpad")
_REASONING_BLOCK_RE = re.compile(
    r"<(" + "|".join(_REASONING_TAGS) + r")\b[^>]*>.*?</\1\s*>",
    re.DOTALL | re.IGNORECASE,
)
_REASONING_OPEN_RE = re.compile(
    r"<(?:" + "|".join(_REASONING_TAGS) + r")\b[^>]*>", re.IGNORECASE
)


def is_reasoning_model(model: str) -> bool:
    return (model or "").strip() in REASONING_MODELS


def _budget(model: str, max_tokens: int) -> tuple[float, int]:
    """Per-call (timeout, max_tokens): reasoning models get the longer timeout
    and at least ``REASONING_MIN_TOKENS`` so their answer survives the thinking."""
    if is_reasoning_model(model):
        return REASONING_TIMEOUT, max(max_tokens, REASONING_MIN_TOKENS)
    return TIMEOUT, max_tokens


def strip_reasoning(text: str) -> str:
    """Drop any reasoning/thinking block a model emits around its answer —
    whether it thinks first, or interleaves thought between answer chunks —
    so downstream JSON parsing sees only the answer. Handles every tag in
    ``_REASONING_TAGS`` (case-insensitive, attributes allowed). Closed blocks
    are removed wherever they appear; once those are gone, any *surviving*
    opener is unclosed (the model was cut off mid-thought), so everything
    from the first such opener on is dropped — there is no answer after it, so
    parsing then fails loudly, which is the behavior we want for a truncated
    response. Untagged reasoning that simply precedes the JSON is handled
    downstream by the JSON extractor (``spec_ai._strip_fences``), not here."""
    text = _REASONING_BLOCK_RE.sub("", text)
    opens = list(_REASONING_OPEN_RE.finditer(text))
    if opens:
        text = text[: opens[0].start()]
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


def active_provider() -> str:
    """The normalized provider id from ``LLM_PROVIDER`` (default ``deepseek``).
    Exposed so callers can special-case the keyless ``mock`` provider — e.g.
    skip real retry backoff, since mock never rate-limits."""
    return os.environ.get("LLM_PROVIDER", "deepseek").strip().lower()


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
    # Extended-thinking models return the chain of thought as its own leading
    # content block(s) (type "thinking"/"redacted_thinking") before the answer
    # block (type "text"), so pick the first text block rather than content[0].
    blocks = resp.json().get("content") or []
    answer = next(
        (b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"),
        "",
    )
    return strip_reasoning(answer)


def _mock(user: str) -> str:
    """Keyless demo/test provider: canned responses per request kind."""
    if user.startswith("ENHANCE PROMPT"):
        request = user.split("Request:", 1)[-1].strip()
        return (
            f"Design brief: {request}. Exactly the requested asset and its "
            "named parts, nothing extra; code-compliant, galvanized-steel or "
            "cast-iron structure with a clear load path to a base plate at "
            "grade; members joined by through-bolts or band clamps; every "
            "named part given its own real dimensions (height, width, "
            "diameter, wall thickness) as adjustable sliders."
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
    if user.startswith("CLARIFY REQUEST"):
        return json.dumps({"questions": [
            {"question": "Who will mainly use it, and where?",
             "options": ["Adults in a public park", "Children at a playground",
                         "Customers outside a shop"]},
            {"question": "What overall size fits the site?",
             "options": ["Compact (about 4 ft)", "Standard (about 6 ft)",
                         "Large (about 8 ft)"]},
            {"question": "What style and material should it be?",
             "options": ["Classic cast iron with wood", "Modern galvanized steel",
                         "Minimal powder-coated metal"]},
        ]})
    if user.startswith("QA REVIEW"):
        return json.dumps({"verdict": "approve", "problems": [], "fixes": []})
    if user.startswith("REVIEW CONNECTIONS"):
        return json.dumps({"findings": [{
            "severity": "warning",
            "title": "Mock AI review — configure a real LLM provider",
            "detail": ("This demo finding comes from the keyless mock "
                       "provider. With a real provider the AI walks every "
                       "joint like a fabricator and proposes reviewed, "
                       "confirmable fixes."),
        }]})
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
             max_tokens: int = DEFAULT_MAX_TOKENS, model: str | None = None) -> str:
    provider = active_provider()
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
                    max_tokens: int = DEFAULT_MAX_TOKENS, model: str | None = None) -> Iterator[str]:
    """Streaming twin of :func:`complete`."""
    provider = active_provider()
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
