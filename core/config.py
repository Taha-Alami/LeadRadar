"""
Runtime configuration for LeadRadar.

All values are read from environment variables (or a ``.env`` file at the project
root, never committed). The resulting ``AppConfig`` object is frozen at
construction time so pipeline stages can receive it as a read-only dependency —
no magic globals, no accidental mutation mid-run.

Environment variable groups:
    LLM_PROVIDER      "azure" (default) or "mistral".
    AZURE_*           Azure OpenAI endpoint, API key, and deployment names.
    MISTRAL_*         Mistral credentials / models (only when LLM_PROVIDER=mistral).
    MAIL_BACKEND      "file" (default, writes to ./outbox), "smtp", or "graph".
    SMTP_*            SMTP relay settings (MAIL_BACKEND=smtp).
    GRAPH_*           Microsoft Graph app registration (MAIL_BACKEND=graph).
    EMAIL_*           Sender address and default recipient lists.
    RECIPIENTS_<CODE> Optional per-country To: list, e.g. RECIPIENTS_SPAIN.
    CC_<CODE>         Optional per-country Cc: list, merged with EMAIL_CC.
    BUSINESS_PROFILE  Path to the business profile TOML (see core/business_profile.py).

Usage::

    from core.config import AppConfig

    cfg = AppConfig.from_env()          # call once in main.py / the web app lifespan
    print(cfg.llm_provider)             # → "azure"
    print(cfg.get_recipients("SPAIN"))  # → ['sales-es@example.com']
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _load_dotenv() -> None:
    """Load .env from the project root (one level above this package directory)."""
    load_dotenv(Path(__file__).parent.parent / ".env", override=False)


def _str(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


def _float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))


def _bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _email_list(key: str, default: str = "") -> tuple[str, ...]:
    """Parse a comma-separated list of email addresses from an env var."""
    raw = os.getenv(key, default)
    return tuple(addr.strip() for addr in raw.split(",") if addr.strip())


@dataclass(frozen=True)
class AppConfig:
    """
    Immutable runtime configuration for LeadRadar.

    Constructed via ``AppConfig.from_env()``, which reads all values from
    environment variables so secrets never appear in source code.

    Attributes:
        llm_provider:          ``"azure"`` or ``"mistral"`` — both are reached through
                               the same OpenAI-compatible SDK (see ``LLMClient``).
        azure_endpoint:        Base URL of the Azure OpenAI resource.
        azure_api_key:         API key for the Azure OpenAI resource.
        deployment_scoring:    Deployment used for structured JSON calls
                               (triage, picking, extraction) — low temperature.
        deployment_writing:    Deployment used for conversational calls (web-app chat).
        mistral_api_key:       Mistral API key (only when ``llm_provider="mistral"``).
        mistral_model_scoring: Mistral model for structured calls.
        mistral_model_writing: Mistral model for conversational calls.
        mail_backend:          ``"file"``, ``"smtp"``, or ``"graph"`` — see ``core.mailer``.
        mail_outbox_dir:       Where the ``file`` backend writes messages.
        smtp_*:                SMTP relay settings for the ``smtp`` backend.
        graph_*:               Entra ID app registration for the ``graph`` backend
                               (needs the ``Mail.Send`` application permission).
        email_sender:          From address for every outgoing message.
        email_recipients:      Default To: recipients for the daily lead digest.
        email_cc:              Default Cc: recipients.
        app_base_url:          Public URL of the web app, used for deep links in emails.
        rss_sleep_s:           Politeness delay between RSS requests.
    """

    # LLM provider selection
    llm_provider:          str

    # Azure OpenAI
    azure_endpoint:        str
    azure_api_key:         str
    deployment_scoring:    str
    deployment_writing:    str

    # Mistral AI (used when llm_provider="mistral")
    mistral_api_key:       str
    mistral_model_scoring: str
    mistral_model_writing: str

    # Mail
    mail_backend:          str
    mail_outbox_dir:       str
    smtp_host:             str
    smtp_port:             int
    smtp_username:         str
    smtp_password:         str
    smtp_use_tls:          bool
    graph_tenant_id:       str
    graph_client_id:       str
    graph_client_secret:   str
    email_sender:          str
    email_recipients:      tuple[str, ...]
    email_cc:              tuple[str, ...]
    app_base_url:          str

    # Collection tuning
    rss_sleep_s:           float

    def get_recipients(self, country_code: str) -> list[str]:
        """
        Return the To: list for a country's lead digest.

        Reads ``RECIPIENTS_<COUNTRY>`` (e.g. ``RECIPIENTS_SPAIN=a@x.com,b@x.com``)
        and falls back to the shared ``email_recipients`` when unset.
        """
        raw = os.getenv(f"RECIPIENTS_{country_code.upper()}", "")
        per_country = [a.strip() for a in raw.split(",") if a.strip()]
        return per_country if per_country else list(self.email_recipients)

    def get_cc(self, country_code: str) -> list[str]:
        """Return the Cc: list for a country's lead digest — ``email_cc`` plus ``CC_<COUNTRY>``."""
        raw = os.getenv(f"CC_{country_code.upper()}", "")
        per_country = [a.strip() for a in raw.split(",") if a.strip()]
        combined = list(self.email_cc)
        combined += [a for a in per_country if a not in combined]
        return combined

    @classmethod
    def from_env(cls) -> "AppConfig":
        """
        Construct an AppConfig by reading all values from the environment.

        Loads a ``.env`` file from the project root if present (via python-dotenv),
        then falls back to actual environment variables.

        Raises:
            ValueError: If a numeric env var contains a non-numeric string.
        """
        _load_dotenv()
        return cls(
            llm_provider          = _str("LLM_PROVIDER", "azure"),
            # Azure OpenAI
            azure_endpoint        = _str("AZURE_ENDPOINT"),
            azure_api_key         = _str("AZURE_API_KEY"),
            deployment_scoring    = _str("DEPLOYMENT_SCORING", "gpt-4.1-mini"),
            deployment_writing    = _str("DEPLOYMENT_WRITING", "gpt-4.1-mini"),
            # Mistral
            mistral_api_key       = _str("MISTRAL_API_KEY"),
            mistral_model_scoring = _str("MISTRAL_MODEL_SCORING", "mistral-medium-latest"),
            mistral_model_writing = _str("MISTRAL_MODEL_WRITING", "mistral-large-latest"),
            # Mail
            mail_backend          = _str("MAIL_BACKEND", "file").strip().lower(),
            mail_outbox_dir       = _str("MAIL_OUTBOX_DIR", "outbox"),
            smtp_host             = _str("SMTP_HOST"),
            smtp_port             = _int("SMTP_PORT", 587),
            smtp_username         = _str("SMTP_USERNAME"),
            smtp_password         = _str("SMTP_PASSWORD"),
            smtp_use_tls          = _bool("SMTP_USE_TLS", True),
            # GRAPH_* preferred; AAD_* accepted so one app registration can serve both SSO and mail
            graph_tenant_id       = _str("GRAPH_TENANT_ID") or _str("AAD_TENANT_ID"),
            graph_client_id       = _str("GRAPH_CLIENT_ID") or _str("AAD_CLIENT_ID"),
            graph_client_secret   = _str("GRAPH_CLIENT_SECRET") or _str("AAD_CLIENT_SECRET"),
            email_sender          = _str("EMAIL_SENDER", "leadradar@example.com"),
            email_recipients      = _email_list("EMAIL_RECIPIENTS"),
            email_cc              = _email_list("EMAIL_CC"),
            app_base_url          = _str("APP_BASE_URL", "http://localhost:8000"),
            # Collection
            rss_sleep_s           = _float("RSS_SLEEP_S", 0.1),
        )
