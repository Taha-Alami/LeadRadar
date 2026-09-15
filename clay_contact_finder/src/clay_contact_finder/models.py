"""Data models returned by :class:`clay_contact_finder.finder.ClayContactFinder`.

Plain, serialisable dataclasses — no behaviour beyond a couple of convenience
properties. Every field that can genuinely be absent (a domain that was never
found, an email that was never resolved) is ``Optional`` and defaults to
``None`` rather than an empty string, so callers can rely on truthiness checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Contact:
    """A single person found for a company.

    ``source`` is currently always ``"domain_search"`` -- found via a
    country-filtered people search directly on a resolved company domain
    (Clay's filters-mode search, which returns a LinkedIn URL natively, so
    every ``Contact`` this package returns is email/phone-eligible). Kept as
    a field rather than hardcoded in case a second discovery mechanism is
    reintroduced later; an earlier version had one (a name-based fallback
    search for when no domain could be found, source
    ``"name_search_fallback"``) and it was removed by explicit request in
    favor of a human correcting the domain directly when automated
    resolution comes up empty -- see ``ClayContactFinder.find``'s
    ``known_domain`` parameter.
    """

    name: str
    title: str | None
    company_name: str | None
    domain: str | None
    linkedin_url: str | None
    city: str | None
    source: str  # "domain_search"

    work_email: str | None = None
    work_email_error: str | None = None
    work_phone: str | None = None
    work_phone_error: str | None = None

    @property
    def email_eligible(self) -> bool:
        """Whether this contact can be submitted for email/phone enrichment.

        Requires ``linkedin_url``, ``domain``, AND ``company_name`` -- Clay's
        Work Email routine hard-rejects a batch containing even one item with
        a null ``Company Domain`` OR a null ``Company Name`` (400 "Expected
        type string" for the *whole* submission, not just that item;
        confirmed live for both fields), so a contact missing either isn't
        actually enrichable in practice. ``find()``'s own contacts always
        have all three, so this mostly matters for hand-built ``Contact``
        objects (e.g. from a different data source) passed to
        ``enrich_email``/``enrich_phone`` directly.
        """
        return bool(self.linkedin_url) and bool(self.domain) and bool(self.company_name)


@dataclass
class DomainResolution:
    """Result of resolving a company name to a domain (see ``finder.py`` §2-3).

    ``domain`` is the raw domain returned by whichever resolver won (search-
    grounded LLM, Clay's Find Domain, or their agreement). ``canonical_domain``
    is the same domain after Clay's Enrich Company alias-resolution (e.g.
    ``pern.com.pl`` -> ``pern.pl``) and is what should actually be used for
    contact search. Both are ``None`` if no domain could be confirmed.

    ``source == "manual_override"`` means the domain was supplied directly by
    the caller (``ClayContactFinder.find(..., known_domain=...)``) rather than
    resolved automatically -- e.g. a human confirming or correcting a prior
    automated result. ``enriched_name`` is always ``None`` in that case
    (Enrich Company is never called), and ``reason`` explains as much.
    """

    domain: str | None
    canonical_domain: str | None
    enriched_name: str | None
    source: str | None  # "agreement" | "serper_llm" | "clay_find_domain" | "manual_override" | None
    reason: str


@dataclass
class EnrichmentResult:
    """Full result of :meth:`ClayContactFinder.find` for one company.

    ``name_confirmed`` is simply whether a domain was found (``bool(domain)``)
    -- there is no separate plain-text confirmation step. ``contacts`` is
    always fully email/phone-eligible; there's no split by discovery
    mechanism to expose here (see ``Contact.source``'s docstring).
    """

    input_name: str
    input_country: str
    resolved_name: str
    domain_resolution: DomainResolution | None
    name_confirmed: bool
    contacts: list[Contact] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        """Whether at least one contact was found."""
        return bool(self.contacts)
