import pytest


@pytest.fixture(autouse=True)
def _default_business_profile(monkeypatch):
    """Every test runs against the bundled example profile unless it opts out."""
    monkeypatch.delenv("BUSINESS_PROFILE", raising=False)
