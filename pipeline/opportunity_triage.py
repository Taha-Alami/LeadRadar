"""
Opportunity triage — Stage 2 of the LeadRadar pipeline, between collection /
dedup and picking.

Tags EVERY candidate article (not just the ones that will eventually be
picked) with a priority tier, a sector, a region, and an in-country verdict,
based directly on the active business profile's own definitions of what makes
an opportunity worth pursuing. This gives ``opportunity_picker`` structured
signal to work with instead of raw headlines alone, and lets a human browsing
the raw article list in the web app filter by these tags before anything is
picked or enriched.

Unlike the picker (which only returns the articles worth keeping), triage
classifies the whole batch. Out-of-country articles are dropped before
persistence; everything else stays visible in ``core.news_articles``.
"""
from __future__ import annotations

import json
import logging

from core.business_profile import get_profile
from core.config import AppConfig
from core.llm_client import LLMClient
from core.news_models import NewsItem

from pipeline.country_config import country_display_name, get_country_config, get_country_regions

logger = logging.getLogger(__name__)

VALID_PRIORITIES = ("High", "Medium", "Low")


def valid_sectors() -> tuple[str, ...]:
    """The closed sector vocabulary — defined by the active business profile."""
    return get_profile().sector_names


def classification_rubric(source_description: str, country_code: str) -> list[str]:
    """
    Shared priority/sector/region classification criteria, reused verbatim by
    triage, the picker, and the enricher so each stage can genuinely
    re-derive these tags from whatever text it has available, rather than
    just trusting and copying forward the previous stage's tag (which is
    what happens if a stage is only told to "re-check" without ever being
    given the actual criteria to check against).

    Args:
        source_description: what text this stage actually has to judge from
            (e.g. "the title/summary" for triage/picker, "the article text"
            for the enricher) — used only in the REGION "null if" clause.
        country_code: determines which administrative regions and which
            city→region hints appear in the rubric.
    """
    profile      = get_profile()
    country_cfg  = get_country_config(country_code)
    regions      = country_cfg["regions"]
    region_label = country_cfg["region_label"]
    city_hints   = country_cfg["city_hints"]
    return [
        "PRIORITY — exactly one of: High, Medium, Low.",
        f"  High = {profile.priority_high}",
        f"  Medium = {profile.priority_medium}",
        f"  Low = {profile.priority_low}",
        "",
        f"SECTOR — exactly one of: {', '.join(profile.sector_names)} — or null if no sector is identifiable.",
        *(f"  {s.name} = {s.description}" for s in profile.sectors),
        "",
        f"REGION — the {region_label} where the project is physically located, exactly one of: {', '.join(regions)} — or null.",
        "  Always answer with the exact spelling of one value from this list — never translate it and never invent a new value.",
        "  If the place appears under another name (English, a co-official or local-language form, an abbreviation), choose the matching value from the list.",
        f"  If only a city, town, municipality, or province is named, use your own knowledge of which {region_label} it belongs to and return that list value. Key mappings (examples, not exhaustive): {city_hints}",
        f"  Use null when {source_description} names no specific place — in particular for national or country-wide news (government policy, national market figures, laws or EU rules, product or company news). Never use the capital's {region_label} as a default for national news.",
    ]


def _make_triage_system_prompt(country_code: str) -> str:
    """Build the triage system prompt for a specific country."""
    profile        = get_profile()
    country_name   = country_display_name(country_code)
    regions        = get_country_regions(country_code)
    example_region = json.dumps(regions[0] if regions else None)
    return "\n".join([
        f"{profile.analyst_persona}. You are triaging news articles for the {country_name} market before any of them are picked for outreach.",
        "For EVERY article in the numbered list, assign a priority, a sector, a region, and a location. Classify ALL of them — do not skip any.",
        "",
        *classification_rubric("the title/summary", country_code),
        "",
        f"LOCATION — where the project or news is, relative to {country_name}. Exactly one of: in_country, abroad, unclear.",
        f"  in_country = the project/news is in {country_name} — a named city, town, province, or region of {country_name} counts.",
        f"  abroad = clearly outside {country_name}: a project in another country (even when the company, investor, or news outlet is from {country_name}), another country's politics, war, or economy, or a foreign website's general content.",
        "  unclear = no place is named and nothing else in the title/summary tells you where it is.",
        "",
        "OUTPUT — raw JSON array ONLY, no preamble, no markdown. Return exactly one entry per article — every article in the input must appear. For each entry, copy the index AND the exact URL from the article's line so every entry can be matched back unambiguously:",
        f'[{{"index": 7, "url": "https://exact-url-from-that-article-line", "priority": "High", "sector": "{profile.sector_names[0]}", "region": {example_region}, "location": "in_country"}}]',
    ])


def triage_articles(
    news_items: list[NewsItem],
    country_code: str,
    cfg: AppConfig,
    llm: LLMClient,
) -> list[NewsItem]:
    """
    Tag every candidate article in place with sector/priority/region/location.

    Args:
        news_items:   Candidate articles for this country (post-dedup).
        country_code: Used for prompt context and region vocabulary lookup.
        cfg:          Loaded ``AppConfig``.
        llm:          Initialised ``LLMClient``.

    Returns:
        The same ``NewsItem`` list, mutated in place. Items the LLM response
        doesn't resolve to (malformed entry, dropped row) default to
        ``priority="Unknown"`` rather than being silently left untagged, so a
        triage parsing gap is visible/auditable rather than indistinguishable
        from "not yet triaged".
    """
    if not news_items:
        return []

    country_regions = get_country_regions(country_code)
    sectors         = valid_sectors()
    articles_block  = "\n".join(item.to_scoring_prompt_line(i + 1) for i, item in enumerate(news_items))
    messages = [
        {"role": "system", "content": _make_triage_system_prompt(country_code)},
        {"role": "user",   "content": f"Country: {country_code}\n\n{articles_block}"},
    ]

    raw  = llm.triage_articles(messages)
    data = llm.parse_json_response(raw)

    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                data = value
                break

    if not isinstance(data, list):
        logger.warning("Triage response was not a JSON array for %s — leaving all candidates untagged.", country_code)
        for item in news_items:
            item.priority = "Unknown"
        return news_items

    by_url = {item.url: item for item in news_items if item.url}
    tagged: set[str] = set()

    for entry in data:
        item = _resolve_entry(entry, news_items, by_url, country_code)
        if item is None:
            continue
        item.priority = clean_priority(entry.get("priority"))
        item.sector   = clean_enum(entry.get("sector"), sectors)
        item.region   = clean_enum(entry.get("region"), country_regions)
        item.location = _clean_location(entry.get("location"))
        if item.location == "unclear":
            item.priority = "Low"   # business rule: location missing/unclear → demote, never drop
        tagged.add(item.url)

    untagged = [item for item in news_items if item.url not in tagged]
    if untagged:
        logger.warning("Triage left %d/%d articles untagged for %s — defaulting to Unknown priority.", len(untagged), len(news_items), country_code)
        for item in untagged:
            item.priority = "Unknown"

    logger.info(
        "Triaged %d articles for %s: %d High, %d Medium, %d Low, %d Unknown | location: %d abroad, %d unclear",
        len(news_items), country_code,
        sum(1 for i in news_items if i.priority == "High"),
        sum(1 for i in news_items if i.priority == "Medium"),
        sum(1 for i in news_items if i.priority == "Low"),
        sum(1 for i in news_items if i.priority == "Unknown"),
        sum(1 for i in news_items if i.location == "abroad"),
        sum(1 for i in news_items if i.location == "unclear"),
    )
    return news_items


def drop_out_of_country(
    new_items: list[NewsItem],
    candidates: list[NewsItem],
    country_code: str,
) -> tuple[list[NewsItem], list[NewsItem]]:
    """
    Remove articles triage judged to be outside the country (``location ==
    "abroad"``) from both the to-be-persisted list and the picker candidates,
    so foreign news is never stored, picked, or emailed.

    Only an explicit "abroad" verdict drops an article: "unclear" ones stay
    (already demoted to Low by ``triage_articles``), and untriaged ones (e.g.
    after a triage failure) are kept rather than lost.
    """
    abroad_urls = {item.url for item in candidates if item.location == "abroad"}
    if not abroad_urls:
        return new_items, candidates
    examples = [item.title[:60] for item in candidates if item.url in abroad_urls][:3]
    logger.info(
        "Dropped %d out-of-country articles for %s — e.g. %s",
        len(abroad_urls), country_code, " | ".join(examples),
    )
    return (
        [item for item in new_items  if item.url not in abroad_urls],
        [item for item in candidates if item.url not in abroad_urls],
    )


def _resolve_entry(
    entry: dict,
    news_items: list[NewsItem],
    by_url: dict[str, NewsItem],
    country_code: str,
) -> NewsItem | None:
    """Same URL-primary/index-fallback resolution as the picker — see opportunity_picker._resolve_pick."""
    url = (entry.get("url") or "").strip()
    by_url_match = by_url.get(url) if url else None

    idx = entry.get("index")
    by_index_match = None
    if isinstance(idx, (int, float)):
        i = int(idx) - 1
        if 0 <= i < len(news_items):
            by_index_match = news_items[i]

    if by_url_match is not None:
        if by_index_match is not None and by_index_match is not by_url_match:
            logger.warning("Triage index/url mismatch for %s (index=%s) — trusting the URL match.", country_code, idx)
        return by_url_match

    return by_index_match


def _clean_location(value: object) -> str:
    """Normalise the triage LOCATION verdict; anything but in_country/abroad (incl. missing) → 'unclear'."""
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text if text in ("in_country", "abroad") else "unclear"


def clean_priority(value: object) -> str:
    """Validate/normalise a priority value against VALID_PRIORITIES, case-insensitively. Defaults to 'Unknown'."""
    text = str(value or "").strip()
    matched = next((p for p in VALID_PRIORITIES if p.lower() == text.lower()), None)
    return matched or "Unknown"


# Known local-language aliases for region values the LLM may return despite prompt instructions.
# Only applied when the resolved canonical form is actually in the valid tuple — safe globally.
_REGION_ALIASES: dict[str, str] = {
    # Belgian regions — canonical names are English; LLM may use Dutch or French forms
    "vlaanderen":              "Flanders",
    "flandre":                 "Flanders",
    "flandres":                "Flanders",
    "wallonie":                "Wallonia",
    "bruxelles-capitale":      "Brussels-Capital",
    "bruxelles capitale":      "Brussels-Capital",
    "brussels capital":        "Brussels-Capital",
    "brussels capital region": "Brussels-Capital",
}


def clean_enum(value: object, valid: tuple[str, ...]) -> str | None:
    """Validate/normalise a value against a closed vocabulary.

    Applies unidecode to the input so diacritics in LLM output map cleanly
    to the ASCII canonical form. Checks explicit aliases before falling back
    to fuzzy match (cutoff 0.9) for near-misses.
    """
    from difflib import get_close_matches

    from unidecode import unidecode
    text = unidecode(str(value or "").strip()).lower()
    if not text or text in ("null", "none"):
        return None
    valid_lower = [v.lower() for v in valid]
    if text in valid_lower:
        return valid[valid_lower.index(text)]
    alias_target = _REGION_ALIASES.get(text)
    if alias_target and alias_target in valid:
        return alias_target
    hits = get_close_matches(text, valid_lower, n=1, cutoff=0.9)
    return valid[valid_lower.index(hits[0])] if hits else None
