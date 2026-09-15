"""
Opportunity enricher — Stage 4 of the LeadRadar pipeline.

Scrapes the full article page for each picked opportunity (``article_scraper``)
and sends each one to the LLM in its own call, asking it to extract the
structured details a sales rep needs: project phase, company, city, project
type/value, and the "why they need us" angle defined by the business profile.
One call per article (rather than batching all picks into one prompt) keeps
grounding accurate — no risk of the model mixing up companies or cities between
articles — and means one bad extraction doesn't lose the rest of the batch. At
3-5 articles/day/country the extra calls cost nothing meaningful.

Extraction is grounded strictly in the provided text — never invented. The
original RSS summary is always included alongside the scraped text (not just
as a fallback when scraping fails) since it sometimes states a figure or fact
more crisply than the full article body.
"""
from __future__ import annotations

import logging
import time

from core.business_profile import get_profile
from core.config import AppConfig
from core.llm_client import LLMClient
from core.news_models import EnrichedOpportunity, NewsItem

from pipeline.article_scraper import scrape_article
from pipeline.country_config import (
    country_display_name, get_country_fallback_strings,
    get_country_output_language, get_country_regions,
)
from pipeline.opportunity_triage import classification_rubric, clean_enum, clean_priority, valid_sectors

logger = logging.getLogger(__name__)

VALID_END_USAGES = (
    "Healthcare", "Education", "Public Administration", "Defense",
    "Residential/Housing", "Commercial", "Mining", "Conventional Energy",
    "Sustainable Energy", "Industry & Services", "Infrastructure",
    "Events", "Data center", "Others",
)

_SCRAPE_TIMEOUT_S = 20   # generous — only 3-5 calls/day, speed is not a constraint
_VALID_PHASES = {"announced", "planning", "permitting", "tender", "awarded", "construction", "completed", "unknown"}


def _make_enricher_system_prompt(country_code: str) -> str:
    """Build the enricher system prompt for a specific country."""
    profile      = get_profile()
    lang         = get_country_output_language(country_code)
    country_name = country_display_name(country_code)
    products     = "; ".join(profile.products)
    return "\n".join([
        f"{profile.analyst_persona}. You are extracting structured details from ONE project news article for the {country_name} market sales team.",
        "Return a single JSON object ONLY. No preamble, no markdown.",
        f"LANGUAGE: Write product_fit, why_it_matters, and recommended_action in {lang}. Write english_summary in English. Company names and city names should match how they appear in the source text. phase, sector, and priority are always in English regardless of the article's language. region must be copied exactly from its list of valid values below (never translated).",
        "",
        "You will be given the FULL ARTICLE TEXT (when scraping succeeded) and the original RSS SUMMARY for the same story. Use both — the summary sometimes states a figure or fact more crisply than the full body. If they conflict, prefer the full article text.",
        "",
        "You will also be shown the CURRENT CLASSIFICATION — a sector/priority/region already assigned by an earlier triage pass and rechecked once by the picker. Don't take it at face value — independently apply the classification rubric below to the full article text you now have, which is more grounding than either earlier pass had. Only repeat the current classification if applying the rubric yourself actually reaches the same answer; correct it whenever your own reading disagrees, even slightly.",
        "",
        "GROUNDING RULES:",
        "- For factual fields (company, city, project_type, project_value): use ONLY facts stated in the text. Never invent or estimate. If a field is not stated anywhere, use null.",
        "- project_value: copy the figure and currency EXACTLY as written in the source (e.g. '120 mln zł', 'EUR 45 million'). Never translate, convert currencies, or round numbers.",
        f"- For sales fields (product_fit, why_it_matters, recommended_action): these are your commercial analysis, not quotations. You MUST always fill them in — use your knowledge of {profile.company_name}'s business to reason about why the project creates demand for its products, even when the article doesn't mention them explicitly.",
        "",
        "FIELDS:",
        "phase — always in English, exactly one of: Announced, Planning, Permitting, Tender, Awarded, Construction, Completed, Unknown.",
        "  Announced = idea/concept stage. Planning = design/feasibility. Permitting = zoning/environmental review in progress.",
        "  Tender = procurement/bidding open. Awarded = contract signed, ground not yet broken. Construction = building has started. Completed = finished. Unknown = cannot tell from the text.",
        "company — the developer, contractor, or main stakeholder named in the text, or null. Extract it whenever stated, even if it is not a company you recognise.",
        "city — the specific city, town, or municipality where the project is located, or null. ALWAYS check the article title first — location names in titles are valid sources. Do NOT use administrative region names here (that level of geography belongs in the 'region' field). Use null only when no specific municipality is identifiable anywhere in the title, article body, or RSS summary.",
        "project_type — short phrase describing what is being built or bought (e.g. 'logistics warehouse', 'housing estate', 'highway extension'), or null.",
        "project_value — value EXACTLY as stated in the source text (e.g. '120 mln zł', 'EUR 45 million'), or null.",
        f"english_summary — 2-3 sentences in English: what the project is, its current stage, and why it is commercially relevant for {profile.company_name}.",
        f"product_fit — one sentence IN {lang.upper()}: {profile.fit_definition} (our offer: {products}). If the text mentions specific technical, certification, or quality requirements, name them. This is a required commercial inference — always provide it.",
        f"why_it_matters — 1-2 sentences IN {lang.upper()} for a sales rep: what makes this worth a call now, referencing the project stage and any time-sensitive element (open tender, recent award, imminent ground-breaking). Required — always provide.",
        f"recommended_action — one concrete next step IN {lang.upper()} naming the company/entity if known, or the procurement channel if not. Required — always provide.",
        "expansion_signal — ONLY when phase=Construction: if the text explicitly describes a sudden expansion or an additional/subcontractor-driven need beyond what's already underway (e.g. a new contractor joining, a scaled-up scope), give a short phrase naming that need. Otherwise null — including whenever phase is not Construction.",
        "end_usage — what the completed project will primarily be used for. Exactly one of:",
        "  Healthcare = hospitals, clinics, medical centers, care homes, emergency facilities.",
        "  Education = schools, universities, training centers, kindergartens, libraries.",
        "  Public Administration = government offices, courts, police stations, municipal buildings, post offices.",
        "  Defense = military bases, barracks, defense contractors' facilities, security installations.",
        "  Residential/Housing = apartment buildings, multi-family housing estates, student residences, senior living (not single-family).",
        "  Commercial = shopping centers, retail parks, office buildings, hotels, restaurants, logistics/warehouses serving retail.",
        "  Mining = mines, quarries, extraction sites, mineral processing plants.",
        "  Conventional Energy = coal/gas/nuclear power plants, oil refineries, fuel depots, traditional energy infrastructure.",
        "  Sustainable Energy = wind farms, solar parks, green hydrogen plants, biogas, EV charging networks, hydropower.",
        "  Industry & Services = mixed industrial/service facilities, factories with integrated office or service components, industrial parks.",
        "  Infrastructure = roads, railways, bridges, airports, ports, water/sewage treatment, telecoms, utility networks.",
        "  Events = stadiums, arenas, exhibition centers, permanent or semi-permanent event venues.",
        "  Data center = server farms, colocation/data hosting facilities, cloud infrastructure campuses.",
        "  Others = does not clearly fit any category above.",
        "  null = end use is not stated or cannot be inferred from the text.",
        "priority, sector, region — your own re-checked classification (see rubric below), not a copy of CURRENT CLASSIFICATION:",
        *classification_rubric("the article text", country_code),
        "",
        f'OUTPUT: {{"phase":"Tender","company":"...","city":"...","project_type":"...","project_value":"...","english_summary":"...","product_fit":"...","why_it_matters":"...","recommended_action":"...","end_usage":"Infrastructure","expansion_signal":null,"priority":"High","sector":"{profile.sector_names[0]}","region":"..."}}',
    ])


def enrich_single_article(
    item: NewsItem,
    llm: LLMClient,
    country_code: str,
) -> EnrichedOpportunity:
    """
    Enrich a single article via scrape + LLM extraction.

    Public entry point used by the web app for on-demand enrichment. Identical
    to what the daily pipeline does per article, but callable for any article
    the user selects — not just the top 3-5 auto-picks.

    Raises:
        Exception: Any scrape or LLM failure propagates to the caller.
    """
    result = scrape_article(item.url, timeout_s=_SCRAPE_TIMEOUT_S)
    logger.info("Scraped '%s' via %s (%d chars)", item.url, result.method, result.char_count)
    entry = _extract_one(item, result, llm, country_code)
    return _build_enriched_opportunity(item, result, entry, country_code)


def enrich_opportunities(
    picks: list[NewsItem],
    country_code: str,
    cfg: AppConfig,
    llm: LLMClient,
) -> list[EnrichedOpportunity]:
    """
    Scrape and enrich a best-first list of picked opportunities, one LLM call
    per article.

    Args:
        picks:        Best-first ``NewsItem`` list from ``opportunity_picker.pick_opportunities()``.
        country_code: Carried into each ``EnrichedOpportunity``.
        cfg:          Loaded ``AppConfig``.
        llm:          Initialised ``LLMClient``.

    Returns:
        ``EnrichedOpportunity`` list in the same order as ``picks``. If
        extraction fails for one article (bad JSON, LLM error), that
        opportunity is skipped and a warning is logged — the rest of the
        batch is unaffected.
    """
    enriched: list[EnrichedOpportunity] = []
    for i, item in enumerate(picks):
        if i > 0:
            time.sleep(1.5)   # spread out Google News decode requests — avoids tripping its rate limit

        result = scrape_article(item.url, timeout_s=_SCRAPE_TIMEOUT_S)
        logger.info("Scraped '%s' via %s (%d chars)", item.url, result.method, result.char_count)

        try:
            entry = _extract_one(item, result, llm, country_code)
        except Exception as exc:
            logger.warning("Enrichment failed for '%s' — skipping. Reason: %s", item.url, exc)
            continue

        enriched.append(_build_enriched_opportunity(item, result, entry, country_code))

    return enriched


def _extract_one(item: NewsItem, result, llm: LLMClient, country_code: str) -> dict:
    summary_block = f"RSS SUMMARY:\n{item.summary}" if item.summary else "RSS SUMMARY: (none provided)"

    if result.success:
        body = f"FULL ARTICLE TEXT:\n{result.text}\n\n{summary_block}"
    else:
        body = f"(Full article could not be scraped — RSS summary is the only available text.)\n\n{summary_block}"

    current_classification = (
        f"CURRENT CLASSIFICATION: priority={item.priority or 'Unknown'}, "
        f"sector={item.sector or 'null'}, region={item.region or 'null'}"
    )
    user_content = f"Title: {item.title}\nSource: {item.source}\n{current_classification}\n\n{body}"
    messages = [
        {"role": "system", "content": _make_enricher_system_prompt(country_code)},
        {"role": "user",   "content": user_content},
    ]
    raw  = llm.extract_opportunity_details(messages)
    data = llm.parse_json_response(raw)

    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object from the enricher, got: {type(data).__name__}.")
    return data


def _build_enriched_opportunity(item: NewsItem, result, entry: dict, country_code: str) -> EnrichedOpportunity:
    def _nullable(v: object) -> str | None:
        return v if v and v not in ("null", "") else None  # type: ignore[return-value]

    phase = str(entry.get("phase", "Unknown")).strip()
    if phase.lower() not in _VALID_PHASES:
        phase = "Unknown"

    priority = clean_priority(entry.get("priority")) if entry.get("priority") is not None else None
    if not priority or priority == "Unknown":
        priority = item.priority   # enricher had no opinion — keep the picker's rechecked tag
    elif item.priority and item.priority != priority:
        logger.info("Enricher corrected priority for '%s': %s → %s", item.title[:60], item.priority, priority)

    fallbacks       = get_country_fallback_strings(country_code)
    country_regions = get_country_regions(country_code)
    sector = clean_enum(entry.get("sector"), valid_sectors()) or item.sector
    region = clean_enum(entry.get("region"), country_regions) or item.region

    # Narrow exception to "Construction phase = too late": only meaningful
    # when phase is actually Construction — ignore it otherwise even if the
    # LLM mistakenly populated it (e.g. for a Tender-phase article).
    expansion_signal = _nullable(entry.get("expansion_signal")) if phase.lower() == "construction" else None

    return EnrichedOpportunity(
        title               = item.title,
        url                 = result.resolved_url or item.url,
        source_url          = item.url,
        source              = item.source,
        country             = country_code,
        published_date      = item.date,
        phase               = phase,
        company             = _nullable(entry.get("company")),
        city                = _nullable(entry.get("city")),
        project_type        = _nullable(entry.get("project_type")),
        project_value       = _nullable(entry.get("project_value")),
        english_summary     = _nullable(entry.get("english_summary")),
        product_fit         = entry.get("product_fit") or fallbacks["product_fit"],
        why_it_matters      = entry.get("why_it_matters") or "",
        recommended_action  = entry.get("recommended_action") or fallbacks["recommended_action"],
        scrape_method       = result.method,
        sector              = sector,
        priority            = priority,
        region              = region,
        expansion_signal    = expansion_signal,
        scraped_text        = result.text if result.success else None,
        end_usage           = clean_enum(entry.get("end_usage"), VALID_END_USAGES),
    )
