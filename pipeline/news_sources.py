"""
News collection — Stage 1 of the LeadRadar pipeline.

For one country, builds a set of Google News RSS searches plus curated
trade-press feeds, fetches them, and returns a URL-deduplicated list of
``NewsItem`` objects. Three kinds of source:

A. **English project themes** — generic "something is about to be built or
   bought" searches (construction, infrastructure, tenders, energy, data
   centres…) scoped to the country, plus the active business profile's
   ``extra_themes_en``.
B. **Local-language themes** — the same idea in the country's own
   language(s), since most early-stage project news is only published
   locally. The profile's ``extra_local_themes`` are appended per language.
C. **Direct feeds** — hand-picked trade publications and official
   procurement / environmental-permit feeds.

The public entry point is ``collect_country_news()``.
"""
from __future__ import annotations

import logging
import time
import urllib.parse

from core.business_profile import get_profile
from core.config import AppConfig
from core.feed_reader import fetch_rss_feed
from core.news_models import NewsItem

from pipeline.country_config import country_iso2, get_country_config, is_country_configured

logger = logging.getLogger(__name__)

_MAX_ITEMS_PER_FEED = 8

# ── A. English project themes (country-agnostic) ─────────────────────────────
# "+" is the Google News AND operator. Deliberately about *projects*, not about
# any particular product: the business profile adds industry-specific themes.

THEMES_EN: list[str] = [
    "construction+infrastructure+project",
    "housing+real+estate+residential",
    "highway+road+rail+infrastructure",
    "renewable+energy+solar+wind+nuclear",
    "data+center+logistics+warehouse",
    "defense+military+infrastructure",
    "tender+public+investment+government",
    "hospital+school+public+building",
]

# ── B. Local-language themes ─────────────────────────────────────────────────
# country_code → list of (theme, local country term, language code).

LOCAL_THEMES_BY_COUNTRY: dict[str, list[tuple[str, str, str]]] = {
    "NETHERLANDS": [
        ("bouw+bouwprojecten+bouwnijverheid",                          "Nederland", "nl"),
        ("infrastructuur+infrastructuurprojecten",                     "Nederland", "nl"),
        ("scholen+schoolgebouwen+onderwijsinfrastructuur",             "Nederland", "nl"),
        ("ziekenhuisbouw+gezondheidszorggebouw+zorginfrastructuur",    "Nederland", "nl"),
        ("logistiek+magazijnen+distributiecentra",                     "Nederland", "nl"),
        ("datacenters+datacenterbouw",                                 "Nederland", "nl"),
        ("defensie+kazernes+defensieprojecten",                        "Nederland", "nl"),
        ("aanbestedingen+overheidsopdrachten+overheidsinvesteringen",  "Nederland", "nl"),
        ("energietransitie+zonne-energie+windenergie",                 "Nederland", "nl"),
        ("woningtekort+vastgoedontwikkeling+huisvesting",              "Nederland", "nl"),
        ("haven+project+investering",                                  "Nederland", "nl"),
    ],
    "BELGIUM": [
        # Dutch (Flanders)
        ("bouw+bouwprojecten+bouwnijverheid",                          "België",   "nl"),
        ("infrastructuur+infrastructuurprojecten",                     "België",   "nl"),
        ("scholen+schoolgebouwen+onderwijsinfrastructuur",             "België",   "nl"),
        ("ziekenhuisbouw+gezondheidszorggebouw+zorginfrastructuur",    "België",   "nl"),
        ("logistiek+magazijnen+distributiecentra",                     "België",   "nl"),
        ("datacenters+datacenterbouw",                                 "België",   "nl"),
        ("defensie+kazernes+defensieprojecten",                        "België",   "nl"),
        ("overheidsopdrachten+aanbestedingen+overheidsinvesteringen",  "België",   "nl"),
        ("energietransitie+zonne-energie+windenergie",                 "België",   "nl"),
        ("woningnood+vastgoedontwikkeling+woningbouw",                 "België",   "nl"),
        # French (Wallonia / Brussels)
        ("construction+projets+bâtiment",                              "Belgique", "fr"),
        ("infrastructure+publique+projets",                            "Belgique", "fr"),
        ("écoles+scolaires+éducation",                                 "Belgique", "fr"),
        ("construction+hôpital+infrastructure+santé",                  "Belgique", "fr"),
        ("logistique+entrepôts+distribution",                          "Belgique", "fr"),
        ("data+centers+données",                                       "Belgique", "fr"),
        ("défense+casernes+militaire",                                 "Belgique", "fr"),
        ("marchés+publics+investissements",                            "Belgique", "fr"),
        ("transition+énergétique+renouvelable",                        "Belgique", "fr"),
        ("logement+immobilier+pénurie",                                "Belgique", "fr"),
    ],
    "SPAIN": [
        ("construcción+infraestructura",                "España", "es"),
        ("licitación+obras",                            "España", "es"),
        ("obras+adjudicación",                          "España", "es"),
        ("promoción+vivienda+obra+nueva+construcción",  "España", "es"),
        ("hospital+centro+de+salud+obras",              "España", "es"),
        ("nueva+planta+fábrica+inversión",              "España", "es"),
        ("centro+de+datos+construcción",                "España", "es"),
        ("parque+eólico+construcción",                  "España", "es"),
        ("hidrógeno+verde+planta",                      "España", "es"),
        ("residencia+estudiantes+mayores+construcción", "España", "es"),
    ],
    "ITALY": [
        ("costruzione+infrastruttura",                   "Italia", "it"),
        ("appalto+investimento+pubblico",                "Italia", "it"),
        ("edilizia+immobiliare",                         "Italia", "it"),
        ("cantiere+opere+infrastrutturali",              "Italia", "it"),
        ("difesa+infrastrutture+militare",               "Italia", "it"),
        ("ospedale+scuola+edilizia+pubblica",            "Italia", "it"),
        ("logistica+magazzini+distribuzione",            "Italia", "it"),
        ("energia+rinnovabile+fotovoltaico+cantiere",    "Italia", "it"),
    ],
    "POLAND": [
        ("budowa+infrastruktura",         "Polska", "pl"),
        ("przetarg+inwestycja+publiczna", "Polska", "pl"),
        ("mieszkalnictwo+nieruchomości",  "Polska", "pl"),
    ],
}

# ── C. Direct feeds ──────────────────────────────────────────────────────────
# "native": True marks a publisher's own RSS feed (not Google News) — its titles
# are kept intact instead of being split on " - " like Google News
# "Headline - Outlet" titles. "outlet" is the readable source name shown on
# cards for native feeds (Google News items already carry the publisher name).

DIRECT_FEEDS_BY_COUNTRY: dict[str, list[dict]] = {
    "POLAND": [
        {"name": "TheFirstNews",      "url": "https://news.google.com/rss/search?q=site:thefirstnews.com+construction+OR+infrastructure+OR+investment+when:2d&hl=en"},
    ],
    "NETHERLANDS": [
        {"name": "DutchNews",         "url": "https://news.google.com/rss/search?q=site:dutchnews.nl+construction+OR+housing+OR+infrastructure+when:2d&hl=en"},
        {"name": "Cobouw",            "url": "https://news.google.com/rss/search?q=site:cobouw.nl+when:3d&hl=nl&gl=NL&ceid=NL:nl"},
        {"name": "FinancieelDagblad", "url": "https://news.google.com/rss/search?q=site:fd.nl+bouw+OR+infrastructuur+OR+investering+when:3d&hl=nl&gl=NL&ceid=NL:nl"},
    ],
    "BELGIUM": [
        {"name": "Bouwkroniek",       "url": "https://news.google.com/rss/search?q=site:bouwkroniek.be+when:3d&hl=nl&gl=BE&ceid=BE:nl"},
        {"name": "BatiChronique",     "url": "https://news.google.com/rss/search?q=site:batichronique.be+when:3d&hl=fr&gl=BE&ceid=BE:fr"},
    ],
    "ITALY": [
        {"name": "IlSole24Ore",       "url": "https://news.google.com/rss/search?q=site:ilsole24ore.com+costruzione+OR+appalto+OR+infrastruttura+when:3d&hl=it&gl=IT&ceid=IT:it"},
        {"name": "Edilportale",       "url": "https://news.google.com/rss/search?q=site:edilportale.com+when:3d&hl=it&gl=IT&ceid=IT:it"},
    ],
    "SPAIN": [
        # Publishers' own feeds + the official state gazette (public-works tenders, environmental permits)
        {"name": "PVMagazine-ES",         "native": True, "outlet": "pv magazine España",         "url": "https://www.pv-magazine.es/feed/"},
        {"name": "DataCenterMarket-ES",   "native": True, "outlet": "Data Center Market",         "url": "https://www.datacentermarket.es/feed/"},
        {"name": "DCD-ES",                "native": True, "outlet": "DatacenterDynamics",         "url": "https://www.datacenterdynamics.com/es/rss/"},
        {"name": "BOE-ObrasLicitacion",   "native": True, "outlet": "BOE — Licitación de obras",  "url": "https://www.boe.es/rss/canal_cpv.php?l=a&c=450"},
        {"name": "BOE-ImpactoAmbiental",  "native": True, "outlet": "BOE — Evaluación ambiental", "url": "https://www.boe.es/rss/canal.php?c=imp_amb"},
        # Google News site: searches, narrowed to project/works vocabulary
        {"name": "ElPeriodicoEnergia-ES", "url": "https://news.google.com/rss/search?q=site:elperiodicodelaenergia.com+planta+OR+parque+OR+proyecto+OR+obras+OR+BESS+OR+autoriza+when:3d&hl=es&gl=ES&ceid=ES:es"},
        {"name": "EnergiasRenovables-ES", "url": "https://news.google.com/rss/search?q=site:energias-renovables.com+when:3d&hl=es&gl=ES&ceid=ES:es"},
        {"name": "EjePrime-ES",           "url": "https://news.google.com/rss/search?q=site:ejeprime.com+when:3d&hl=es&gl=ES&ceid=ES:es"},
        {"name": "Alimarket-ES",          "url": "https://news.google.com/rss/search?q=site:alimarket.es+construcci%C3%B3n+OR+obras+OR+%22nueva+planta%22+OR+ampliaci%C3%B3n+OR+%22centro+log%C3%ADstico%22+OR+nave+when:3d&hl=es&gl=ES&ceid=ES:es"},
        {"name": "Interempresas-ES",      "url": "https://news.google.com/rss/search?q=site:interempresas.net+%22nueva+planta%22+OR+%22nueva+f%C3%A1brica%22+OR+%22nuevo+centro%22+OR+ampliaci%C3%B3n+OR+inversi%C3%B3n+millones+when:3d&hl=es&gl=ES&ceid=ES:es"},
        {"name": "elEconomista-ES",       "url": "https://news.google.com/rss/search?q=site:eleconomista.es+obras+OR+adjudica+OR+licita+OR+%22nueva+planta%22+OR+construir%C3%A1+-ranking+when:3d&hl=es&gl=ES&ceid=ES:es"},
        # Topic searches for public infrastructure operators and project types
        {"name": "ES-Infraestructuras",   "url": "https://news.google.com/rss/search?q=Adif+OR+Aena+licita+OR+adjudica+OR+obras+when:3d&hl=es&gl=ES&ceid=ES:es"},
        {"name": "ES-Agua",               "url": "https://news.google.com/rss/search?q=desaladora+OR+depuradora+obras+OR+licitaci%C3%B3n+OR+adjudica+when:3d&hl=es&gl=ES&ceid=ES:es"},
        {"name": "ES-Logistica",          "url": "https://news.google.com/rss/search?q=%22plataforma+log%C3%ADstica%22+OR+%22centro+log%C3%ADstico%22+OR+%22nave+log%C3%ADstica%22+construcci%C3%B3n+OR+obras+when:3d&hl=es&gl=ES&ceid=ES:es"},
        {"name": "ES-Defensa",            "url": "https://news.google.com/rss/search?q=cuartel+OR+%22base+militar%22+OR+Defensa+obras+OR+infraestructura+Espa%C3%B1a+when:3d&hl=es&gl=ES&ceid=ES:es"},
    ],
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def build_gnews_url(theme: str, country_term: str, lang: str, gl: str, freshness: str = "2d") -> str:
    """
    Build a Google News RSS search URL for one country + theme combination.

    The query is ``"<theme> <country_term>"`` percent-encoded like JavaScript's
    ``encodeURIComponent`` (space→%20, +→%2B), with ``+when:<freshness>``
    appended *unencoded* so Google treats it as the recency operator.

    Args:
        theme:        Search theme with ``+`` as AND operator (e.g. ``"construction+infrastructure"``).
        country_term: Country name in English or the local language (e.g. ``"España"``).
        lang:         Language code for ``hl`` and ``ceid`` (e.g. ``"es"``).
        gl:           Country code for ``gl`` and ``ceid`` (e.g. ``"ES"``).
        freshness:    Google News ``when:`` window, e.g. ``"2d"``.
    """
    q = urllib.parse.quote(f"{theme} {country_term}", safe="") + f"+when:{freshness}"
    return f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={gl}&ceid={gl}:{lang}"


def _deduplicate_by_url(items: list[NewsItem]) -> list[NewsItem]:
    """Remove duplicate items by exact URL, keeping the first occurrence."""
    seen: set[str] = set()
    unique: list[NewsItem] = []
    for item in items:
        if item.url and item.url not in seen:
            seen.add(item.url)
            unique.append(item)
    return unique


def build_search_plan(country_code: str) -> list[tuple[str, str, str]]:
    """
    All Google News searches (themes A + B, including profile extras) for a
    country as ``(theme, country_term, lang)`` triples. Split out so the plan
    can be inspected/tested without any network access.
    """
    country = get_country_config(country_code)
    profile = get_profile()
    local   = LOCAL_THEMES_BY_COUNTRY.get(country_code, [])

    plan: list[tuple[str, str, str]] = [
        (theme, country["news_term_en"], "en")
        for theme in (*THEMES_EN, *profile.extra_themes_en)
    ]
    plan += local

    # Profile extras in every (local term, language) pair this country already uses
    for term, lang in dict.fromkeys((term, lang) for _, term, lang in local):
        plan += [(theme, term, lang) for theme in profile.extra_local_themes.get(lang, ())]
    return plan


# ── Public entry point ───────────────────────────────────────────────────────

def collect_country_news(country_code: str, cfg: AppConfig, lookback_days: int = 2) -> list[NewsItem]:
    """
    Collect fresh news for a single country.

    Args:
        country_code:  A configured country code (see ``pipeline.country_config``).
        cfg:           Loaded ``AppConfig`` — supplies the RSS politeness delay.
        lookback_days: Google News ``when:Xd`` window. A "not yet started"
                       project announcement is still actionable days later,
                       and dedup against the database keeps re-collection cheap.

    Returns:
        URL-deduplicated list of ``NewsItem`` objects, each tagged with ``country_code``.

    Raises:
        ValueError: If ``country_code`` is not configured.
    """
    if not is_country_configured(country_code):
        raise ValueError(f"Unknown country code: {country_code!r}. Add it to pipeline/country_config.py first.")

    gl        = country_iso2(country_code)
    freshness = f"{lookback_days}d"
    all_items: list[NewsItem] = []

    for theme, term, lang in build_search_plan(country_code):
        url = build_gnews_url(theme, term, lang, gl, freshness=freshness)
        all_items.extend(fetch_rss_feed(url, country_code, max_items=_MAX_ITEMS_PER_FEED))
        time.sleep(cfg.rss_sleep_s)

    for feed in DIRECT_FEEDS_BY_COUNTRY.get(country_code, []):
        all_items.extend(fetch_rss_feed(
            feed["url"], feed.get("outlet", feed["name"]),
            max_items=_MAX_ITEMS_PER_FEED,
            split_gnews_title=not feed.get("native"),
        ))
        time.sleep(cfg.rss_sleep_s)

    for item in all_items:
        item.country = country_code

    unique = _deduplicate_by_url(all_items)
    logger.info(
        "collect_country_news(%s): %d raw → %d unique by URL (lookback=%dd)",
        country_code, len(all_items), len(unique), lookback_days,
    )
    return unique
