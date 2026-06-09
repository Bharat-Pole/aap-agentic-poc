"""Provider-agnostic LLM text generation with a never-fail fallback chain.

This is the ONLY module in the POC that talks to a language model, and it is
used at exactly two human-facing edges: the draft-PO justification narrative
(:mod:`agents.approval`) and the notification body phrasing
(:mod:`agents.notification`). No detection, forecasting, or routing logic ever
calls it — all of that is deterministic Python.

:func:`generate` tries providers in a configured order and **never raises**:

1. **Google Gemini 2.5 Flash** via ``google-generativeai`` — only if
   ``GEMINI_API_KEY`` is set and the SDK is installed.
2. **Local Ollama** — ``POST {OLLAMA_BASE_URL}/api/generate`` with the env model
   (default ``qwen2.5``) — only if the server is reachable.
3. **Deterministic template fallback** — the caller-supplied ``fallback`` string
   (its own template render), or a last-resort echo of the prompt if none given.

Every call logs which path produced the text, so a demo can prove the provider
that was used. The master switch :data:`config.USE_LLM` short-circuits straight
to the template, giving a fully offline, byte-stable run.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

import requests

import config

log = logging.getLogger("llm.provider")

#: Canonical names for the path that produced a piece of text (also logged and,
#: where useful, recorded on agent state for the dashboard/audit).
GEMINI = "gemini"
OLLAMA = "ollama"
TEMPLATE = "template"


class GenResult(NamedTuple):
    """The generated text plus the provider path that produced it.

    Attributes:
        text: The generated (or template-fallback) string. Never empty.
        provider: One of :data:`GEMINI`, :data:`OLLAMA`, or :data:`TEMPLATE`.
    """

    text: str
    provider: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def generate(prompt: str, system: str | None = None, fallback: str | None = None) -> str:
    """Generate text from the configured provider chain, never raising.

    Args:
        prompt: The user prompt (the structured facts + the writing instruction).
        system: Optional system instruction setting role/tone/constraints.
        fallback: Deterministic text to return if no provider yields output.
            Callers pass their own template render here so the demo never fails.

    Returns:
        Generated text, or the deterministic fallback — always a non-empty string.

    Rationale: thin ``-> str`` convenience over :func:`generate_with_provider` for
        callers that do not care which path produced the text.
    """
    return generate_with_provider(prompt, system=system, fallback=fallback).text


def generate_with_provider(
    prompt: str, system: str | None = None, fallback: str | None = None
) -> GenResult:
    """Generate text and report which provider path produced it.

    Args:
        prompt: The user prompt (structured facts + writing instruction).
        system: Optional system instruction.
        fallback: Deterministic text used if every provider is unavailable.

    Returns:
        A :class:`GenResult` with the text and its provider label.

    Rationale: the agents record the provider on state so the dashboard and the
        audit log can show whether a narrative was machine-written or templated —
        the routing stays deterministic regardless of which path ran.
    """
    if not config.USE_LLM:
        log.info("USE_LLM=false -> deterministic template (no provider call).")
        return GenResult(_fallback_text(prompt, fallback), TEMPLATE)

    for provider in _provider_order():
        if not _configured(provider):
            log.debug("LLM provider %s not configured; skipping.", provider)
            continue
        try:
            if provider == GEMINI:
                text = _try_gemini(prompt, system)
            else:  # OLLAMA
                text = _try_ollama(prompt, system)
        except Exception as exc:  # never let a provider failure escape
            log.info("LLM provider %s unavailable (%s); trying next.", provider, exc)
            continue
        if text and text.strip():
            log.info("LLM text via %s (%d chars).", provider, len(text))
            return GenResult(text.strip(), provider)
        log.info("LLM provider %s returned empty text; trying next.", provider)

    log.info("No LLM provider available -> deterministic template fallback.")
    return GenResult(_fallback_text(prompt, fallback), TEMPLATE)


def _configured(provider: str) -> bool:
    """Report whether a provider is worth attempting.

    Args:
        provider: One of :data:`GEMINI` or :data:`OLLAMA`.

    Returns:
        ``True`` if the provider has the minimum config to try. Gemini needs a
        key; Ollama is always attempted (reachability is only known by trying,
        bounded by a short connect timeout).

    Rationale: skipping an unkeyed Gemini avoids a misleading "empty text" log
        and a pointless SDK import on a key-less demo.
    """
    if provider == GEMINI:
        return bool(config.GEMINI_API_KEY)
    return True


# ---------------------------------------------------------------------------
# Provider order
# ---------------------------------------------------------------------------
def _provider_order() -> list[str]:
    """Resolve the provider attempt order from the configured preference.

    Returns:
        ``[gemini, ollama]`` by default; the env-preferred provider is moved to
        the front so ``LLM_PROVIDER=ollama`` tries the local model first.

    Rationale: the spec's default order is Gemini -> Ollama, but the demo machine
        may prefer a local model; the chain still falls through either way.
    """
    base = [GEMINI, OLLAMA]
    pref = config.LLM_PROVIDER
    if pref in base:
        base.remove(pref)
        return [pref, *base]
    return base


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
def _try_gemini(prompt: str, system: str | None) -> str | None:
    """Call Google Gemini 2.5 Flash if a key and the SDK are present.

    Args:
        prompt: User prompt.
        system: Optional system instruction (passed as ``system_instruction``).

    Returns:
        The model's text, or ``None`` if Gemini is not configured/available.

    Rationale: gated on ``GEMINI_API_KEY`` so an unkeyed demo skips it instantly
        rather than importing the SDK and erroring; the key is read from env only.
    """
    if not config.GEMINI_API_KEY:
        return None
    import google.generativeai as genai  # imported lazily; optional dependency

    genai.configure(api_key=config.GEMINI_API_KEY)
    model = genai.GenerativeModel(
        config.GEMINI_MODEL,
        system_instruction=system or None,
    )
    response = model.generate_content(prompt)
    return getattr(response, "text", None)


def _try_ollama(prompt: str, system: str | None) -> str | None:
    """Call a local Ollama server's ``/api/generate`` endpoint.

    Args:
        prompt: User prompt.
        system: Optional system instruction (Ollama's ``system`` field).

    Returns:
        The generated ``response`` text, or ``None`` on a non-200 reply.

    Rationale: a short connect timeout (see :data:`config.OLLAMA_TIMEOUT`) means
        an offline demo fails over to the template almost immediately.
    """
    url = f"{config.OLLAMA_BASE_URL.rstrip('/')}/api/generate"
    payload = {
        "model": config.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
    }
    if system:
        payload["system"] = system
    resp = requests.post(url, json=payload, timeout=config.OLLAMA_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("response")


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------
def _fallback_text(prompt: str, fallback: str | None) -> str:
    """Return the deterministic last-resort text.

    Args:
        prompt: The original prompt (used only if no fallback was supplied).
        fallback: The caller's deterministic template render, if any.

    Returns:
        ``fallback`` when provided; otherwise a trimmed echo of the prompt so the
        contract "always return non-empty text" holds even with no fallback.

    Rationale: callers (Approval, Notification) own their domain templates and
        pass them in, keeping this module free of any domain knowledge.
    """
    if fallback and fallback.strip():
        return fallback.strip()
    return prompt.strip() or "(no content)"
