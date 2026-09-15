import pytest

from core.news_models import EnrichedOpportunity, NewsItem
from pipeline.country_config import get_country_regions
from pipeline.opportunity_picker import _resolve_pick
from pipeline.opportunity_triage import _clean_location, clean_enum, clean_priority, drop_out_of_country


def _item(n: int, **kwargs) -> NewsItem:
    return NewsItem(title=f"Title {n}", summary="", url=f"https://news.example/{n}", date="", source="Test", **kwargs)


# ── Closed-vocabulary normalisation of LLM output ────────────────────────────

def test_clean_enum_strips_diacritics():
    assert clean_enum("Cataluña", get_country_regions("SPAIN")) == "Cataluna"


def test_clean_enum_maps_local_language_alias():
    assert clean_enum("Vlaanderen", get_country_regions("BELGIUM")) == "Flanders"


def test_clean_enum_is_case_insensitive():
    assert clean_enum("lombardia", get_country_regions("ITALY")) == "Lombardia"


@pytest.mark.parametrize("value", ["Bavaria", "null", "", None])
def test_clean_enum_rejects_unknown_and_empty(value):
    assert clean_enum(value, get_country_regions("ITALY")) is None


def test_clean_priority():
    assert clean_priority("high") == "High"
    assert clean_priority("urgent") == "Unknown"
    assert clean_priority(None) == "Unknown"


def test_clean_location():
    assert _clean_location("In-Country") == "in_country"
    assert _clean_location("abroad") == "abroad"
    assert _clean_location("somewhere else") == "unclear"


# ── Resolving LLM picks back to articles ─────────────────────────────────────

def test_resolve_pick_trusts_url_over_index():
    items  = [_item(1), _item(2), _item(3)]
    by_url = {i.url: i for i in items}
    assert _resolve_pick({"index": 1, "url": "https://news.example/3"}, items, by_url, "SPAIN") is items[2]


def test_resolve_pick_falls_back_to_index():
    items  = [_item(1), _item(2)]
    by_url = {i.url: i for i in items}
    assert _resolve_pick({"index": 2, "url": "https://mangled.example"}, items, by_url, "SPAIN") is items[1]
    assert _resolve_pick({"index": 99}, items, by_url, "SPAIN") is None


# ── Location filter ──────────────────────────────────────────────────────────

def test_drop_out_of_country_keeps_unclear_and_untriaged():
    abroad, unclear, untriaged = _item(1, location="abroad"), _item(2, location="unclear"), _item(3)
    everything = [abroad, unclear, untriaged]
    new_items, candidates = drop_out_of_country(everything, everything, "SPAIN")
    assert new_items == [unclear, untriaged]
    assert candidates == [unclear, untriaged]


# ── Prompt line format ───────────────────────────────────────────────────────

def test_scoring_prompt_line_appends_tags_only_after_triage():
    item = _item(1)
    assert "SECTOR:" not in item.to_scoring_prompt_line(1)
    item.sector, item.priority = "Industry", "High"
    assert "SECTOR:Industry | PRIORITY:High | REGION:Unknown" in item.to_scoring_prompt_line(1)


# ── Actionability rule ───────────────────────────────────────────────────────

def _lead(phase: str, expansion_signal: str | None = None) -> EnrichedOpportunity:
    return EnrichedOpportunity(
        title="t", url="u", source="s", country="SPAIN", published_date="", phase=phase,
        company=None, city=None, project_type=None, project_value=None,
        product_fit="", why_it_matters="", recommended_action="", scrape_method="trafilatura",
        expansion_signal=expansion_signal,
    )


@pytest.mark.parametrize(("phase", "expansion", "expected"), [
    ("Tender",       None,                        True),
    ("Awarded",      None,                        True),
    ("Completed",    None,                        False),
    ("Construction", None,                        False),
    ("Construction", "second contractor joining", True),
])
def test_is_actionable(phase, expansion, expected):
    assert _lead(phase, expansion).is_actionable is expected
