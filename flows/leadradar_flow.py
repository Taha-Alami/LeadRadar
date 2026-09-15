"""
LeadRadar — Prefect flow.

Wraps the ``run_country_pipeline()`` stages as Prefect tasks so progress,
retries, and run history are visible in the Prefect UI. Behaviour matches
``main.py``:

- an error-alert email on any unhandled exception for a country
- remaining countries still run
- the flow is marked FAILED at the end if any country failed

Deployment
----------
1. prefect server start
2. prefect work-pool create leadradar-pool --type process   (skip if it exists)
3. python flows/deploy.py
4. prefect worker start --pool leadradar-pool

Manual run
----------
    python flows/leadradar_flow.py [COUNTRY]   (default: ALL)
"""
from __future__ import annotations

import logging
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from prefect import flow, get_run_logger, task
from prefect.cache_policies import NO_CACHE

root = Path(__file__).parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
)

# ── Tasks ─────────────────────────────────────────────────────────────────────

@task(name="collect-and-filter-news", retries=2, retry_delay_seconds=60, cache_policy=NO_CACHE)
def collect_task(country_code: str, cfg):
    """RSS collection → deduplicated NewsItem list + new-item subset + picker candidates."""
    task_logger = get_run_logger()
    from pipeline.lead_pipeline import _filter_new_items, _get_recently_sent_urls
    from pipeline.news_sources import collect_country_news

    task_logger.info("[COLLECT] Collecting %s news from RSS feeds…", country_code)
    news_items = collect_country_news(country_code, cfg)
    task_logger.info("[COLLECT] %d unique articles collected", len(news_items))

    if not news_items:
        raise RuntimeError(f"No articles collected for {country_code}. Check network / RSS sources.")

    new_items = _filter_new_items(news_items, country_code)
    task_logger.info("[FILTER]  %d truly new articles (not previously stored)", len(new_items))

    already_sent = _get_recently_sent_urls(country_code)
    candidates   = [item for item in new_items if item.url not in already_sent]
    task_logger.info(
        "[DEDUP]   %d candidates after excluding %d already-featured URLs",
        len(candidates), len(already_sent),
    )
    return news_items, new_items, candidates


@task(name="triage-and-persist-news", cache_policy=NO_CACHE)
def triage_persist_task(country_code: str, new_items, candidates, cfg, llm):
    """LLM triage (sector / priority / region / location) + write to core.news_articles."""
    task_logger = get_run_logger()
    from pipeline.lead_pipeline import _persist_country_news_to_db
    from pipeline.opportunity_triage import drop_out_of_country, triage_articles

    if candidates:
        task_logger.info("[TRIAGE]  Tagging %d candidates with sector/priority/region…", len(candidates))
        try:
            triage_articles(candidates, country_code, cfg, llm)
            task_logger.info("[TRIAGE]  Done.")
        except Exception:
            task_logger.warning("[TRIAGE]  Failed — continuing with untagged candidates (pipeline not blocked).")

    before = len(candidates)
    new_items, candidates = drop_out_of_country(new_items, candidates, country_code)
    if len(candidates) < before:
        task_logger.info("[TRIAGE]  Dropped %d out-of-country articles (not stored, not picked).", before - len(candidates))

    try:
        written_news = _persist_country_news_to_db(new_items, cfg, extra_logger=task_logger)
    except Exception:
        task_logger.exception("[PERSIST-NEWS] Unhandled exception writing to core.news_articles.")
        written_news = 0
    if written_news < len(new_items):
        task_logger.warning(
            "[PERSIST-NEWS] Only %d/%d new articles written to core.news_articles — see per-row errors above.",
            written_news, len(new_items),
        )
    else:
        task_logger.info("[PERSIST-NEWS] %d/%d new articles written to core.news_articles", written_news, len(new_items))

    task_logger.info("[FILTER]  %d candidates ready for picking", len(candidates))
    return candidates


@task(name="pick-leads", cache_policy=NO_CACHE)
def pick_task(country_code: str, candidates, cfg, llm):
    """LLM picker → 3-5 best leads."""
    task_logger = get_run_logger()
    from pipeline.opportunity_picker import pick_opportunities

    task_logger.info(
        "[PICK]    Selecting best leads from %d candidates — provider: %s | model: %s",
        len(candidates), llm.active_provider.upper(), llm.scoring_model(),
    )
    picks = pick_opportunities(candidates, country_code, cfg, llm)
    task_logger.info("[PICK]    %d leads selected", len(picks))
    return picks


@task(name="enrich-leads", cache_policy=NO_CACHE)
def enrich_task(country_code: str, picks, cfg, llm):
    """Scrape articles + per-lead LLM extraction; drops leads that turn out to be too late."""
    task_logger = get_run_logger()
    from pipeline.opportunity_enricher import enrich_opportunities

    task_logger.info("[ENRICH]  Scraping and enriching %d picks…", len(picks))
    enriched_all = enrich_opportunities(picks, country_code, cfg, llm)
    enriched     = []
    for opp in enriched_all:
        if opp.is_actionable:
            enriched.append(opp)
        else:
            reason = (
                "phase=Completed — project finished"
                if opp.phase.lower() == "completed"
                else "phase=Construction, no expansion signal — too late"
            )
            task_logger.warning("[ENRICH]  FILTERED  '%s'  →  %s", opp.title[:70], reason)
    task_logger.info("[ENRICH]  %d/%d leads survived enrichment", len(enriched), len(enriched_all))
    return enriched


@task(name="compose-and-send-digest", cache_policy=NO_CACHE)
def compose_send_task(country_code: str, enriched, news_items, cfg):
    """Deterministic HTML composition + email dispatch."""
    task_logger = get_run_logger()
    from core.mailer import send_email
    from pipeline.digest_composer import compose_digest_email

    html, subject = compose_digest_email(country_code, enriched, len(news_items), app_base_url=cfg.app_base_url)
    task_logger.info("[COMPOSE] Email body: %d chars | subject: '%s'", len(html), subject)

    recipients = cfg.get_recipients(country_code)
    cc         = cfg.get_cc(country_code)
    task_logger.info("[SEND]    To: %s | Cc: %s", ", ".join(recipients) or "(none)", ", ".join(cc) or "(none)")
    sent = send_email(cfg, subject, html, recipients=recipients, cc=cc)
    if sent:
        task_logger.info("[SEND]    Digest sent via %s.", cfg.mail_backend)
    else:
        task_logger.error("[SEND]    Dispatch failed — check MAIL_BACKEND settings in .env.")
    return sent


@task(name="persist-enriched-leads", cache_policy=NO_CACHE)
def persist_enriched_task(enriched, cfg):
    """Write enriched leads to core.news_enriched (fire-and-forget)."""
    task_logger = get_run_logger()
    from pipeline.lead_pipeline import _persist_opportunities_to_db

    written = _persist_opportunities_to_db(enriched, cfg)
    if written < len(enriched):
        task_logger.warning(
            "[PERSIST-ENRICHED] Only %d/%d leads written to core.news_enriched — check module logs above.",
            written, len(enriched),
        )
    else:
        task_logger.info("[PERSIST-ENRICHED] %d/%d leads written to core.news_enriched", written, len(enriched))


# ── Flow ──────────────────────────────────────────────────────────────────────

@flow(name="LeadRadar — Daily Lead Discovery", log_prints=True)
def run_leadradar(country_code: str = "ALL") -> None:
    """
    Full LeadRadar pipeline.

    ``"ALL"`` (the default) runs ``ENABLED_COUNTRIES`` from the environment,
    or every configured country when unset — sequentially. A failure for one
    country sends an error alert and continues with the rest; the flow is
    marked FAILED at the end only if at least one country failed.

    Stages per country:
        1 — collect + filter + dedup
        2 — triage (sector/priority/region/location) + persist to core.news_articles
        3 — pick the best leads
        4 — enrich (scrape + LLM extraction)
        5 — compose + send the HTML digest
        6 — persist to core.news_enriched
    """
    from core.config import AppConfig
    from core.llm_client import LLMClient
    from core.mailer import send_error_alert
    from pipeline.country_config import configured_countries, is_country_configured

    logger = get_run_logger()
    start  = datetime.now()

    cfg = AppConfig.from_env()
    llm = LLMClient(cfg)

    if country_code.upper() == "ALL":
        enabled   = [c.strip().upper() for c in os.getenv("ENABLED_COUNTRIES", "").split(",") if c.strip()]
        countries = enabled or list(configured_countries())
    else:
        countries = [country_code.upper()]

    logger.info(
        "══════════════════════════════════════════════════════\n"
        "  LEADRADAR — %s\n"
        "  Countries     : %s\n"
        "  Provider      : %s\n"
        "  Scoring model : %s\n"
        "══════════════════════════════════════════════════════",
        start.strftime("%Y-%m-%d %H:%M"),
        ", ".join(countries),
        cfg.llm_provider.upper(),
        llm.scoring_model(),
    )

    failures: list[str] = []

    for cc in countries:
        if not is_country_configured(cc):
            logger.info("Skipping %s — not configured in pipeline/country_config.py.", cc)
            continue

        logger.info("── %s: starting ──", cc)
        try:
            news_items, new_items, candidates = collect_task(cc, cfg)
            candidates = triage_persist_task(cc, new_items, candidates, cfg, llm)

            picks = pick_task(cc, candidates, cfg, llm)
            if not picks:
                logger.info("%s: no actionable leads today — skipping email.", cc)
                continue

            enriched = enrich_task(cc, picks, cfg, llm)
            if not enriched:
                logger.info("%s: no leads survived enrichment — skipping email.", cc)
                continue

            sent = compose_send_task(cc, enriched, news_items, cfg)

            # Always persist, even if dispatch failed — the web app still shows the leads
            persist_enriched_task(enriched, cfg)

            if not sent:
                raise RuntimeError("Email dispatch failed — check MAIL_BACKEND settings in .env.")

            logger.info("── %s: done ──", cc)

        except Exception as exc:
            logging.exception("%s LeadRadar pipeline failed: %s", cc, exc)
            send_error_alert(cfg, f"{cc} LeadRadar", str(exc), traceback.format_exc())
            failures.append(cc)

    elapsed = (datetime.now() - start).total_seconds()
    if failures:
        logger.error("LEADRADAR DONE WITH FAILURES — %.0fs | failed: %s | %s", elapsed, ", ".join(failures), llm.token_summary)
        raise RuntimeError(f"LeadRadar finished with failures: {', '.join(failures)}")

    logger.info("LEADRADAR DONE — %.0fs | %s", elapsed, llm.token_summary)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_leadradar(sys.argv[1] if len(sys.argv) > 1 else "ALL")
