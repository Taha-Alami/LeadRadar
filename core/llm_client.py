"""
LLM client for LeadRadar.

Supports two providers, switchable via ``LLM_PROVIDER`` in ``.env``:

* **``azure``** (default) — Azure OpenAI endpoint, uses ``AZURE_API_KEY`` and
  ``DEPLOYMENT_SCORING`` / ``DEPLOYMENT_WRITING`` deployment names.
* **``mistral``** — Mistral AI endpoint, uses ``MISTRAL_API_KEY`` and
  ``MISTRAL_MODEL_SCORING`` / ``MISTRAL_MODEL_WRITING`` model names.

Both providers share the same OpenAI-compatible SDK interface so the rest of
the codebase is completely unaware of which backend is active.

``LLMClient`` wraps the official ``openai`` SDK and adds:

* **Named shortcuts** — ``triage_articles()``, ``pick_leads()``,
  ``extract_opportunity_details()``, ``chat_with_opportunity()`` — so pipeline
  modules never hard-code model names, temperatures, or timeouts.
* **Session token tracking** — cumulative prompt and completion token counts
  across all calls made through one instance, exposed via ``token_summary``.
* **Auth guard** — HTTP 401 / 403 is re-raised as ``PermissionError`` with a
  clear message, trivial to catch in the entry points.
* **Empty-response retry** — up to 3 attempts when the model returns no content.

Usage::

    from core.config import AppConfig
    from core.llm_client import LLMClient

    cfg    = AppConfig.from_env()
    client = LLMClient(cfg)
    raw    = client.pick_leads([{"role": "system", "content": "..."}, ...])
"""
from __future__ import annotations

import json
import logging
from typing import Any

from openai import APIStatusError, OpenAI

from core.config import AppConfig

logger = logging.getLogger(__name__)


_MISTRAL_BASE_URL = "https://api.mistral.ai/v1"


class LLMClient:
    """
    Injectable LLM client for LeadRadar.

    Supports Azure OpenAI and Mistral AI, selected at construction time via
    ``cfg.llm_provider``. One instance is created per pipeline run (or per
    web-app process) and passed down to every stage — no global state,
    independently testable.

    Args:
        cfg: Loaded ``AppConfig``. The client reads ``llm_provider`` to decide
             which backend to initialise, then picks the matching credentials
             and model names from the same config object.
    """

    def __init__(self, cfg: AppConfig) -> None:
        api_key = cfg.mistral_api_key if cfg.llm_provider == "mistral" else cfg.azure_api_key
        if not api_key:
            # Don't fail at construction: the web app must still start (browsing
            # leads needs no LLM). Any actual call then fails with a clear 401 →
            # PermissionError("Check your API key in .env").
            logger.warning("No API key configured for LLM provider %r — AI features will fail until one is set.", cfg.llm_provider)
            api_key = "not-configured"
        if cfg.llm_provider == "mistral":
            self._client = OpenAI(base_url=_MISTRAL_BASE_URL, api_key=api_key)
        else:   # default: azure
            self._client = OpenAI(base_url=cfg.azure_endpoint or None, api_key=api_key)
        self._cfg               = cfg
        self.prompt_tokens:     int = 0
        self.completion_tokens: int = 0

    @property
    def active_provider(self) -> str:
        """Returns the active provider name for logging (``"azure"`` or ``"mistral"``)."""
        return self._cfg.llm_provider

    # ── Core completion method ─────────────────────────────────────────────────

    def complete(
        self,
        messages:    list[dict[str, str]],
        *,
        model:       str,
        temperature: float = 0.15,
        max_tokens:  int | None = None,
        json_mode:   bool = False,
        timeout:     float | None = None,
    ) -> str:
        """
        Send a chat completion request.

        Args:
            messages:    OpenAI-format message list
                         (``[{"role": "system", "content": "..."}]``).
            model:       Deployment / model name to route the request to.
            temperature: Sampling temperature (0.0 = deterministic, 1.0 = creative).
            max_tokens:  Hard ceiling on completion tokens. ``None`` (default)
                         lets the model self-terminate.
            json_mode:   When ``True``, sets ``response_format={"type":"json_object"}``.
            timeout:     Per-request timeout in seconds. ``None`` keeps the
                         ``openai`` client default (600 s).

        Returns:
            The raw string content of the first choice.

        Raises:
            PermissionError:       On HTTP 401 or 403 (bad or expired key).
            ValueError:            If the model returns an empty completion 3 times.
            openai.APIStatusError: For any other non-2xx HTTP error.
        """
        kwargs: dict[str, Any] = dict(model=model, messages=messages, temperature=temperature)
        if max_tokens is not None:
            token_key = "max_tokens" if self._cfg.llm_provider == "mistral" else "max_completion_tokens"
            kwargs[token_key] = max_tokens
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if timeout is not None:
            kwargs["timeout"] = timeout

        finish_reason = "unknown"
        for attempt in range(1, 4):
            try:
                response = self._client.chat.completions.create(**kwargs)
            except APIStatusError as exc:
                if exc.status_code in (401, 403):
                    provider = self._cfg.llm_provider.upper()
                    raise PermissionError(
                        f"[{provider}] Authentication failed — HTTP {exc.status_code}. "
                        "Check your API key in .env."
                    ) from exc
                raise

            if response.usage:
                self.prompt_tokens     += response.usage.prompt_tokens
                self.completion_tokens += response.usage.completion_tokens
                logger.debug(
                    "LLM %-22s  in:%4d  out:%4d  │  session total  in:%5d  out:%5d",
                    model,
                    response.usage.prompt_tokens, response.usage.completion_tokens,
                    self.prompt_tokens,            self.completion_tokens,
                )

            finish_reason = response.choices[0].finish_reason or "unknown"
            content       = response.choices[0].message.content or ""

            if content.strip():
                return content

            logger.warning(
                "LLM %s empty response (finish_reason='%s') — attempt %d/3",
                model, finish_reason, attempt,
            )

        raise ValueError(
            f"LLM returned empty content for model '{model}' on all 3 attempts "
            f"(last finish_reason='{finish_reason}'). "
            "If finish_reason='content_filter', review the provider's content filter settings."
        )

    def parse_json_response(self, raw: str) -> Any:
        """
        Strip Markdown code fences and parse JSON, with a fallback that scans
        for the first ``[`` or ``{`` so preamble text does not break parsing.

        Raises:
            json.JSONDecodeError: If no valid JSON can be extracted.
        """
        clean = raw.replace("```json", "").replace("```", "").strip()
        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            # Model added preamble text — find the first JSON container
            for ch in ("[", "{"):
                idx = clean.find(ch)
                if idx != -1:
                    try:
                        return json.loads(clean[idx:])
                    except json.JSONDecodeError:
                        pass
            raise

    # ── Model selection ───────────────────────────────────────────────────────

    def scoring_model(self) -> str:
        """Model used for structured JSON calls on the active provider."""
        return (
            self._cfg.mistral_model_scoring
            if self._cfg.llm_provider == "mistral"
            else self._cfg.deployment_scoring
        )

    def writing_model(self) -> str:
        """Model used for conversational calls on the active provider."""
        return (
            self._cfg.mistral_model_writing
            if self._cfg.llm_provider == "mistral"
            else self._cfg.deployment_writing
        )

    # ── Named shortcuts (one per pipeline phase) ───────────────────────────────
    #
    # json_mode is enabled for Mistral only on the array-returning calls: Azure
    # OpenAI's json_object response format rejects top-level JSON arrays, while
    # without it Azure follows the prompt's "return a JSON array ONLY" directly.

    def triage_articles(self, messages: list[dict[str, str]]) -> str:
        """
        Triage phase — tags every candidate article with sector, priority,
        region, and location in a single call.

        One call classifies the whole batch (200+ articles on busy days can
        take several minutes), so the timeout is raised to 20 min instead of
        the 600 s client default.
        """
        return self.complete(
            messages,
            model       = self.scoring_model(),
            temperature = 0.1,
            json_mode   = (self._cfg.llm_provider == "mistral"),
            timeout     = 1200,
        )

    def pick_leads(self, messages: list[dict[str, str]]) -> str:
        """Picking phase — selects the best handful of still-actionable leads from headlines + summaries."""
        return self.complete(
            messages,
            model       = self.scoring_model(),
            temperature = 0.15,
            json_mode   = (self._cfg.llm_provider == "mistral"),
        )

    def extract_opportunity_details(self, messages: list[dict[str, str]]) -> str:
        """
        Enrichment phase — extracts structured fields (phase, company, city,
        project type/value, product fit, …) from one scraped article.
        """
        return self.complete(
            messages,
            model       = self.scoring_model(),
            temperature = 0.1,
            json_mode   = (self._cfg.llm_provider == "mistral"),
        )

    def chat_with_opportunity(self, messages: list[dict[str, str]]) -> str:
        """
        Conversational chat for the web app — a user asks questions about one
        enriched opportunity. Higher temperature for natural responses.
        """
        return self.complete(
            messages,
            model       = self.writing_model(),
            temperature = 0.7,
        )

    # ── Observability ──────────────────────────────────────────────────────────

    @property
    def token_summary(self) -> str:
        """One-line string suitable for end-of-run log output."""
        total = self.prompt_tokens + self.completion_tokens
        return (
            f"[{self._cfg.llm_provider.upper()}]  "
            f"scoring: {self.scoring_model()}  │  "
            f"Tokens — prompt: {self.prompt_tokens:,}  "
            f"completion: {self.completion_tokens:,}  "
            f"total: {total:,}"
        )
