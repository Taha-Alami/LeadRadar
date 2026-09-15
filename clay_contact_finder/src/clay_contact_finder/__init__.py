"""clay_contact_finder -- B2B company/contact enrichment on top of Clay's Public API.

Public API::

    from clay_contact_finder import ClayContactFinder

    finder = ClayContactFinder(
        clay_api_key="clay_...",
        azure_api_key="...",
        azure_endpoint="https://<resource>.services.ai.azure.com/openai/v1",
        azure_deployment="gpt-4.1-mini",
        serper_api_key="...",  # optional but strongly recommended, see README
    )
    result = finder.find("CPK", "Poland")
    finder.enrich_email(result.contacts)
    finder.enrich_phone(result.contacts)

See the README for the full pipeline explanation, design rationale, and known
limitations.
"""

from .exceptions import ClayAPIError, ClayContactFinderError, ConfigurationError, LLMError
from .finder import ClayContactFinder, encode_linkedin_url
from .models import Contact, DomainResolution, EnrichmentResult

__version__ = "0.1.0"

__all__ = [
    "ClayContactFinder",
    "Contact",
    "DomainResolution",
    "EnrichmentResult",
    "ClayContactFinderError",
    "ClayAPIError",
    "LLMError",
    "ConfigurationError",
    "encode_linkedin_url",
]
