"""
Opportunity picker — Stage 3 of the LeadRadar pipeline.

Sends the country's triaged headlines+summaries to the LLM and asks it to pick
only the opportunities that are (a) relevant to the business, per the active
business profile, (b) still early enough that a sales rep has time to act —
i.e. not yet under construction — and (c) physically in the country. This
headline-only pass decides *which* articles are worth the cost of a full
scrape; the enrichment stage (``opportunity_enricher``) then extracts details
from the full text of just the picks.
"""
from __future__ import annotations

import json
import logging

from core.business_profile import get_profile
from core.config import AppConfig
from core.llm_client import LLMClient
from core.news_models import NewsItem

from pipeline.country_config import country_display_name, get_country_regions
from pipeline.opportunity_triage import classification_rubric, clean_enum, clean_priority, valid_sectors

logger = logging.getLogger(__name__)

_MIN_PICKS = 3
_MAX_PICKS = 5


def _make_picker_system_prompt(country_code: str) -> str:
    """Build the picker system prompt for a specific country."""
    profile        = get_profile()
    country_name   = country_display_name(country_code)
    regions        = get_country_regions(country_code)
    example_region = json.dumps(regions[0] if regions else None)

    quality = [f"- {signal}" for signal in profile.quality_signals]
    if profile.higher_margin_sectors:
        quality.insert(0, f"- Sector: {', '.join(profile.higher_margin_sectors)} are higher-margin sectors and outrank the others.")

    return "\n".join([
        f"{profile.analyst_persona}. You are evaluating opportunities for the {country_name} market.",
        f"From the numbered list of {country_name} articles, pick {_MIN_PICKS}–{_MAX_PICKS} opportunities when the batch supports it. Return fewer only when genuinely few articles satisfy all the hard requirements below — do not pad with weak picks.",
        "",
        "Some article lines already carry SECTOR/PRIORITY/REGION tags from an earlier triage pass. Don't take those tags at face value — independently apply the classification rubric below to the title and summary on the same line, and for every article you pick, return YOUR OWN conclusion. Only repeat the existing tag if applying the rubric yourself actually reaches the same answer; do not copy it forward by default. You have the exact same information the triage step had, so a second independent read is only useful if you genuinely re-derive each tag from the rubric rather than trusting the earlier pass.",
        "",
        *classification_rubric("the title/summary", country_code),
        "",
        "A picked opportunity MUST satisfy ALL THREE of these hard requirements:",
        f"1. RELEVANCE — {profile.relevance}",
        "2. TIMING — the project has not yet broken ground OR describes a sudden unplanned expansion of a site already under construction (e.g. a new contractor joining, a scope increase creating additional demand). Acceptable stages: announced, planning, permitting/zoning, tender/bidding open, contract awarded but not started, under construction with an explicit new/additional need.",
        f"3. LOCATION — the project is physically located in {country_name}. Exclude projects abroad, even when the company, investor, or news outlet is from {country_name}. Articles tagged LOCATION:unclear may be picked only if the title/summary gives a concrete indication that the project is in {country_name}.",
        "",
        "QUALITY — among articles that clear all three hard requirements above, rank best-first. None of these is individually required; each one met adds value:",
        *quality,
        "",
        "EXCLUDE:",
        "- Projects nearing completion or already completed — too late for a sales rep to act.",
        "- Projects explicitly under construction with no new or additional need described (the article merely reports progress, not a scope change).",
        "- Pure macro/political/economic news with no specific project.",
        "- Vague mentions with no identifiable project, company, or location.",
        f"- Projects or news located outside {country_name}.",
        "",
        "OUTPUT — raw JSON array ONLY, no preamble, no markdown. Copy the index AND the exact URL from the article's line so the pick can be resolved unambiguously even if the numbering shifts:",
        f'[{{"index": 7, "url": "https://exact-url-from-that-article-line", "reason": "one short sentence on why this is a good, still-actionable opportunity", "priority": "High", "sector": "{profile.sector_names[0]}", "region": {example_region}}}]',
        "Order the array best opportunity first. If nothing qualifies, return [].",
    ])


def pick_opportunities(
    news_items: list[NewsItem],
    country_code: str,
    cfg: AppConfig,
    llm: LLMClient,
) -> list[NewsItem]:
    """
    Select the best still-actionable opportunities for one country.

    Args:
        news_items:   Triaged candidate articles for this country.
        country_code: Used for prompt context and region vocabulary.
        cfg:          Loaded ``AppConfig``.
        llm:          Initialised ``LLMClient``.

    Returns:
        Best-first list of picked ``NewsItem`` objects, capped at
        ``_MAX_PICKS``. May be shorter than ``_MIN_PICKS`` (even empty) if
        genuinely few or no good opportunities exist today.
    """
    if not news_items:
        return []

    country_regions = get_country_regions(country_code)
    articles_block  = "\n".join(item.to_scoring_prompt_line(i + 1) for i, item in enumerate(news_items))
    messages = [
        {"role": "system", "content": _make_picker_system_prompt(country_code)},
        {"role": "user",   "content": f"Country: {country_code}\n\n{articles_block}"},
    ]

    raw  = llm.pick_leads(messages)
    data = llm.parse_json_response(raw)

    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                data = value
                break

    if not isinstance(data, list):
        logger.warning("Picker response was not a JSON array for %s — treating as zero picks.", country_code)
        return []

    by_url = {item.url: item for item in news_items if item.url}

    picks: list[NewsItem] = []
    for entry in data[:_MAX_PICKS]:
        item = _resolve_pick(entry, news_items, by_url, country_code)
        if item is None:
            logger.debug("Picker entry %r could not be resolved to an article for %s — skipping.", entry, country_code)
            continue
        _apply_picker_recheck(item, entry, country_regions)
        picks.append(item)
        logger.info("Pick: '%s' — %s", item.title[:80], entry.get("reason", ""))

    logger.info("Picked %d/%d opportunities for %s (min advisory: %d)", len(picks), len(news_items), country_code, _MIN_PICKS)
    return picks


def _apply_picker_recheck(item: NewsItem, entry: dict, country_regions: tuple[str, ...]) -> None:
    """
    Overwrite ``item``'s triage tags with the picker's own rechecked
    sector/priority/region, logging when the picker disagreed with the
    earlier triage pass — a useful signal for how often triage needs a
    second look. Falls back to the existing triage tag (rather than
    clearing it) if the picker omitted a field or returned something
    unrecognised, since "no opinion" shouldn't erase a known-good tag.
    """
    new_priority = clean_priority(entry.get("priority")) if entry.get("priority") is not None else None
    if new_priority and new_priority != "Unknown":
        if item.priority and item.priority != new_priority:
            logger.info("Picker corrected priority for '%s': %s → %s", item.title[:60], item.priority, new_priority)
        item.priority = new_priority

    new_sector = clean_enum(entry.get("sector"), valid_sectors())
    if new_sector:
        if item.sector and item.sector != new_sector:
            logger.info("Picker corrected sector for '%s': %s → %s", item.title[:60], item.sector, new_sector)
        item.sector = new_sector

    new_region = clean_enum(entry.get("region"), country_regions)
    if new_region:
        if item.region and item.region != new_region:
            logger.info("Picker corrected region for '%s': %s → %s", item.title[:60], item.region, new_region)
        item.region = new_region


def _resolve_pick(
    entry: dict,
    news_items: list[NewsItem],
    by_url: dict[str, NewsItem],
    country_code: str,
) -> NewsItem | None:
    """
    Resolve one picker entry to the actual ``NewsItem`` it refers to.

    The URL is authoritative — it uniquely identifies the article regardless
    of any indexing slip. The index is a fallback only, used when the LLM
    omits or mangles the URL. If both are present but disagree, the URL wins
    and the mismatch is logged so picker reliability can be monitored.
    """
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
            logger.warning(
                "Picker index/url mismatch for %s (index=%s) — trusting the URL match.",
                country_code, idx,
            )
        return by_url_match

    return by_index_match
