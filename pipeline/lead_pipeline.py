"""
LeadRadar pipeline orchestrator.

``run_country_pipeline()`` is the single entry point for one country's run.

Stage summary:
    1.  collect  → ``news_sources.collect_country_news()``
    1b. filter   → which collected articles aren't already in core.news_articles (lookup only)
    2.  dedup    → exclude URLs already turned into leads in the last 14 days
    2b. triage   → ``opportunity_triage.triage_articles()`` tags every candidate with
                   sector / priority / region / location
    2c. drop     → out-of-country articles are discarded (never stored or picked)
    1c. persist  → all new articles → core.news_articles (single write, carrying
                   the triage tags; fire-and-forget)
    3.  pick     → ``opportunity_picker.pick_opportunities()``
    4.  enrich   → ``opportunity_enricher.enrich_opportunities()``
    5.  compose  → ``digest_composer.compose_digest_email()``
    6.  send     → ``core.mailer.send_email()``
    7.  persist  → core.news_enriched (fire-and-forget, never blocks on DB errors)

Stage 1c makes every collected article browsable in the web app, not only
the handful that were picked. A day with zero good opportunities is not an
error — the pipeline logs and returns without sending an empty digest.

Database calls are deliberately fire-and-forget throughout: a DB outage
degrades dedup and history, but never prevents the digest from going out.
"""
from __future__ import annotations

import logging
from datetime import datetime

from core.config import AppConfig
from core.llm_client import LLMClient
from core.mailer import send_email
from core.news_models import EnrichedOpportunity

from pipeline.country_config import VALID_REGIONS, country_display_name, is_country_configured
from pipeline.digest_composer import compose_digest_email
from pipeline.news_sources import collect_country_news
from pipeline.opportunity_enricher import VALID_END_USAGES, enrich_opportunities
from pipeline.opportunity_picker import pick_opportunities
from pipeline.opportunity_triage import drop_out_of_country, triage_articles, valid_sectors

logger = logging.getLogger(__name__)

_DEDUP_LOOKBACK_DAYS = 14
_VALID_PRIORITIES    = frozenset({"High", "Medium", "Low", "Unknown"})


def _parse_date(date_str: str):
    """
    Parse a date string to a Python date, or None on failure.

    Accepts RFC 2822 (e.g. "Mon, 22 Jun 2026 11:25:57 GMT" — what RSS feeds
    give the daily pipeline) or ISO "YYYY-MM-DD" (what the web app passes,
    since it already formats ``publication_date`` via SQL).
    """
    if not date_str:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(date_str).date()
    except Exception:
        pass
    try:
        from datetime import date as _date
        return _date.fromisoformat(date_str)
    except Exception:
        return None


def _filter_new_items(items: list, country_code: str = "") -> list:
    """
    Return the subset of ``items`` not already present in core.news_articles —
    lookup only, no write. Split out from the actual write so the caller can
    triage these items (assigning sector/priority/region) before the single
    INSERT in ``_persist_country_news_to_db`` happens, instead of inserting
    untagged rows now and updating them later.

    On any DB error the full ``items`` list is returned so the pipeline
    degrades gracefully (triage/picker see everything rather than nothing).

    Dedup is scoped per country so the same article can appear independently
    in two different countries' pipelines.
    """
    if not items:
        return []
    try:
        from core.sql_connector import SQLConnector

        connector      = SQLConnector()
        urls           = [item.url for item in items]
        placeholders   = ", ".join("?" * len(urls))
        country_clause = "country = ? AND " if country_code else ""
        country_param  = (country_code,) if country_code else ()
        existing_df    = connector.execute_query(
            f"SELECT url FROM core.news_articles WHERE {country_clause}url IN ({placeholders})",
            country_param + tuple(urls),
        )
        existing_urls = set(existing_df["url"].dropna()) if not existing_df.empty else set()

        new_items = [item for item in items if item.url not in existing_urls]
        logger.info(
            "DB  %d collected → %d new (not yet stored), %d already in core.news_articles",
            len(items), len(new_items), len(existing_urls),
        )
        return new_items

    except Exception:
        logger.exception("DB lookup skipped for country news — treating all items as new so pipeline continues")
        return items


# INSERT only when no row for this URL + country already exists — idempotent so
# re-runs and _filter_new_items fallbacks never create duplicates.
_INSERT_NEWS_SQL = """
INSERT INTO core.news_articles
    (country, title, source_outlet, url, llm_provider, llm_model, publication_date, sector, priority, region)
SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
WHERE NOT EXISTS (
    SELECT 1 FROM core.news_articles WHERE url = ? AND country = ?
)
"""


def _persist_country_news_to_db(items: list, cfg: AppConfig, extra_logger=None) -> int:
    """
    Write already-filtered "new" country news to core.news_articles, one row at a
    time (row-by-row so one bad row never blocks the rest). The INSERT is
    idempotent — if a row for this URL + country already exists (e.g. because
    _filter_new_items fell back to "treat all as new" due to a DB error) the
    SELECT…WHERE NOT EXISTS simply returns 0 rows and nothing is written.

    extra_logger: optional secondary logger (e.g. Prefect's task logger) — any
    per-row exception is logged through both the module logger and extra_logger
    so failures surface on the correct task in the Prefect UI.

    Returns the number of rows successfully written.
    """
    if not items:
        return 0

    from core.sql_connector import SQLConnector

    connector = SQLConnector()
    llm_model = cfg.deployment_scoring if cfg.llm_provider == "azure" else cfg.mistral_model_scoring
    sectors   = frozenset(valid_sectors())
    written   = 0

    for item in items:
        if not item.title:
            msg = f"DB  skipping article with empty title — url={item.url}"
            logger.warning(msg)
            if extra_logger:
                extra_logger.warning(msg)
            continue
        try:
            rows_inserted = connector.execute_non_query(
                _INSERT_NEWS_SQL,
                (
                    item.country,
                    item.title,
                    item.source or None,
                    item.url,
                    cfg.llm_provider,
                    llm_model or None,
                    _parse_date(item.date),
                    item.sector   if item.sector   in sectors           else None,
                    item.priority if item.priority in _VALID_PRIORITIES else None,
                    item.region   if item.region   in VALID_REGIONS     else None,
                    item.url,
                    item.country,
                ),
            )
            if rows_inserted > 0:
                written += 1
            else:
                logger.debug("DB  skipped duplicate row for '%s'", item.url)
                if extra_logger:
                    extra_logger.info("[PERSIST-NEWS] skipped (already in DB): '%s'", item.title[:80])
        except Exception as exc:
            msg = f"DB  failed to write article '{item.title[:60]}' — {exc}"
            logger.exception("DB  failed to write article '%s' — skipping", item.title[:60])
            if extra_logger:
                extra_logger.error(msg)

    logger.info("DB  persisted %d/%d new articles to core.news_articles", written, len(items))
    return written


def _get_recently_sent_urls(country_code: str, lookback_days: int = _DEDUP_LOOKBACK_DAYS) -> set[str]:
    """
    Source URLs (as stored in core.news_articles) of articles that were
    enriched for this country in the last ``lookback_days``, so the same
    still-not-started project isn't re-sent every day. Joins via the
    source_news_id reference so we compare against the original feed URL
    (core.news_articles.url), not the decoded publisher URL
    (core.news_enriched.url).

    Returns an empty set on any DB error — dedup is a nice-to-have, never
    a hard dependency.
    """
    try:
        from core.sql_connector import SQLConnector

        connector = SQLConnector()
        df = connector.execute_query(
            """
            SELECT n.url
            FROM   core.news_articles n
            JOIN   core.news_enriched e ON e.source_news_id = n.id
            WHERE  n.country = ?
              AND  e.created_at >= DATEADD(day, ?, SYSDATETIMEOFFSET())
            """,
            params=(country_code, -lookback_days),
        )
        return set(df["url"].dropna())
    except Exception as exc:
        logger.debug("Dedup lookup skipped: %s", exc)
        return set()


_INSERT_ENRICHED_SQL = """
INSERT INTO core.news_enriched (
    source_news_id, country, title, url, source_outlet, publication_date,
    phase, company, city, project_type, project_value,
    product_fit, why_it_matters, recommended_action,
    scrape_method, llm_provider, llm_model,
    sector, priority, region, scraped_text,
    end_usage, english_summary
) VALUES (?,?,?,?,?,?, ?,?,?,?,?, ?,?,?, ?,?,?, ?,?,?,?,?,?)
"""


def _enricher_llm_model(cfg: AppConfig) -> str:
    return cfg.mistral_model_scoring if cfg.llm_provider == "mistral" else cfg.deployment_scoring


def persist_enriched_opportunity(connector, source_news_id: int | None, opp: EnrichedOpportunity, cfg: AppConfig) -> None:
    """
    Insert one enriched opportunity into core.news_enriched, tied via
    ``source_news_id`` to the core.news_articles row it was scraped from.

    Single write path shared by the daily pipeline (``_persist_opportunities_to_db``
    below) and the web app's on-demand "Enrich with AI" button, so leads found
    either way land in the same table and show up in the same "Enriched" view.

    Also syncs the enricher's (re-checked) tags back to the source article row,
    so filters on the raw article list match what the enriched card displays.
    """
    region    = opp.region    if opp.region    in VALID_REGIONS             else None
    sector    = opp.sector    if opp.sector    in frozenset(valid_sectors()) else None
    priority  = opp.priority  if opp.priority  in _VALID_PRIORITIES         else None
    end_usage = opp.end_usage if opp.end_usage in VALID_END_USAGES          else None
    connector.execute_non_query(
        _INSERT_ENRICHED_SQL,
        (
            source_news_id,
            opp.country,
            opp.title or "",
            opp.url,
            opp.source or "",
            _parse_date(opp.published_date),
            (opp.phase or "")[:29],
            opp.company,
            opp.city,
            opp.project_type,
            opp.project_value,
            opp.product_fit,
            opp.why_it_matters,
            opp.recommended_action,
            (opp.scrape_method or "")[:19],
            (cfg.llm_provider  or "")[:19],
            _enricher_llm_model(cfg) or "",
            sector, priority, region,
            opp.scraped_text,
            end_usage, opp.english_summary,
        ),
    )
    if source_news_id:
        sync_fields: list[str] = []
        sync_vals:   list      = []
        if priority:
            sync_fields.append("priority = ?")
            sync_vals.append(priority)
        if sector:
            sync_fields.append("sector = ?")
            sync_vals.append(sector)
        if region:
            sync_fields.append("region = ?")
            sync_vals.append(region)
        if sync_fields:
            try:
                connector.execute_non_query(
                    f"UPDATE core.news_articles SET {', '.join(sync_fields)} WHERE id = ?",
                    tuple(sync_vals + [source_news_id]),
                )
            except Exception:
                logger.exception("Failed to sync triage tags to news_articles for id=%d", source_news_id)


def _persist_opportunities_to_db(enriched: list[EnrichedOpportunity], cfg: AppConfig) -> int:
    """
    Write today's featured leads to core.news_enriched. Fire-and-forget — a DB
    error here never blocks the email that was already sent.

    Every lead was already written to core.news_articles in Stage 1c, so its
    ``id`` is looked up here by (country, source URL) and used as
    ``source_news_id`` — the same reference the web app supplies directly.

    Returns the number of rows actually committed. Each INSERT is isolated so
    one failure does not block the rest of the batch.
    """
    if not enriched:
        return 0

    written = 0  # declared outside try so the except block can return it
    try:
        from core.sql_connector import SQLConnector

        connector    = SQLConnector()
        lookup_urls  = sorted({opp.source_url or opp.url for opp in enriched})
        placeholders = ", ".join("?" * len(lookup_urls))
        id_df = connector.execute_query(
            f"SELECT MAX(id) AS id, country, url FROM core.news_articles "
            f"WHERE url IN ({placeholders}) GROUP BY country, url",
            tuple(lookup_urls),
        )
        id_by_key = (
            {(r["country"], r["url"]): r["id"] for r in id_df.to_dict("records")}
            if not id_df.empty else {}
        )

        # Skip leads already written (protects against double-runs on the same day)
        known_ids = [int(v) for v in id_by_key.values() if v is not None]
        if known_ids:
            already_df = connector.execute_query(
                f"SELECT source_news_id FROM core.news_enriched "
                f"WHERE source_news_id IN ({', '.join('?' * len(known_ids))})",
                tuple(known_ids),
            )
            already_enriched = set(already_df["source_news_id"].dropna().astype(int)) if not already_df.empty else set()
        else:
            already_enriched = set()

        for opp in enriched:
            source_news_id = id_by_key.get((opp.country, opp.source_url or opp.url))
            if source_news_id is None:
                logger.warning(
                    "No core.news_articles row found for '%s' — inserting with source_news_id=NULL.",
                    opp.source_url or opp.url,
                )
            sid_int = int(source_news_id) if source_news_id is not None else None
            if sid_int is not None and sid_int in already_enriched:
                logger.info("Skipping already-enriched lead (source_news_id=%d): '%s'", sid_int, opp.title[:80])
                continue
            try:
                persist_enriched_opportunity(connector, sid_int, opp, cfg)
                written += 1
            except Exception:
                logger.exception(
                    "Failed to persist enriched lead '%s' (source_news_id=%s) — skipping.",
                    opp.title[:80], source_news_id,
                )

        logger.info("DB  persisted %d/%d leads to core.news_enriched", written, len(enriched))
        return written

    except Exception:
        logger.exception(
            "DB setup failed before writing enriched leads (%d/%d already committed).",
            written, len(enriched),
        )
        return written


def run_country_pipeline(country_code: str, cfg: AppConfig, llm: LLMClient) -> None:
    """
    Execute one full LeadRadar run for a single country.

    Args:
        country_code: A code from ``pipeline.country_config`` (e.g. ``"SPAIN"``).
        cfg:          Loaded ``AppConfig`` — passed to every stage.
        llm:          Initialised ``LLMClient``.

    Raises:
        RuntimeError: If no articles are collected (network/RSS issue) or if
                      email dispatch fails after leads were found.
    """
    start   = datetime.now()
    country = country_display_name(country_code)
    logger.info("=== %s LEADRADAR — %s ===", country.upper(), start.isoformat())

    if not is_country_configured(country_code):
        logger.info("STAGE 0  %s is not configured in pipeline/country_config.py — skipping.", country_code)
        return

    # ── Stage 1: Collect ──────────────────────────────────────────────────────
    logger.info("STAGE 1  Collecting %s news…", country)
    news_items = collect_country_news(country_code, cfg)
    logger.info("STAGE 1  %d unique articles collected", len(news_items))
    if not news_items:
        raise RuntimeError(f"No articles collected for {country}. Check network connectivity and RSS source availability.")

    # ── Stage 1b: Filter — which collected articles are not already stored ────
    new_items = _filter_new_items(news_items, country_code)
    logger.info("STAGE 1b %d truly new articles (not previously stored)", len(new_items))

    # ── Stage 2: Exclude URLs already turned into leads recently ─────────────
    already_sent = _get_recently_sent_urls(country_code)
    candidates   = [item for item in new_items if item.url not in already_sent]
    logger.info("STAGE 2  %d candidates after excluding %d already-featured URLs", len(candidates), len(already_sent))

    # ── Stage 2b: Triage — tag sector/priority/region for every candidate ─────
    # Mutates the same NewsItem objects referenced by new_items, so the single
    # persist call below (which writes ALL new_items) picks up these tags. A
    # triage failure degrades to untagged rows rather than losing the articles.
    if candidates:
        logger.info("STAGE 2b Triaging %d candidates…", len(candidates))
        try:
            triage_articles(candidates, country_code, cfg, llm)
        except Exception:
            logger.exception("STAGE 2b Triage failed — continuing with untagged candidates")

    # ── Stage 2c: Drop out-of-country articles (never stored, picked, or emailed)
    new_items, candidates = drop_out_of_country(new_items, candidates, country_code)

    # ── Stage 1c: Persist — single write for all new articles, tags included ──
    _persist_country_news_to_db(new_items, cfg)

    # ── Stage 3: Pick ─────────────────────────────────────────────────────────
    logger.info(
        "STAGE 3  Picking best leads with %s / %s… (%d candidates)",
        llm.active_provider.upper(), llm.scoring_model(), len(candidates),
    )
    picks = pick_opportunities(candidates, country_code, cfg, llm)
    if not picks:
        logger.info("STAGE 3  No actionable leads for %s today — skipping email.", country)
        return

    # ── Stage 4: Enrich (scrape + extract) ────────────────────────────────────
    logger.info("STAGE 4  Scraping and enriching %d picks…", len(picks))
    enriched = [opp for opp in enrich_opportunities(picks, country_code, cfg, llm) if opp.is_actionable]
    if not enriched:
        logger.info("STAGE 4  No actionable leads survived enrichment for %s — skipping email.", country)
        return
    logger.info("STAGE 4  %d leads enriched", len(enriched))

    # ── Stage 5: Compose ──────────────────────────────────────────────────────
    html, subject = compose_digest_email(country_code, enriched, len(news_items), app_base_url=cfg.app_base_url)
    logger.info("STAGE 5  Email body: %d chars", len(html))

    # ── Stage 6: Send ─────────────────────────────────────────────────────────
    recipients = cfg.get_recipients(country_code)
    cc         = cfg.get_cc(country_code)
    logger.info("STAGE 6  Sending to %s (cc: %s)…", ", ".join(recipients) or "(none)", ", ".join(cc) or "(none)")
    sent = send_email(cfg, subject, html, recipients=recipients, cc=cc)

    # ── Stage 7: Persist (always — not gated on dispatch success) ─────────────
    _persist_opportunities_to_db(enriched, cfg)

    elapsed = (datetime.now() - start).total_seconds()
    logger.info("=== %s LEADRADAR DONE — %.0fs | %s ===", country.upper(), elapsed, llm.token_summary)

    if not sent:
        raise RuntimeError("Email dispatch failed. Check the MAIL_BACKEND settings in .env.")
