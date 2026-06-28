"""Shared Gemini call helper: build the request, retry, return parsed JSON.

``generate_json`` is small and synchronous; callers add their own concurrency.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from google import genai
from google.genai import types

from . import config
from .json_utils import parse_json_robust

logger = logging.getLogger(__name__)

# (bytes, mime_type)
ImageInput = Tuple[bytes, str]

# Global cap on simultaneous Gemini calls (see config.LLM_MAX_CONCURRENCY). One
# throttle for the whole run, independent of how many folios/workers are active.
_concurrency = threading.BoundedSemaphore(max(1, config.LLM_MAX_CONCURRENCY))


def set_max_concurrency(n: int) -> None:
    """Reset the global Gemini concurrency cap (call before processing starts)."""
    global _concurrency
    _concurrency = threading.BoundedSemaphore(max(1, int(n)))


# Substrings (matched case-insensitively) marking a transient error worth backing
# off and retrying: rate limits, 5xx, AND network/timeout failures (a dropped or
# slow connection), as opposed to a permanent client error (e.g. 400/invalid).
_RETRYABLE = (
    "429", "500", "502", "503", "504",
    "resource_exhausted", "unavailable", "internal", "deadline", "overloaded",
    "timeout", "timed out", "connection", "connect error", "max retries",
    "getaddrinfo", "remote end closed", "temporarily unavailable",
    "broken pipe", "reset by peer", "read error", "network",
)
_BACKOFF_BASE = 1.5  # seconds; grows as base * 2**attempt + jitter


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(tok in msg for tok in _RETRYABLE)


def image_part(data: bytes, mime: str = "image/jpeg") -> types.Part:
    """Wrap raw image bytes as a Gemini inline-data part."""
    return types.Part(inline_data=types.Blob(mime_type=mime, data=data))


def generate_json(
    client: genai.Client,
    model_id: str,
    prompt: str,
    *,
    thinking_level: str = "medium",
    temperature: Optional[float] = None,
    system_instruction: Optional[str] = None,
    images: Optional[Sequence[ImageInput]] = None,
    cached_content: Optional[str] = None,
    max_attempts: int = 3,
    default: Any = None,
    stage: str = "llm",
) -> Any:
    """Call Gemini, expect JSON, return the parsed object.

    Args:
        client:         Gemini client.
        model_id:       Model for this call.
        prompt:         Text prompt.
        thinking_level: ``none`` | ``low`` | ``medium`` | ``high``.
        temperature:    Sampling temperature. ``None`` leaves the API default.
        system_instruction: Optional system prompt (house style / standing rules).
        images:         Optional sequence of ``(bytes, mime)`` to attach.
        cached_content: Optional cached-content resource name to reference
                        (e.g. a page image cached once and reused across calls).
        max_attempts:   Retry count on transport / decode failure.
        default:        Returned if every attempt fails (e.g. ``[]`` or ``{}``).
        stage:          Label for log messages.

    Returns:
        The parsed JSON (``list``/``dict``/scalar), or ``default``.
    """
    parts: List[types.Part] = [types.Part(text=prompt)]
    for data, mime in (images or []):
        parts.append(image_part(data, mime))
    contents = [types.Content(parts=parts)]

    cfg_kwargs: dict = dict(
        thinking_config=types.ThinkingConfig(thinking_level=thinking_level),
        response_mime_type="application/json",
    )
    if temperature is not None:
        cfg_kwargs["temperature"] = temperature
    if system_instruction:
        cfg_kwargs["system_instruction"] = system_instruction
    if cached_content:
        cfg_kwargs["cached_content"] = cached_content
    config = types.GenerateContentConfig(**cfg_kwargs)

    # Transient errors (rate limits / 5xx) get extra attempts with exponential
    # backoff so a burst under high concurrency doesn't silently fall back to
    # ``default`` (an empty region). Non-transient failures keep the normal budget.
    transient_cap = max(max_attempts, 5)
    attempt = 0
    while True:
        attempt += 1
        retryable = False
        try:
            with _concurrency:   # global cap on simultaneous Gemini calls
                response = client.models.generate_content(
                    model=model_id, contents=contents, config=config,
                )
            parsed = parse_json_robust(response.text)
            if parsed is not None:
                return parsed
            logger.error("[%s] empty/invalid JSON (attempt %d)", stage, attempt)
        except json.JSONDecodeError as exc:
            logger.error("[%s] JSON parse failed (attempt %d): %s", stage, attempt, exc)
        except Exception as exc:
            retryable = _is_retryable(exc)
            (logger.warning if retryable else logger.error)(
                "[%s] call failed (attempt %d%s): %s",
                stage, attempt, ", transient" if retryable else "", exc,
            )

        if attempt >= (transient_cap if retryable else max_attempts):
            return default
        if retryable:   # back off (slot released during the sleep)
            time.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, 1.0))
