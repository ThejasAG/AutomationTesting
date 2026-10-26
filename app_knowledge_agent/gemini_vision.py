"""Gemini Vision provider for the App Knowledge Agent.

Uses the NEW google-genai SDK (replaces google-generativeai).
Subclasses LLMProvider from automation/ai/provider.py so it fits
the existing provider abstraction.

Why this file exists:
- The platform's current providers (Groq, Ollama) are text-only.
- Gemini 2.0 Flash supports vision (screenshot + text in one call)
  and has a free tier of 1500 requests/day — enough for full app scans.
- Using the existing LLMProvider base class means the rest of the
  platform can treat this like any other provider.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types

from automation.ai.provider import LLMProvider, LLMResponse

logger = logging.getLogger("aka.gemini_vision")

_DEFAULT_MODEL  = "gemini-3.1-flash-lite"
_REQUEST_SPACING_S = 5.0   # keep well under free-tier 15 rpm
_MAX_RETRIES    = 2   # TRANSIENT errors only (503/500/timeout) — see
                      # is_quota_error below. Was 4 and applied to every
                      # failure including quota exhaustion, so one 503 could
                      # burn 4 of a 20/day budget, and once the daily quota
                      # was genuinely gone every remaining screen still
                      # retried 4 times before failing.


def is_quota_error(exc: Exception) -> bool:
    """True for a quota/rate-limit error retrying cannot fix within this
    run — a daily free-tier cap, not a transient server overload. Shared
    with perceiver.py so it can stop the whole run rather than failing
    through every remaining screen one at a time."""
    s = str(exc).lower()
    return ("429" in str(exc) or "quota" in s or "resource_exhausted" in s
            or "rate limit" in s)


class GeminiVisionProvider(LLMProvider):
    """Wraps the google-genai client for both text and vision calls.

    generate()            — text only, satisfies the abstract base class.
    generate_with_image() — screenshot + text → JSON string (Perceiver).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = _DEFAULT_MODEL,
        request_spacing_s: float = _REQUEST_SPACING_S,
    ):
        self._api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "GEMINI_API_KEY is not set. Add it to your .env file."
            )
        self._model   = model
        self._spacing = request_spacing_s
        self._last_call = 0.0
        # New SDK: create a Client instance, not a module-level configure().
        self._client  = genai.Client(api_key=self._api_key)

    @property
    def name(self) -> str:
        return "gemini"

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _throttle(self) -> None:
        """Block until at least _spacing seconds have passed since the last call."""
        elapsed = time.time() - self._last_call
        if elapsed < self._spacing:
            time.sleep(self._spacing - elapsed)
        self._last_call = time.time()

    def _extract_text(self, response) -> str:
        """Pull the text out of a google-genai response object."""
        try:
            return response.text or "{}"
        except Exception:
            return "{}"

    # ── Text-only generate — satisfies the abstract base class ───────────────

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        json_schema: Optional[dict] = None,
        max_tokens: int = 4096,
        temperature: float = 0.1,
    ) -> LLMResponse:
        """Text-only generation.

        Used by the Scenario Generator when it sends the full
        App Knowledge Pack to Gemini to reason about all possible
        test scenarios. No image involved — pure text in, JSON out.
        """
        # New SDK: contents is a list of strings or Part objects.
        # System instruction is a separate field in GenerateContentConfig.
        config = types.GenerateContentConfig(
            system_instruction=system_prompt or None,
            temperature=temperature,
            max_output_tokens=max_tokens,
            response_mime_type="application/json" if json_schema else None,
        )

        for attempt in range(_MAX_RETRIES):
            try:
                self._throttle()
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=user_prompt,
                    config=config,
                )
                return LLMResponse(
                    content=self._extract_text(response),
                    model=self._model,
                    usage={},
                    provider=self.name,
                )
            except Exception as e:
                if is_quota_error(e):
                    logger.error("Gemini quota exhausted — not retrying: %s", e)
                    raise
                wait = 2 ** attempt * self._spacing
                logger.warning(
                    "Gemini text call failed (attempt %d/%d): %s — "
                    "retrying in %.0fs",
                    attempt + 1, _MAX_RETRIES, e, wait,
                )
                if attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(wait)

    # ── Vision generate — the key new method ─────────────────────────────────

    def generate_with_image(
        self,
        image_path: str | Path,
        text_prompt: str,
        system_prompt: str = "",
        max_tokens: int = 2048,
        temperature: float = 0.1,
    ) -> str:
        """Send a screenshot + text prompt to Gemini and return raw JSON string.

        The Perceiver calls this once per unique screen hash.
        The system prompt tells Gemini to return only valid JSON matching
        the screen schema. The text prompt carries the trimmed
        accessibility tree so Gemini has both visual and structural signal.

        Why inline bytes and not a file URI?
        The Gemini REST API requires either a Google Cloud Storage URI or
        inline bytes for images. Inline bytes needs no GCS setup and keeps
        the pipeline fully self-contained on your Mac.
        """
        image_path = Path(image_path)
        if not image_path.exists():
            raise FileNotFoundError(
                f"Screenshot not found: {image_path}"
            )

        with open(image_path, "rb") as f:
            image_bytes = f.read()

        # New SDK: use types.Part.from_bytes() for inline image data.
        image_part = types.Part.from_bytes(
            data=image_bytes,
            mime_type="image/png",
        )

        # Contents list: [image_part, text_part]
        # System instruction goes in the config, not in contents.
        contents = [image_part, text_prompt]

        config = types.GenerateContentConfig(
            system_instruction=system_prompt or None,
            temperature=temperature,
            max_output_tokens=max_tokens,
            # Tell Gemini to return JSON — reduces markdown wrapping.
            response_mime_type="application/json",
        )

        for attempt in range(_MAX_RETRIES):
            try:
                self._throttle()
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=contents,
                    config=config,
                )
                text = self._extract_text(response)
                logger.debug(
                    "Gemini vision ok — %d chars returned",
                    len(text),
                )
                return text

            except Exception as e:
                if is_quota_error(e):
                    # Retrying cannot fix a daily cap — stop immediately so
                    # the caller (perceiver.py) can stop the whole run
                    # instead of failing through every remaining screen.
                    logger.error("Gemini quota exhausted — not retrying: %s", e)
                    raise
                wait = (2 ** attempt) * self._spacing
                logger.warning(
                    "Gemini vision call failed (attempt %d/%d): %s — "
                    "retrying in %.0fs",
                    attempt + 1, _MAX_RETRIES, e, wait,
                )
                if attempt == _MAX_RETRIES - 1:
                    raise
                time.sleep(wait)

        return "{}"

    def close(self) -> None:
        # google-genai client has no explicit close — nothing to do.
        pass