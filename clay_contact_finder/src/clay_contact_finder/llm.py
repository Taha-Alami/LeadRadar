"""Minimal Azure OpenAI chat-completion helper.

Deliberately small and stateless: the caller supplies their own credentials
(no environment variables are read here), and it does exactly one thing —
send a chat completion request and return the text, retrying on empty
responses. This package does not depend on any particular project's LLM
client; it brings its own.
"""

from __future__ import annotations

import logging

from openai import APIStatusError, OpenAI

from .exceptions import LLMError

logger = logging.getLogger(__name__)


def azure_openai_complete(
    messages: list[dict[str, str]],
    *,
    api_key: str,
    endpoint: str,
    deployment: str,
    temperature: float = 0.1,
    json_mode: bool = False,
    max_retries: int = 3,
) -> str:
    """Send one chat completion request to an Azure OpenAI deployment.

    Args:
        messages: OpenAI-format message list (``[{"role": "system", ...}, ...]``).
        api_key: Azure OpenAI API key.
        endpoint: Azure OpenAI resource base URL, e.g.
            ``"https://<resource>.services.ai.azure.com/openai/v1"``.
        deployment: Azure deployment name to route the request to.
        temperature: Sampling temperature. Low (0.0-0.2) is appropriate for
            every call this package makes — all of them expect structured,
            factual JSON output, never creative text.
        json_mode: When ``True``, constrains the model to return a JSON object
            (Azure's ``response_format={"type": "json_object"}``).
        max_retries: Number of attempts before giving up on an empty response.

    Returns:
        The raw string content of the model's first choice.

    Raises:
        LLMError: If Azure returns 401/403 (bad key), or every attempt returns
            an empty completion.
    """
    client = OpenAI(base_url=endpoint, api_key=api_key)
    kwargs: dict = dict(model=deployment, messages=messages, temperature=temperature)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    finish_reason = "unknown"
    for attempt in range(1, max_retries + 1):
        try:
            response = client.chat.completions.create(**kwargs)
        except APIStatusError as exc:
            if exc.status_code in (401, 403):
                raise LLMError(
                    f"Azure OpenAI authentication failed (HTTP {exc.status_code}) "
                    f"for deployment {deployment!r} -- check the API key/endpoint."
                ) from exc
            raise LLMError(f"Azure OpenAI request failed (HTTP {exc.status_code}): {exc}") from exc

        finish_reason = response.choices[0].finish_reason or "unknown"
        content = response.choices[0].message.content or ""
        if content.strip():
            return content

        logger.warning(
            "Azure OpenAI deployment %s returned empty content (finish_reason=%r) -- attempt %d/%d",
            deployment, finish_reason, attempt, max_retries,
        )

    raise LLMError(
        f"Azure OpenAI returned empty content for deployment {deployment!r} on all "
        f"{max_retries} attempts (last finish_reason={finish_reason!r})."
    )
