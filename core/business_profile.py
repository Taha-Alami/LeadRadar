"""
Business profile — the single place that describes *who* is looking for leads
and *what* counts as a good one.

Every LLM prompt in LeadRadar (triage, picker, enricher, web-app chat) is
assembled from a ``BusinessProfile`` instead of hard-coding an industry, so the
same pipeline can hunt leads for an equipment-rental company, a solar
installer, or a catering business just by pointing ``BUSINESS_PROFILE`` at a
different TOML file.

Profiles live in ``profiles/*.toml`` — see ``profiles/acme_equipment_rental.toml``
for a fully commented example.

Usage::

    from core.business_profile import get_profile

    profile = get_profile()
    print(profile.company_name)      # → "Acme Equipment Rental"
    print(profile.sector_names)      # → ("Construction", "Energy & Utilities", ...)
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT    = Path(__file__).parent.parent
_DEFAULT_PROFILE = _PROJECT_ROOT / "profiles" / "acme_equipment_rental.toml"


@dataclass(frozen=True)
class Sector:
    """One target sector the triage step can assign to an article."""

    name:          str
    description:   str
    higher_margin: bool = False   # outranks "standard" sectors when the picker ranks leads


@dataclass(frozen=True)
class TeamMember:
    """A sales-team member leads can be assigned to, shared with, or @mentioned."""

    name:      str
    email:     str
    countries: tuple[str, ...] = ()   # empty = covers every country


@dataclass(frozen=True)
class BusinessProfile:
    """
    Immutable description of the business LeadRadar is finding leads for.

    Attributes:
        company_name:       Display name used in prompts, emails, and the web app.
        tagline:            Short "what we do" phrase (e.g. "construction equipment rental").
        description:        A few sentences of context for the chat assistant.
        products:           Concrete products/services — the LLM reasons about
                            demand for *these* when judging a lead.
        fit_label:          UI/email label for the per-lead "why they need us" field.
        fit_definition:     Instruction for what the enricher writes in that field.
        relevance:          The picker's RELEVANCE hard requirement.
        quality_signals:    Ranking hints for the picker (none individually required).
        priority_high/medium/low: Definitions of each priority tier.
        sectors:            Closed vocabulary of target sectors, with descriptions.
        extra_themes_en:    Extra English Google News themes for collection.
        extra_local_themes: Extra local-language themes, keyed by language code.
        team:               Sales-team directory for the web app.
    """

    company_name:       str
    tagline:            str
    description:        str
    products:           tuple[str, ...]
    fit_label:          str
    fit_definition:     str
    relevance:          str
    quality_signals:    tuple[str, ...]
    priority_high:      str
    priority_medium:    str
    priority_low:       str
    sectors:            tuple[Sector, ...]
    extra_themes_en:    tuple[str, ...] = ()
    extra_local_themes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    team:               tuple[TeamMember, ...] = ()

    @property
    def sector_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.sectors)

    @property
    def higher_margin_sectors(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.sectors if s.higher_margin)

    @property
    def analyst_persona(self) -> str:
        """Opening 'who you work for' sentence shared by every pipeline system prompt."""
        return (
            f"You are a commercial intelligence analyst for {self.company_name} "
            f"({self.tagline}: {', '.join(self.products)})"
        )

    def team_for_country(self, country_code: str) -> list[TeamMember]:
        """Team members covering ``country_code``; falls back to the whole team so the UI is never empty."""
        members = [m for m in self.team if not m.countries or country_code in m.countries]
        return members or list(self.team)


def load_profile(path: str | Path) -> BusinessProfile:
    """
    Parse a business profile TOML file.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        KeyError:          If a required key (``company.name``, ``lead.relevance``,
                           ``priority.high/medium/low``) is missing.
        ValueError:        If the profile defines no ``[[sectors]]``.
    """
    raw      = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    company  = raw.get("company", {})
    lead     = raw.get("lead", {})
    priority = raw.get("priority", {})
    search   = raw.get("search", {})

    sectors = tuple(
        Sector(
            name          = s["name"],
            description   = s.get("description", ""),
            higher_margin = bool(s.get("higher_margin", False)),
        )
        for s in raw.get("sectors", [])
    )
    if not sectors:
        raise ValueError(f"Business profile {path} defines no [[sectors]].")

    team = tuple(
        TeamMember(
            name      = m["name"],
            email     = m["email"],
            countries = tuple(c.upper() for c in m.get("countries", [])),
        )
        for m in raw.get("team", [])
    )

    return BusinessProfile(
        company_name       = company["name"],
        tagline            = company.get("tagline", ""),
        description        = company.get("description", "").strip(),
        products           = tuple(company.get("products", [])),
        fit_label          = lead.get("fit_label", "Product fit"),
        fit_definition     = lead.get("fit_definition", "why this project plausibly needs our products or services"),
        relevance          = lead["relevance"],
        quality_signals    = tuple(lead.get("quality_signals", [])),
        priority_high      = priority["high"],
        priority_medium    = priority["medium"],
        priority_low       = priority["low"],
        sectors            = sectors,
        extra_themes_en    = tuple(search.get("extra_themes_en", [])),
        extra_local_themes = {lang: tuple(themes) for lang, themes in search.get("extra_local_themes", {}).items()},
        team               = team,
    )


@lru_cache(maxsize=None)
def _load_cached(path: str) -> BusinessProfile:
    return load_profile(path)


def get_profile() -> BusinessProfile:
    """
    Return the active business profile.

    Reads ``BUSINESS_PROFILE`` (a path, absolute or relative to the project
    root) from the environment / ``.env``; falls back to the bundled
    ``profiles/acme_equipment_rental.toml`` example. Parsed once per path.
    """
    load_dotenv(_PROJECT_ROOT / ".env", override=False)
    raw  = os.getenv("BUSINESS_PROFILE", "").strip()
    path = Path(raw) if raw else _DEFAULT_PROFILE
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return _load_cached(str(path))
