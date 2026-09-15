from pathlib import Path

import pytest

from core.business_profile import get_profile, load_profile

EXAMPLE = Path(__file__).parent.parent / "profiles" / "acme_equipment_rental.toml"

SOLAR_PROFILE = """
[company]
name     = "Sunny Rooftops"
tagline  = "commercial solar installation"
products = ["rooftop PV systems", "battery storage"]

[lead]
fit_label      = "Solar potential"
fit_definition = "why this building could host a PV installation"
relevance      = "the project involves a large roof or a new commercial building."

[priority]
high   = "large logistics or industrial roofs"
medium = "offices and schools"
low    = "single-family homes"

[[sectors]]
name        = "Logistics"
description = "warehouses and distribution centres"
higher_margin = true

[[sectors]]
name        = "Public"
description = "schools, hospitals, municipal buildings"
"""


def test_example_profile_loads():
    profile = load_profile(EXAMPLE)
    assert profile.company_name == "Acme Equipment Rental"
    assert profile.products
    assert profile.priority_high and profile.priority_medium and profile.priority_low
    assert "Construction" in profile.sector_names


def test_get_profile_defaults_to_bundled_example():
    assert get_profile().company_name == "Acme Equipment Rental"


def test_higher_margin_sectors():
    profile = load_profile(EXAMPLE)
    assert "Industry" in profile.higher_margin_sectors
    assert "Construction" not in profile.higher_margin_sectors


def test_team_is_scoped_per_country_with_global_members():
    names = [m.name for m in load_profile(EXAMPLE).team_for_country("SPAIN")]
    assert "Lucía Fernández" in names
    assert "Sam Taylor" in names          # no `countries` → covers every country
    assert "Marco Bianchi" not in names   # Italy only


def test_profile_without_sectors_is_rejected(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text(
        '[company]\nname = "X"\n[lead]\nrelevance = "r"\n'
        '[priority]\nhigh = "h"\nmedium = "m"\nlow = "l"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_profile(bad)


def test_switching_profile_changes_every_prompt(tmp_path, monkeypatch):
    custom = tmp_path / "solar.toml"
    custom.write_text(SOLAR_PROFILE, encoding="utf-8")
    monkeypatch.setenv("BUSINESS_PROFILE", str(custom))

    from pipeline.opportunity_enricher import _make_enricher_system_prompt
    from pipeline.opportunity_picker import _make_picker_system_prompt
    from pipeline.opportunity_triage import _make_triage_system_prompt

    for build in (_make_triage_system_prompt, _make_picker_system_prompt, _make_enricher_system_prompt):
        prompt = build("SPAIN")
        assert "Sunny Rooftops" in prompt
        assert "Logistics" in prompt
        assert "Acme" not in prompt
