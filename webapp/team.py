"""
Sales-team directory for the web app — who leads can be assigned to, shared
with, or @mentioned.

Read from the ``[[team]]`` entries of the active business profile
(``core.business_profile``), so no personal data ever lives in source code.
"""
from __future__ import annotations

from core.business_profile import get_profile


def get_contacts_for_country(country_code: str) -> list[dict[str, str]]:
    """
    Team members covering ``country_code`` as ``{"name", "email"}`` dicts.

    Falls back to the whole team when nobody is scoped to that country, so the
    assignment / share UI is never empty.
    """
    return [{"name": m.name, "email": m.email} for m in get_profile().team_for_country(country_code)]


def all_team_contacts() -> list[dict[str, str]]:
    """Every team member — used for @mention autocomplete, which should suggest anyone."""
    return [{"name": m.name, "email": m.email} for m in get_profile().team]
