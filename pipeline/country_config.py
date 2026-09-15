"""
Country configuration — the single source of truth for which countries
LeadRadar covers.

Each entry holds everything country-specific that is *not* a news query:
display name, ISO code, administrative regions (a closed vocabulary the LLM
must pick from), city→region hints, the language sales text is written in,
and language-specific fallback strings.

Onboarding a new country = one entry here plus (optionally) local-language
themes and trade-press feeds in ``news_sources``.
"""
from __future__ import annotations

_COUNTRY_CONFIG: dict[str, dict] = {
    "POLAND": {
        "name": "Poland",
        "iso2": "PL",
        "news_term_en": "Poland",
        "regions": (
            "Dolnoslaskie", "Kujawsko-Pomorskie", "Lubelskie", "Lubuskie", "Lodzkie",
            "Malopolskie", "Mazowieckie", "Opolskie", "Podkarpackie", "Podlaskie",
            "Pomorskie", "Slaskie", "Swietokrzyskie", "Warminsko-Mazurskie",
            "Wielkopolskie", "Zachodniopomorskie",
        ),
        "region_label": "voivodeship",
        "output_language": "Polish",
        "city_hints": (
            "Warszawa → Mazowieckie, Krakow → Malopolskie, Wroclaw → Dolnoslaskie, "
            "Gdansk → Pomorskie, Katowice → Slaskie, Poznan → Wielkopolskie, "
            "Lodz → Lodzkie, Szczecin → Zachodniopomorskie, Bydgoszcz → Kujawsko-Pomorskie, "
            "Lublin → Lubelskie, Bialystok → Podlaskie, Rzeszow → Podkarpackie, "
            "Olsztyn → Warminsko-Mazurskie, Kielce → Swietokrzyskie, "
            "Opole → Opolskie, Zielona Gora → Lubuskie."
        ),
        "product_fit_fallback": "Nie określono",
        "recommended_action_fallback": "Przejrzyj artykuł źródłowy, aby zaplanować kolejne kroki.",
    },
    "NETHERLANDS": {
        "name": "Netherlands",
        "iso2": "NL",
        "news_term_en": "Netherlands",
        "regions": (
            "Drenthe", "Flevoland", "Friesland", "Gelderland", "Groningen",
            "Limburg", "Noord-Brabant", "Noord-Holland", "Overijssel",
            "Utrecht", "Zeeland", "Zuid-Holland",
        ),
        "region_label": "province",
        "output_language": "English",
        "city_hints": (
            "Amsterdam → Noord-Holland, Rotterdam → Zuid-Holland, "
            "Den Haag → Zuid-Holland, The Hague → Zuid-Holland, "
            "Utrecht → Utrecht, Eindhoven → Noord-Brabant, "
            "Tilburg → Noord-Brabant, Groningen → Groningen, "
            "Almere → Flevoland, Breda → Noord-Brabant, "
            "Nijmegen → Gelderland, Enschede → Overijssel, "
            "Haarlem → Noord-Holland, Arnhem → Gelderland, "
            "Amersfoort → Utrecht, Maastricht → Limburg, "
            "s-Hertogenbosch → Noord-Brabant, Zwolle → Overijssel."
        ),
        "product_fit_fallback": "Not specified",
        "recommended_action_fallback": "Review the source article to plan next steps.",
    },
    "BELGIUM": {
        "name": "Belgium",
        "iso2": "BE",
        "news_term_en": "Belgium",
        "regions": (
            "Flanders", "Wallonia", "Brussels-Capital",
        ),
        "region_label": "region",
        "output_language": "English",
        "city_hints": (
            "Brussels → Brussels-Capital, Bruxelles → Brussels-Capital, "
            "Brussel → Brussels-Capital, Antwerp → Flanders, "
            "Antwerpen → Flanders, Ghent → Flanders, Gent → Flanders, "
            "Bruges → Flanders, Brugge → Flanders, Leuven → Flanders, "
            "Hasselt → Flanders, Mechelen → Flanders, Kortrijk → Flanders, "
            "Liège → Wallonia, Luik → Wallonia, Charleroi → Wallonia, "
            "Namur → Wallonia, Namen → Wallonia, Mons → Wallonia, "
            "Bergen → Wallonia, Verviers → Wallonia."
        ),
        "product_fit_fallback": "Not specified",
        "recommended_action_fallback": "Review the source article to plan next steps.",
    },
    "ITALY": {
        "name": "Italy",
        "iso2": "IT",
        "news_term_en": "Italy",
        "regions": (
            "Valle d'Aosta", "Piemonte", "Liguria", "Lombardia",
            "Trentino-Alto Adige", "Veneto", "Friuli-Venezia Giulia",
            "Emilia-Romagna", "Toscana", "Marche", "Umbria", "Lazio",
            "Abruzzo", "Molise", "Campania", "Puglia",
            "Basilicata", "Calabria", "Sicilia", "Sardegna",
        ),
        "region_label": "region",
        "output_language": "Italian",
        "city_hints": (
            "Milano → Lombardia, Roma → Lazio, Napoli → Campania, "
            "Torino → Piemonte, Palermo → Sicilia, Genova → Liguria, "
            "Bologna → Emilia-Romagna, Firenze → Toscana, Bari → Puglia, "
            "Catania → Sicilia, Venezia → Veneto, Verona → Veneto, "
            "Padova → Veneto, Trieste → Friuli-Venezia Giulia, "
            "Brescia → Lombardia, Modena → Emilia-Romagna, "
            "Reggio Emilia → Emilia-Romagna, Cagliari → Sardegna, "
            "Taranto → Puglia, Reggio Calabria → Calabria."
        ),
        "product_fit_fallback": "Non specificato",
        "recommended_action_fallback": "Rivedi l'articolo sorgente per pianificare i prossimi passi.",
    },
    "SPAIN": {
        "name": "Spain",
        "iso2": "ES",
        "news_term_en": "Spain",
        "regions": (
            "Andalucia", "Aragon", "Asturias", "Baleares", "Canarias", "Cantabria",
            "Castilla-La Mancha", "Castilla y Leon", "Cataluna", "Comunidad Valenciana",
            "Extremadura", "Galicia", "La Rioja", "Madrid", "Murcia", "Navarra",
            "Pais Vasco", "Ceuta", "Melilla",
        ),
        "region_label": "autonomous community",
        "output_language": "Spanish",
        "city_hints": (
            "Madrid → Madrid, Barcelona → Cataluna, Tarragona → Cataluna, "
            "Valencia → Comunidad Valenciana, Alicante → Comunidad Valenciana, "
            "Castellón → Comunidad Valenciana, Sevilla → Andalucia, Málaga → Andalucia, "
            "Córdoba → Andalucia, Granada → Andalucia, Huelva → Andalucia, "
            "Zaragoza → Aragon, Bilbao → Pais Vasco, Vitoria → Pais Vasco, "
            "Pamplona → Navarra, Valladolid → Castilla y Leon, León → Castilla y Leon, "
            "Burgos → Castilla y Leon, Vigo → Galicia, A Coruña → Galicia, "
            "Gijón → Asturias, Oviedo → Asturias, Santander → Cantabria, "
            "Toledo → Castilla-La Mancha, Badajoz → Extremadura, Logroño → La Rioja, "
            "Palma → Baleares, Las Palmas → Canarias, Santa Cruz de Tenerife → Canarias, "
            "Murcia → Murcia, Cartagena → Murcia."
        ),
        "product_fit_fallback": "No especificado",
        "recommended_action_fallback": "Revisa el artículo de origen para planificar los próximos pasos.",
    },
}

# Used for any country code without an entry above (e.g. a stale DB row).
_DEFAULT_CONFIG: dict = {
    "name": "",
    "iso2": "",
    "news_term_en": "",
    "regions": (),
    "region_label": "region",
    "output_language": "English",
    "city_hints": "",
    "product_fit_fallback": "Not specified",
    "recommended_action_fallback": "Review the source article to plan next steps.",
}

# Flat union of all configured regions — for validating region tags on write.
VALID_REGIONS: frozenset[str] = frozenset(
    r for cfg in _COUNTRY_CONFIG.values() for r in cfg["regions"]
)


def get_country_config(country_code: str) -> dict:
    """Full config dict for ``country_code``, or a neutral default if it isn't configured."""
    return _COUNTRY_CONFIG.get(country_code, _DEFAULT_CONFIG)


def configured_countries() -> dict[str, str]:
    """``{country_code: display_name}`` for every configured country, in definition order."""
    return {code: cfg["name"] for code, cfg in _COUNTRY_CONFIG.items()}


def is_country_configured(country_code: str) -> bool:
    """Return True if ``country_code`` has a dedicated entry in ``_COUNTRY_CONFIG``."""
    return country_code in _COUNTRY_CONFIG


def country_display_name(country_code: str) -> str:
    """Human-readable country name for subject lines, headers, and the web app."""
    return get_country_config(country_code)["name"] or country_code.replace("_", " ").title()


def country_iso2(country_code: str) -> str:
    """Two-letter ISO code (e.g. ``"ES"``) — used for Google News geo-targeting, flags, and lead codes."""
    return get_country_config(country_code)["iso2"] or country_code[:2].upper()


def get_country_regions(country_code: str) -> tuple[str, ...]:
    """Valid administrative regions for ``country_code`` (empty tuple if not configured)."""
    return get_country_config(country_code)["regions"]


def get_country_output_language(country_code: str) -> str:
    """Language (e.g. ``"Spanish"``) the LLM writes sales text in for this country."""
    return get_country_config(country_code)["output_language"]


def get_country_fallback_strings(country_code: str) -> dict[str, str]:
    """Language-specific fallback strings for ``product_fit`` and ``recommended_action``."""
    cfg = get_country_config(country_code)
    return {
        "product_fit":        cfg["product_fit_fallback"],
        "recommended_action": cfg["recommended_action_fallback"],
    }
