from dataclasses import replace

import pytest

from core.config import AppConfig
from core.mailer import send_email
from core.news_models import EnrichedOpportunity
from pipeline.digest_composer import compose_digest_email, compose_share_email


def _lead(**overrides) -> EnrichedOpportunity:
    fields = dict(
        title="City tenders new hospital wing", url="https://example.com/a?x=1&y=2",
        source="Example Times", country="SPAIN", published_date="Mon, 14 Sep 2026", phase="Tender",
        company="Example Health Authority", city="Zaragoza", project_type="hospital extension",
        project_value="EUR 38 million", product_fit="Earthworks and temporary power for a 24-month site.",
        why_it_matters="Tender open until October.", recommended_action="Contact the procurement office.",
        scrape_method="trafilatura", sector="Public Administration",
    )
    fields.update(overrides)
    return EnrichedOpportunity(**fields)


# ── Digest composer ──────────────────────────────────────────────────────────

def test_digest_subject_and_stats():
    html, subject = compose_digest_email("SPAIN", [_lead(), _lead(title="Second")], n_scanned=240)
    assert subject.startswith("Spain Lead Digest — 2 new leads — ")
    assert ">240<" in html
    assert "Equipment need" in html     # fit label comes from the business profile


def test_single_lead_subject_is_singular():
    _, subject = compose_digest_email("ITALY", [_lead(country="ITALY")], n_scanned=10)
    assert "— 1 new lead —" in subject


def test_untrusted_text_is_escaped():
    html, _ = compose_digest_email(
        "SPAIN", [_lead(title="<script>alert(1)</script>", company='"><img src=x onerror=alert(1)>')], n_scanned=1,
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x" not in html


def test_urls_are_attribute_safe():
    html, _ = compose_digest_email("SPAIN", [_lead()], n_scanned=1)
    assert 'href="https://example.com/a?x=1&amp;y=2"' in html


def test_app_link_uses_configured_base_url():
    html, _ = compose_digest_email("SPAIN", [_lead()], 1, app_base_url="https://leads.example.org/")
    assert 'href="https://leads.example.org/?country=SPAIN"' in html


def test_share_email_escapes_the_sender_message():
    html, subject = compose_share_email(_lead(), "Look at this <b>now</b>\nThanks", enrichment_id=42,
                                        app_base_url="https://x.example")
    assert "&lt;b&gt;now&lt;/b&gt;<br>Thanks" in html
    assert 'href="https://x.example/group/42"' in html
    assert subject == "Shared Opportunity — City tenders new hospital wing"


# ── Mail backends + recipients ───────────────────────────────────────────────

@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for var in ("MAIL_BACKEND", "EMAIL_RECIPIENTS", "EMAIL_CC", "RECIPIENTS_SPAIN", "CC_SPAIN"):
        monkeypatch.delenv(var, raising=False)
    return replace(AppConfig.from_env(), mail_backend="file", mail_outbox_dir=str(tmp_path))


def test_file_backend_writes_the_message(cfg, tmp_path):
    assert send_email(cfg, "Spain Lead Digest", "<p>hi</p>", recipients=["a@example.com"]) is True
    files = list(tmp_path.glob("*.html"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "To: a@example.com" in content
    assert "<p>hi</p>" in content


def test_unknown_backend_fails_softly(cfg):
    assert send_email(replace(cfg, mail_backend="pigeon"), "s", "<p/>", recipients=["a@example.com"]) is False


def test_real_backend_without_recipients_fails_softly(cfg):
    assert send_email(replace(cfg, mail_backend="smtp", email_recipients=()), "s", "<p/>") is False


def test_per_country_recipients(cfg, monkeypatch):
    monkeypatch.setenv("RECIPIENTS_SPAIN", "es1@example.com, es2@example.com")
    monkeypatch.setenv("CC_SPAIN", "manager@example.com")
    cfg = replace(cfg, email_recipients=("all@example.com",), email_cc=("cc@example.com",))
    assert cfg.get_recipients("SPAIN") == ["es1@example.com", "es2@example.com"]
    assert cfg.get_recipients("ITALY") == ["all@example.com"]
    assert cfg.get_cc("SPAIN") == ["cc@example.com", "manager@example.com"]
