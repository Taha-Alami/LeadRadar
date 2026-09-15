"""Exception hierarchy for clay_contact_finder.

All exceptions raised by this package inherit from ``ClayContactFinderError``,
so callers can catch everything from this library with a single except clause
if they don't need to distinguish the failure mode.
"""

from __future__ import annotations


class ClayContactFinderError(Exception):
    """Base class for all exceptions raised by this package."""


class ClayAPIError(ClayContactFinderError):
    """Clay's Public API returned a non-2xx response that the caller must know about.

    Raised only for failures that should stop the current operation (e.g. a
    malformed request). Expected, recoverable failures — a domain that Clay's
    search rejects, a routine that finds nothing — are handled internally and
    surfaced as ``None``/empty results, not exceptions, so a single bad company
    never aborts a batch.
    """


class LLMError(ClayContactFinderError):
    """The configured LLM backend failed to return a usable completion."""


class ConfigurationError(ClayContactFinderError):
    """Required configuration (an API key, a routine ID) is missing or invalid."""
