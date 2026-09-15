import pytest

from pipeline.country_config import configured_countries, get_country_regions
from pipeline.news_sources import THEMES_EN, build_gnews_url, build_search_plan
from pipeline.opportunity_enricher import _make_enricher_system_prompt
from pipeline.opportunity_picker import _make_picker_system_prompt
from pipeline.opportunity_triage import _make_triage_system_prompt

COUNTRIES = list(configured_countries())


@pytest.mark.parametrize("build", [_make_triage_system_prompt, _make_picker_system_prompt, _make_enricher_system_prompt])
@pytest.mark.parametrize("country", COUNTRIES)
def test_prompts_are_built_from_profile_and_country(build, country):
    prompt = build(country)
    assert "Acme Equipment Rental" in prompt
    assert "Energy & Utilities" in prompt              # sector vocabulary comes from the profile
    for region in get_country_regions(country):        # region vocabulary comes from the country
        assert region in prompt


def test_enricher_writes_sales_text_in_the_country_language():
    assert "IN SPANISH" in _make_enricher_system_prompt("SPAIN")
    assert "IN POLISH" in _make_enricher_system_prompt("POLAND")
    assert "Equipment need" not in _make_enricher_system_prompt("SPAIN")   # label is UI-only
    assert "rent equipment" in _make_enricher_system_prompt("SPAIN")       # fit_definition is used


def test_picker_uses_profile_quality_signals():
    prompt = _make_picker_system_prompt("ITALY")
    assert "Heavy phases" in prompt
    assert "higher-margin sectors" in prompt


def test_gnews_url_encoding():
    url = build_gnews_url("construction+infrastructure", "España", "es", "ES", freshness="3d")
    assert url.startswith("https://news.google.com/rss/search?q=construction%2Binfrastructure%20Espa%C3%B1a+when:3d")
    assert url.endswith("&hl=es&gl=ES&ceid=ES:es")


def test_search_plan_includes_profile_extras():
    plan = build_search_plan("SPAIN")
    assert ("earthworks+civil+engineering+contract", "Spain", "en") in plan
    assert ("movimiento+de+tierras+licitación", "España", "es") in plan


def test_belgium_gets_profile_extras_in_both_languages():
    extras = {"grondwerk+aanbesteding", "terrassement+marché+public"}
    langs  = {lang for theme, _, lang in build_search_plan("BELGIUM") if theme in extras}
    assert langs == {"nl", "fr"}


@pytest.mark.parametrize("country", COUNTRIES)
def test_every_configured_country_has_a_search_plan(country):
    assert len(build_search_plan(country)) > len(THEMES_EN)
