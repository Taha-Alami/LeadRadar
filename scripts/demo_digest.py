"""
Render a sample lead digest from built-in fictional leads — no API keys,
database, or network access needed. Handy for previewing the email design or
tweaking the business profile's labels.

Usage::

    uv run python scripts/demo_digest.py                         # → ./outbox/<timestamp>_....html
    uv run python scripts/demo_digest.py docs/sample_digest.html # → explicit path

All companies, projects, and figures below are invented.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import AppConfig
from core.mailer import send_email
from core.news_models import EnrichedOpportunity
from pipeline.digest_composer import compose_digest_email

SAMPLE_LEADS = [
    EnrichedOpportunity(
        title          = "Water authority opens tender for 14 km dike reinforcement near Zwolle",
        url            = "https://news.example.com/dike-reinforcement-zwolle",
        source         = "Example Infra News",
        country        = "NETHERLANDS",
        published_date = "Mon, 14 Sep 2026",
        phase          = "Tender",
        company        = "Regional water authority",
        city           = "Zwolle",
        project_type   = "dike reinforcement",
        project_value  = "EUR 62 million",
        product_fit    = "A 14 km linear site over roughly three years needs earthmoving fleets, "
                         "lighting towers for winter shifts, and mobile site cabins along the route.",
        why_it_matters = "The tender closes in six weeks — bidding contractors are building their equipment plans right now.",
        recommended_action = "Identify the bidding consortia on the national tender portal and offer a route-wide fleet package.",
        scrape_method  = "trafilatura",
        sector         = "Construction",
        priority       = "High",
        region         = "Overijssel",
        end_usage      = "Infrastructure",
    ),
    EnrichedOpportunity(
        title          = "Contract awarded for 120 MW solar park near Emmen",
        url            = "https://news.example.com/solar-park-emmen",
        source         = "Example Energy Weekly",
        country        = "NETHERLANDS",
        published_date = "Fri, 11 Sep 2026",
        phase          = "Awarded",
        company        = "Northwind Solar B.V.",
        city           = "Emmen",
        project_type   = "utility-scale solar park",
        project_value  = "EUR 85 million",
        product_fit    = "A remote greenfield site needs generators and temporary power until grid connection, "
                         "plus telehandlers for panel installation.",
        why_it_matters = "EPC contract just signed; ground-breaking is planned for Q1 — equipment is sourced in the next weeks.",
        recommended_action = "Call the EPC contractor's site-setup manager about temporary power and telehandlers.",
        scrape_method  = "newspaper4k",
        sector         = "Energy & Utilities",
        priority       = "High",
        region         = "Drenthe",
        end_usage      = "Sustainable Energy",
    ),
    EnrichedOpportunity(
        title          = "Developer plans 90,000 m² distribution hub in Tilburg",
        url            = "https://news.example.com/distribution-hub-tilburg",
        source         = "Example Property Journal",
        country        = "NETHERLANDS",
        published_date = "Thu, 10 Sep 2026",
        phase          = "Permitting",
        company        = "Example Logistics Real Estate",
        city           = "Tilburg",
        project_type   = "logistics distribution centre",
        project_value  = None,
        product_fit    = "Large-footprint groundworks and steel erection imply excavators, aerial work platforms, and site fencing.",
        why_it_matters = "The zoning plan is under review — early contact puts us on the general contractor's shortlist.",
        recommended_action = "Contact the developer's project director and ask which contractors are invited to tender.",
        scrape_method  = "trafilatura",
        sector         = "Industry",
        priority       = "Medium",
        region         = "Noord-Brabant",
        end_usage      = "Commercial",
    ),
]


def main() -> None:
    cfg = AppConfig.from_env()
    html, subject = compose_digest_email("NETHERLANDS", SAMPLE_LEADS, n_scanned=187, app_base_url=cfg.app_base_url)

    if len(sys.argv) > 1:
        out = Path(sys.argv[1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        print(f"Wrote {out}")
        return

    # Always the file backend — a demo must never email real people.
    send_email(replace(cfg, mail_backend="file"), subject, html, recipients=["demo@example.com"])
    print(f"Digest written to ./{cfg.mail_outbox_dir}/")


if __name__ == "__main__":
    main()
