"""
LeadRadar web app — FastAPI + HTMX + Tailwind.

Browse every collected article per country (with its triage tags), trigger
on-demand AI enrichment for any of them, and work the resulting leads as a
team: link duplicate stories into groups, assign, qualify, comment with
@mentions, share by email, find decision-maker contacts, and chat with an AI
assistant that has read the full article.

Articles come from core.news_articles; enriched leads live in core.news_enriched.

Run:
    uv run uvicorn webapp.app:app --reload --port 8000
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import html as html_lib
import json
import logging
import math
import os
import secrets
from urllib.parse import parse_qs, urlparse
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from clay_contact_finder import ClayContactFinder, Contact as ClayContact
from core.business_profile import get_profile
from core.config import AppConfig
from core.llm_client import LLMClient
from core.mailer import send_email
from core.news_models import PHASE_COLORS, EnrichedOpportunity, NewsItem
from core.sql_connector import SQLConnector
from pipeline.country_config import configured_countries, country_iso2, get_country_output_language, get_country_regions
from pipeline.digest_composer import compose_share_email
from pipeline.lead_pipeline import persist_enriched_opportunity
from pipeline.opportunity_enricher import enrich_single_article
from pipeline.opportunity_triage import VALID_PRIORITIES, valid_sectors
from webapp.auth import PUBLIC_PATHS, auth_configured, current_user, router as auth_router, signed_in_user
from webapp.team import all_team_contacts, get_contacts_for_country

logger = logging.getLogger(__name__)

# ── Globals initialised in lifespan ──────────────────────────────────────────
_cfg: AppConfig | None = None
_llm: LLMClient  | None = None
_db:  SQLConnector | None = None
_contact_finder: ClayContactFinder | None = None

# Dedicated pool for Clay/LLM calls (contact search + email/phone lookups),
# used instead of asyncio's default executor. The default one is sized
# min(32, os.cpu_count() + 4) -- fine for CPU-bound work, but this is pure
# network wait (a single phone lookup alone can take up to 5 minutes), so
# tying its concurrency to core count starves it under real load: a few
# concurrent searches can fill the default pool and leave a "Get email"
# click queued behind them indefinitely, which looks exactly like a hang.
# 20 is deliberately generous -- idle threads waiting on I/O are cheap.
_io_executor = concurrent.futures.ThreadPoolExecutor(max_workers=20, thread_name_prefix="clay-io")

# In-memory, single-process only -- fine at today's scale (one uvicorn
# process, no --workers, see Dockerfile). Current search stage per
# enrichment_id, polled by GET /contacts/{id}/progress while a search is
# in flight; entry is removed once the search finishes either way.
_search_progress: dict[int, str] = {}

# (contact_id, field) pairs currently being looked up -- reopening the
# contacts modal mid-lookup used to show a fresh, clickable button (the row
# hasn't been written yet), letting a re-click fire a second concurrent
# search for the same contact. Checked/added/removed in
# _enrich_one_contact_field.
_enrich_in_flight: set[tuple[int, str]] = set()

_COUNTRIES: dict[str, str] = configured_countries()
_DEFAULT_COUNTRY = next(iter(_COUNTRIES))

_PHASE_COLORS: dict[str, str] = PHASE_COLORS

_PRIORITY_COLORS: dict[str, str] = {
    "high":   "#2E7D32",
    "medium": "#C8900A",
    "low":    "#94A3B8",
}

# Sort key for "priority first, then most recent" ordering — higher = better.
# COALESCE(e.priority, n.priority) so an enriched row's final rechecked tag
# wins over the original triage tag once one exists.
_PRIORITY_RANK_SQL = """
    CASE COALESCE(e.priority, n.priority)
        WHEN 'High' THEN 3
        WHEN 'Medium' THEN 2
        WHEN 'Low' THEN 1
        ELSE 0
    END
"""

_PAGE_SIZE = 24

# Two-letter ISO code as the opportunity-id prefix, e.g. "ES-123".
_COUNTRY_PREFIX: dict[str, str] = {code: country_iso2(code) for code in _COUNTRIES}


# ── App setup ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cfg, _llm, _db, _contact_finder
    _cfg = AppConfig.from_env()
    _llm = LLMClient(_cfg)
    _db  = SQLConnector()
    templates.env.globals["profile"]       = get_profile()
    templates.env.globals["team_contacts"] = all_team_contacts()
    # Contact-finder config (Clay routine IDs, Serper key) is intentionally kept
    # out of AppConfig -- read directly here so this optional feature stays
    # fully isolated. Without CLAY_API_KEY it is simply disabled.
    _contact_finder = None
    if os.getenv("CLAY_API_KEY"):
        try:
            _contact_finder = ClayContactFinder(
                clay_api_key               = os.environ["CLAY_API_KEY"],
                azure_api_key              = _cfg.azure_api_key,
                azure_endpoint             = _cfg.azure_endpoint,
                azure_deployment           = _cfg.deployment_scoring,
                domain_routine_id          = os.environ["CLAY_DOMAIN_ROUTINE_ID"],
                enrich_company_routine_id  = os.environ["CLAY_ENRICH_COMPANY_ROUTINE_ID"],
                email_routine_id           = os.environ["CLAY_EMAIL_ROUTINE_ID"],
                phone_routine_id           = os.environ["CLAY_PHONE_ROUTINE_ID"],
                serper_api_key             = os.environ.get("SERPER_API_KEY"),
            )
        except Exception:
            logger.exception("ClayContactFinder init failed -- contact finder will be unavailable")
    else:
        logger.info("CLAY_API_KEY not set -- contact finder disabled")
    logger.info("LeadRadar web app ready")
    yield


_HERE = Path(__file__).parent

app = FastAPI(title="LeadRadar", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
app.include_router(auth_router)
templates = Jinja2Templates(directory=_HERE / "templates")
templates.env.globals["current_user"]    = current_user
templates.env.globals["auth_configured"] = auth_configured
templates.env.globals["country_flag"]    = lambda code: country_iso2(code or "").lower() or "un"


@app.middleware("http")
async def require_sign_in(request: Request, call_next):
    """
    Gate every route behind sign-in, same as Easy Auth already does on the
    deployed container. A no-op once deployed: Easy Auth authenticates the
    request before it ever reaches the app and injects the principal-name
    header, so signed_in_user() returns non-None via that fallback and this
    check passes straight through.
    """
    path = request.url.path
    if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PATHS):
        return await call_next(request)
    if auth_configured() and not signed_in_user(request):
        return RedirectResponse("/login")
    return await call_next(request)


# Registered AFTER require_sign_in above: Starlette's add_middleware() prepends,
# so the last-registered middleware ends up outermost (runs first per request) —
# SessionMiddleware must run before require_sign_in so request.session exists by
# the time the latter checks it.
# Without SESSION_SECRET_KEY a random key is generated: sessions then simply
# reset on restart, which is fine for a local demo.
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET_KEY") or secrets.token_hex(32))


@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    """Liveness probe for container platforms — no DB or LLM access."""
    return JSONResponse({"status": "ok"})


def _phase_color(phase: str | None) -> str:
    return _PHASE_COLORS.get((phase or "").lower(), "#9E9E9E")

templates.env.filters["phase_color"] = _phase_color


def _priority_color(priority: str | None) -> str:
    return _PRIORITY_COLORS.get((priority or "").lower(), "#9E9E9E")

templates.env.filters["priority_color"] = _priority_color


_AVATAR_COLORS = ["#1976D2", "#2E7D32", "#6A1B9A", "#C62828", "#E65100", "#00695C", "#0d1f45", "#1565C0", "#558B2F"]

def _avatar_color(name: str | None) -> str:
    if not name:
        return "#64748b"
    return _AVATAR_COLORS[sum(ord(c) for c in name) % len(_AVATAR_COLORS)]

templates.env.filters["avatar_color"] = _avatar_color


import re as _re
from markupsafe import Markup as _Markup, escape as _escape

# Matches @FirstName or @FirstName LastName where each word starts with a
# capital letter (covers most European names). The trailing space written by
# _pickMention is NOT consumed so it remains in the text after the chip.
_MENTION_RE = _re.compile(
    r'@([A-ZÀ-ÖØ-öø-ÿŁŃŚŹŻĄĘ]\S+(?:\s[A-ZÀ-ÖØ-öø-ÿŁŃŚŹŻĄĘ]\S+)?)'
)

def _render_mentions(text: str | None) -> _Markup:
    """Return HTML with @mentions styled as blue chips (Teams-style)."""
    if not text:
        return _Markup("")
    escaped = str(_escape(text))
    def _replace(m: _re.Match) -> str:
        name = _escape(m.group(1))
        return (
            f'<span style="color:#1558d6;font-weight:600;'
            f'background:#e8f0fe;border-radius:4px;padding:0 3px 1px;'
            f'font-size:0.95em;">@{name}</span>'
        )
    return _Markup(_MENTION_RE.sub(_replace, escaped))

templates.env.filters["render_mentions"] = _render_mentions


# ── Helpers ──────────────────────────────────────────────────────────────────

def _clean(row: dict[str, Any]) -> dict[str, Any]:
    """Replace pandas NaN with None; convert whole-number floats (e.g. 44.0) to int
    so they render as clean CSS ids rather than '44.0'."""
    out = {}
    for k, v in row.items():
        if isinstance(v, float):
            if math.isnan(v):
                out[k] = None
            elif v == int(v):
                out[k] = int(v)
            else:
                out[k] = v
        else:
            out[k] = v
    return out


def _build_where(
    country: str, search: str, filt: str,
    date_from: str = "", date_to: str = "",
    sector: str = "", priority: str = "", region: list[str] | str = (),
    assigned_to: str = "", end_usage: str = "",
) -> tuple[str, list]:
    parts:  list[str] = ["n.country = ?"]
    params: list      = [country]
    if search.strip():
        parts.append("n.title LIKE ?")
        params.append(f"%{search.strip()}%")
    if filt == "enriched":
        parts.append("e.id IS NOT NULL")
    elif filt == "pending":
        parts.append("e.id IS NULL")
    elif filt == "qualified":
        parts.append("e.id IS NOT NULL AND e.qualification = 'Qualified'")
    elif filt == "disqualified":
        parts.append("e.id IS NOT NULL AND e.qualification = 'Disqualified'")
    if date_from:
        parts.append("CAST(n.publication_date AS DATE) >= CAST(? AS DATE)")
        params.append(date_from)
    if date_to:
        parts.append("CAST(n.publication_date AS DATE) <= CAST(? AS DATE)")
        params.append(date_to)
    # Use COALESCE(e.*, n.*) so the filter matches the value actually displayed
    # on the card — the enricher may correct the triage tag, and both enriched
    # and unenriched rows are covered (e.* is NULL for unenriched, COALESCE
    # falls back to n.* automatically).
    if sector:
        parts.append("COALESCE(e.sector, n.sector) = ?")
        params.append(sector)
    if priority:
        parts.append("COALESCE(e.priority, n.priority) = ?")
        params.append(priority)
    _regions = [region] if isinstance(region, str) and region else list(region) if region else []
    if _regions:
        unknown_selected = "Unknown" in _regions
        known_regions    = [r for r in _regions if r != "Unknown"]
        region_conds: list[str] = []
        if known_regions:
            placeholders = ",".join("?" * len(known_regions))
            region_conds.append(f"COALESCE(e.region, n.region) IN ({placeholders})")
            params.extend(known_regions)
        if unknown_selected:
            region_conds.append("COALESCE(e.region, n.region) IS NULL")
        if region_conds:
            parts.append(f"({' OR '.join(region_conds)})")
    if assigned_to == "__unassigned__":
        parts.append("e.id IS NOT NULL")
        parts.append("(e.assigned_to_email IS NULL OR e.assigned_to_email = '')")
    elif assigned_to:
        parts.append("e.assigned_to_email = ?")
        params.append(assigned_to)
    if end_usage:
        parts.append("e.end_usage = ?")
        params.append(end_usage)
    return " AND ".join(parts), params


def _safe_int(val: Any) -> int:
    return 0 if (val is None or (isinstance(val, float) and math.isnan(val))) else int(val)


def _opportunity_code(country: str | None, enrichment_id: int | None) -> str | None:
    """Human-friendly opportunity label, e.g. 'PL-123' — None if not yet enriched."""
    if enrichment_id is None:
        return None
    prefix = _COUNTRY_PREFIX.get(country or "", (country or "??")[:2].upper())
    return f"{prefix}-{int(enrichment_id)}"


def _fetch_enriched_card_ctx(enrichment_id: int) -> dict[str, Any] | None:
    """
    Build the full template context for one enriched_card.html render, from
    core.news_enriched alone (it already stores its own copy of title/url/
    source/publication_date from enrichment time, so no join back to
    core.news_articles is needed). Shared by the index page, the on-demand enrich
    route, and the link/unlink/group routes so every card looks the same
    regardless of which action last touched it.
    """
    df = _db.execute_query(
        """
        SELECT e.id AS enrichment_id, e.source_news_id, e.country, e.title, e.url, e.source_outlet,
               CONVERT(VARCHAR(10), e.publication_date, 23) AS publication_date,
               CONVERT(VARCHAR(10), e.created_at, 23)        AS created_at,
               e.phase, e.company, e.city, e.project_type, e.project_value,
               e.product_fit, e.why_it_matters, e.recommended_action, e.parent_id,
               e.priority, e.sector, e.region,
               e.end_usage, e.english_summary,
               e.assigned_to_email, e.assigned_to_name, e.assigned_by,
               CONVERT(VARCHAR(16), e.assigned_at AT TIME ZONE 'UTC', 120) AS assigned_at,
               e.qualification, e.qualified_by,
               CONVERT(VARCHAR(16), e.qualified_at AT TIME ZONE 'UTC', 120) AS qualified_at,
               (
                 SELECT COUNT(*) FROM core.news_enriched g
                 WHERE g.id <> e.id
                   AND (g.parent_id = COALESCE(e.parent_id, e.id) OR g.id = COALESCE(e.parent_id, e.id))
               ) AS linked_count
        FROM core.news_enriched e
        WHERE e.id = ?
        """,
        (enrichment_id,),
    )
    if df.empty:
        return None

    row = _clean(df.iloc[0].to_dict())
    return {
        "id":                 row["source_news_id"],   # matches news_articles.id — used for #article-{{ id }} DOM targeting
        "enrichment_id":      row["enrichment_id"],
        "title":              row["title"],
        "url":                row["url"],
        "source_outlet":      row["source_outlet"],
        "publication_date":   row["publication_date"],
        "created_at":         row["created_at"],
        "country":            row["country"],
        "phase":              row["phase"],
        "company":            row["company"],
        "city":               row["city"],
        "project_type":       row["project_type"],
        "project_value":      row["project_value"],
        "product_fit":       row["product_fit"],
        "why_it_matters":     row["why_it_matters"],
        "recommended_action": row["recommended_action"],
        "priority":           row["priority"],
        "sector":             row["sector"],
        "region":             row["region"],
        "end_usage":          row.get("end_usage"),
        "english_summary":    row.get("english_summary"),
        "output_language":    get_country_output_language(row["country"]),
        "assigned_to_email":  row.get("assigned_to_email"),
        "assigned_to_name":   row.get("assigned_to_name"),
        "assigned_by":        row.get("assigned_by"),
        "assigned_at":        row.get("assigned_at"),
        "qualification":      row.get("qualification"),
        "qualified_by":       row.get("qualified_by"),
        "qualified_at":       row.get("qualified_at"),
        "phase_color":        _phase_color(row["phase"]),
        "parent_id":          _safe_int(row["parent_id"]) if row["parent_id"] else None,
        "linked_count":       _safe_int(row["linked_count"]),
        "opportunity_code":   _opportunity_code(row["country"], row["enrichment_id"]),
        "country_prefix":     _COUNTRY_PREFIX.get(row["country"] or "", (row["country"] or "??")[:2].upper()),
    }


def _to_enriched_opportunity(ctx: dict[str, Any]) -> EnrichedOpportunity:
    """
    Reconstruct an EnrichedOpportunity from a card context dict, so the
    'Send via Email' action can reuse digest_composer's _render_card() —
    the exact same card markup already used in the daily digest emails.
    """
    return EnrichedOpportunity(
        title              = ctx.get("title") or "",
        url                = ctx.get("url") or "",
        source             = ctx.get("source_outlet") or "",
        country            = ctx.get("country") or "",
        published_date     = ctx.get("publication_date") or "",
        phase              = ctx.get("phase") or "Unknown",
        company            = ctx.get("company"),
        city               = ctx.get("city"),
        project_type       = ctx.get("project_type"),
        project_value      = ctx.get("project_value"),
        product_fit       = ctx.get("product_fit") or "",
        why_it_matters     = ctx.get("why_it_matters") or "",
        recommended_action = ctx.get("recommended_action") or "",
        scrape_method      = "",
    )


def _short_title(enrichment_id: int, max_len: int = 55) -> str:
    """Return a truncated title for an enriched opportunity, for use in event notes."""
    try:
        df = _db.execute_query(
            "SELECT TOP 1 title FROM core.news_enriched WHERE id = ?", (enrichment_id,)
        )
        if not df.empty:
            t = (df.iloc[0]["title"] or "").strip()
            return (t[:max_len] + "…") if len(t) > max_len else t
    except Exception:
        pass
    return f"#{enrichment_id}"


def _get_enrichment_country(group_id: int) -> str | None:
    """Look up the country code for a given enrichment group_id."""
    try:
        df = _db.execute_query(
            "SELECT TOP 1 country FROM core.news_enriched WHERE id = ?", (group_id,)
        )
        return df.iloc[0]["country"] if not df.empty else None
    except Exception:
        return None


def _insert_event_note(group_id: int, text: str, author: str | None = None) -> None:
    """Insert a system-generated immutable event note into a group's thread."""
    try:
        country = _get_enrichment_country(group_id)
        _db.execute_non_query(
            "INSERT INTO core.opportunity_notes (group_id, country, author_name, comment_text, note_type) "
            "VALUES (?, ?, ?, ?, 'event')",
            (group_id, country, author, text),
        )
    except Exception:
        logger.exception("Failed to insert event note for group %s", group_id)


def _copy_notes_as_history(from_group_id: int, to_group_id: int) -> None:
    """Copy all non-history notes from one group into another as frozen 'history' entries."""
    try:
        df = _db.execute_query(
            """
            SELECT author_name, comment_text, created_at
            FROM   core.opportunity_notes
            WHERE  group_id = ? AND note_type IN ('comment', 'event')
            ORDER  BY created_at ASC
            """,
            (from_group_id,),
        )
        country = _get_enrichment_country(to_group_id)
        for row in df.to_dict("records"):
            _db.execute_non_query(
                """
                INSERT INTO core.opportunity_notes
                    (group_id, country, author_name, comment_text, note_type, source_group_id, created_at)
                VALUES (?, ?, ?, ?, 'history', ?, ?)
                """,
                (to_group_id, country, row.get("author_name"), row["comment_text"],
                 from_group_id, row["created_at"]),
            )
    except Exception:
        logger.exception("Failed to copy notes from group %s to %s", from_group_id, to_group_id)


def _fetch_notes(group_id: int) -> list[dict[str, Any]]:
    """All notes for one group's thread, oldest first (chat-style)."""
    df = _db.execute_query(
        """
        SELECT id, author_name, comment_text,
               CONVERT(VARCHAR(16), created_at AT TIME ZONE 'UTC', 120) AS created_at,
               updated_at,
               note_type,
               source_group_id
        FROM   core.opportunity_notes
        WHERE  group_id = ?
        ORDER  BY created_at ASC, id ASC
        """,
        (group_id,),
    )
    notes = []
    for r in df.to_dict("records"):
        row = _clean(r)
        row["was_edited"]      = bool(row.get("updated_at"))
        row["note_type"]       = row.get("note_type") or "comment"
        row["source_group_id"] = row.get("source_group_id")
        notes.append(row)
    return notes


def _render_notes_list_html(group_id: int, request: Request) -> str:
    tmpl = templates.get_template("partials/notes_list.html")
    user = current_user(request)
    return tmpl.render({
        "request":              request,
        "notes":                _fetch_notes(group_id),
        "current_user_display": user["display"] if user else "",
    })


def _notes_oob(group_id: int, request: Request) -> str:
    """Out-of-band notes list refresh — append to any response that modifies
    notes (qualify, assign, link, unlink) so the chat auto-updates without
    a page reload when the user is on the group detail page."""
    inner = _render_notes_list_html(group_id, request)
    return f'<div id="notes-list" hx-swap-oob="innerHTML">{inner}</div>'


def _is_group_detail(request: Request) -> bool:
    """True when the HTMX request originated from a /group/ page."""
    return "/group/" in (request.headers.get("HX-Current-URL") or "")


def _children_section_oob(root_id: int, request: Request) -> str:
    """OOB replacement of #children-section with fresh children list + header."""
    df = _db.execute_query(
        "SELECT id FROM core.news_enriched WHERE parent_id = ? ORDER BY publication_date ASC",
        (root_id,),
    )
    children = []
    if not df.empty:
        for cid in df["id"]:
            ctx = _fetch_enriched_card_ctx(int(cid))
            if ctx:
                children.append(ctx)
    tmpl  = templates.get_template("partials/children_section.html")
    inner = tmpl.render({"request": request, "children": children})
    count = len(children)
    badge_cls = "inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-blue-50 border border-blue-100 text-blue-700 text-[11px] font-bold"
    badge_hidden = " hidden" if count == 0 else ""
    return (
        f'<div id="children-section" hx-swap-oob="innerHTML">{inner}</div>'
        f'<span id="linked-count-badge" hx-swap-oob="outerHTML"'
        f' class="{badge_cls}{badge_hidden}">'
        f'<span id="linked-count-num">{count}</span> linked</span>'
    )


def _fetch_assignees(country: str) -> list[dict[str, str]]:
    """Distinct assignees who have at least one assignment for this country."""
    try:
        df = _db.execute_query(
            """
            SELECT DISTINCT assigned_to_email,
                   COALESCE(assigned_to_name, assigned_to_email) AS display_name
            FROM   core.news_enriched
            WHERE  country = ? AND assigned_to_email IS NOT NULL
            ORDER  BY display_name
            """,
            (country,),
        )
        return [
            {"email": r["assigned_to_email"], "name": r["display_name"]}
            for r in df.to_dict("records")
        ]
    except Exception:
        return []


def _fetch_end_usage_options(country: str) -> list[str]:
    """Distinct end_usage values present for this country."""
    try:
        df = _db.execute_query(
            "SELECT DISTINCT end_usage FROM core.news_enriched "
            "WHERE country = ? AND end_usage IS NOT NULL ORDER BY end_usage",
            (country,),
        )
        return [r["end_usage"] for r in df.to_dict("records")]
    except Exception:
        return []


# ── Contacts helpers ─────────────────────────────────────────────────────────

def _group_root_id(enrichment_id: int) -> int:
    """The current representative's id for whatever group enrichment_id belongs to."""
    df = _db.execute_query("SELECT id, parent_id FROM core.news_enriched WHERE id = ?", (enrichment_id,))
    if df.empty:
        return enrichment_id
    row = _clean(df.iloc[0].to_dict())
    return int(row["parent_id"]) if row.get("parent_id") else enrichment_id


def _group_member_ids(enrichment_id: int) -> list[int]:
    """Every core.news_enriched id currently in enrichment_id's group (root + children)."""
    root_id = _group_root_id(enrichment_id)
    df = _db.execute_query(
        "SELECT id FROM core.news_enriched WHERE id = ? OR parent_id = ?", (root_id, root_id)
    )
    ids = [int(r) for r in df["id"]] if not df.empty else []
    return ids or [enrichment_id]


def _clean_contact_row(row: dict[str, Any]) -> dict[str, Any]:
    """_clean() converts float NaN -> None but leaves NULL datetime columns as
    pandas.NaT, which is truthy (bool(pd.NaT) is True, unlike None) -- without
    this, email_enriched_at/phone_enriched_at read back as "truthy" even when
    an email/phone lookup was never attempted, making contact_row.html render
    the permanent "not found" state instead of the "Get email"/"Get phone"
    button for contacts that were simply never tried. Scoped to this feature's
    own helper rather than changing the shared _clean() used everywhere else
    in this file."""
    row = _clean(row)
    for key in ("email_enriched_at", "phone_enriched_at", "created_at"):
        val = row.get(key)
        if val is not None and val != val:   # NaN/NaT are the only values unequal to themselves
            row[key] = None
    # Surfaces even on a fresh modal reopen, not just the enrich route's own
    # response -- see _enrich_in_flight's module-level comment for why this
    # matters (prevents a second concurrent search for the same contact).
    row["email_in_flight"] = (row.get("id"), "email") in _enrich_in_flight
    row["phone_in_flight"] = (row.get("id"), "phone") in _enrich_in_flight
    return row


def _fetch_group_contacts(enrichment_id: int) -> tuple[bool, str | None, list[dict[str, Any]]]:
    """
    (searched, domain, contacts) for enrichment_id's whole group.

    Only ``is_active = 1`` rows are read -- each search (initial or a rep's
    domain-override correction) deactivates whatever was previously active
    for this enrichment_id first, then inserts the fresh outcome as active
    (see _persist_search_outcome). Older rows are kept, just flagged
    inactive, as an audit trail.

    A search that found no contacts still leaves one active row behind: one
    with ``full_name IS NULL`` that holds just the domain that was searched
    (or NULL if no domain was ever resolved). That's what makes the domain
    visible on reopen even when nothing was found -- filtered out of
    ``contacts`` below, but still the source of ``domain``.

    Contacts are stored keyed to the SPECIFIC article that was searched, not
    the group root (see sql/create_table_opportunity_contacts.sql for why --
    root re-assignment on group merges would otherwise silently orphan rows).
    This reads across every current member so contacts found on any article
    now in the group still show up here.

    `searched` reuses the existing opportunity_notes event-log rather than a
    new column: a "Contact search run" event already gets written on every
    search attempt (see contacts_search()), so checking for that note also
    means the search shows up in the group's activity timeline for free.
    """
    root_id     = _group_root_id(enrichment_id)
    member_ids  = _group_member_ids(enrichment_id)

    searched = False
    try:
        note_df = _db.execute_query(
            "SELECT TOP 1 id FROM core.opportunity_notes "
            "WHERE group_id = ? AND note_type = 'event' AND comment_text LIKE 'Contact search run%'",
            (root_id,),
        )
        searched = not note_df.empty
    except Exception:
        logger.exception("Failed to check contact-search history for group %d", root_id)

    domain: str | None = None
    contacts: list[dict[str, Any]] = []
    try:
        placeholders = ",".join("?" * len(member_ids))
        df = _db.execute_query(
            f"SELECT * FROM core.opportunity_contacts WHERE enrichment_id IN ({placeholders}) "
            f"AND is_active = 1 ORDER BY id",
            tuple(member_ids),
        )
        if not df.empty:
            rows = [_clean_contact_row(r) for r in df.to_dict("records")]
            contacts = [r for r in rows if r.get("full_name")]
            domain = next((r["company_domain"] for r in rows if r.get("company_domain")), None)
    except Exception:
        logger.exception("Failed to fetch contacts for group %d", root_id)

    return searched, domain, contacts


def _fetch_contact_row(contact_id: int) -> dict[str, Any] | None:
    df = _db.execute_query("SELECT * FROM core.opportunity_contacts WHERE id = ?", (contact_id,))
    if df.empty:
        return None
    return _clean_contact_row(df.iloc[0].to_dict())


def _persist_search_outcome(
    enrichment_id: int, country: str, domain: str | None, contacts: list, actor: str | None,
) -> None:
    """Record one search's outcome (initial or a domain-override correction)
    as the new active state for enrichment_id: deactivate whatever was
    previously active, then insert the fresh result as active. Older rows
    are kept, just flagged inactive -- an audit trail of past searches, not
    deleted.

    If no contacts were found, one row is still inserted with
    ``full_name = NULL``, carrying just ``domain`` (possibly also NULL, if
    resolution itself found nothing) -- this is what keeps the domain
    visible on reopen even for a 0-contact result. See
    _fetch_group_contacts, which reads this back.
    """
    try:
        _db.execute_non_query(
            "UPDATE core.opportunity_contacts SET is_active = 0 WHERE enrichment_id = ? AND is_active = 1",
            (enrichment_id,),
        )
    except Exception:
        logger.exception("Failed to deactivate prior contacts for enrichment %d", enrichment_id)

    if not contacts:
        try:
            _db.execute_non_query(
                """
                INSERT INTO core.opportunity_contacts
                    (enrichment_id, country, full_name, company_domain, found_by, is_active)
                VALUES (?, ?, NULL, ?, ?, 1)
                """,
                (enrichment_id, country, domain, actor),
            )
        except Exception:
            logger.exception("Failed to persist empty search outcome for enrichment %d", enrichment_id)
        return

    for c in contacts:
        try:
            _db.execute_non_query(
                """
                INSERT INTO core.opportunity_contacts
                    (enrichment_id, country, full_name, job_title, company_name,
                     company_domain, linkedin_url, city, source, found_by, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (enrichment_id, country, c.name, c.title, c.company_name,
                 c.domain, c.linkedin_url, c.city, c.source, actor),
            )
        except Exception:
            logger.exception("Failed to persist contact %r for enrichment %d", c.name, enrichment_id)


def _clean_domain_input(raw: str) -> str:
    """Light normalization for a rep-typed domain: strip scheme, "www.", and
    any trailing path/slash, so "https://www.example.com/" and "example.com"
    both land on the same known_domain value."""
    d = (raw or "").strip()
    for prefix in ("https://", "http://"):
        if d.lower().startswith(prefix):
            d = d[len(prefix):]
            break
    if d.lower().startswith("www."):
        d = d[4:]
    return d.split("/")[0].strip()


# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(
    request:    Request,
    country:    str = "",
    page:       int = 1,
    search:     str = "",
    filt:       str = "all",   # all | enriched | pending | qualified | disqualified
    date_from:  str = "",      # YYYY-MM-DD lower bound for publication_date
    date_to:    str = "",      # YYYY-MM-DD upper bound for publication_date
    sector:     str = "",      # triage tag (core.news_articles.sector)
    priority:   str = "",      # triage tag (core.news_articles.priority)
    region:     list[str] = Query(default=[]),  # multiple regions allowed
    assigned_to: str = "",     # filter by assignee email (or "__unassigned__")
    end_usage:  str = "",      # filter by end_usage (enriched tab only)
    cols:       int = 2,       # card grid columns: 1 or 2
):
    if country not in _COUNTRIES:
        country = _DEFAULT_COUNTRY
    offset        = (page - 1) * _PAGE_SIZE
    where, params = _build_where(country, search, filt, date_from, date_to, sector, priority, region, assigned_to, end_usage)

    count_df = _db.execute_query(
        f"""
        SELECT COUNT(*) AS n
        FROM   core.news_articles n
        LEFT JOIN core.news_enriched e ON e.source_news_id = n.id
        WHERE  {where}
        """,
        tuple(params),
    )
    total       = int(count_df.iloc[0]["n"]) if not count_df.empty else 0
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    # Primary sort: collection date descending (most recently collected first, so
    # today's pipeline run sits at the top). Secondary: enriched before pending
    # on the "all" tab (so already-processed leads don't get buried by today's
    # new candidates). Tertiary: priority rank, then publication date.
    if filt == "all":
        order_by = (
            f"CAST(n.created_at AS DATE) DESC, "
            f"CASE WHEN e.id IS NOT NULL THEN 0 ELSE 1 END, "
            f"{_PRIORITY_RANK_SQL} DESC, n.publication_date DESC"
        )
    else:
        order_by = (
            f"CAST(n.created_at AS DATE) DESC, "
            f"{_PRIORITY_RANK_SQL} DESC, n.publication_date DESC"
        )

    df = _db.execute_query(
        f"""
        SELECT
            n.id,
            n.country,
            n.title,
            n.source_outlet,
            n.url,
            CONVERT(VARCHAR(10), n.publication_date, 23)  AS publication_date,
            CONVERT(VARCHAR(10), n.created_at, 23)        AS created_at,
            COALESCE(e.priority, n.priority)              AS priority,
            COALESCE(e.sector, n.sector)                  AS sector,
            COALESCE(e.region, n.region)                  AS region,
            CASE WHEN CAST(n.created_at AS DATE) = CAST(GETDATE() AS DATE) THEN 1 ELSE 0 END AS is_new,
            e.id         AS enrichment_id,
            e.phase,
            e.company,
            e.city,
            e.project_type,
            e.project_value,
            e.product_fit,
            e.why_it_matters,
            e.recommended_action,
            e.end_usage,
            e.english_summary,
            e.parent_id,
            e.assigned_to_email, e.assigned_to_name, e.assigned_by,
            CONVERT(VARCHAR(16), e.assigned_at AT TIME ZONE 'UTC', 120) AS assigned_at,
            e.qualification, e.qualified_by,
            CONVERT(VARCHAR(16), e.qualified_at AT TIME ZONE 'UTC', 120) AS qualified_at,
            (
              SELECT COUNT(*) FROM core.news_enriched g
              WHERE e.id IS NOT NULL
                AND g.id <> e.id
                AND (g.parent_id = COALESCE(e.parent_id, e.id) OR g.id = COALESCE(e.parent_id, e.id))
            ) AS linked_count
        FROM   core.news_articles n
        LEFT JOIN core.news_enriched e ON e.source_news_id = n.id
        WHERE  {where}
        ORDER  BY {order_by}
        OFFSET ? ROWS FETCH NEXT ? ROWS ONLY
        """,
        tuple(params + [offset, _PAGE_SIZE]),
    )
    articles = []
    for r in df.to_dict("records"):
        row = _clean(r)
        row["phase_color"]        = _phase_color(row.get("phase"))
        row["opportunity_code"]   = _opportunity_code(row.get("country"), row.get("enrichment_id"))
        row["country_prefix"]     = _COUNTRY_PREFIX.get(row.get("country") or "", (row.get("country") or "??")[:2].upper())
        row["linked_count"]       = _safe_int(row.get("linked_count"))
        row["is_new"]             = bool(row.get("is_new"))
        row["output_language"]    = get_country_output_language(row.get("country") or "")
        # Assignment + qualification — included in SELECT, already present in row via _clean()
        # but explicitly default to None so templates can safely access them:
        for _f in ("assigned_to_email", "assigned_to_name", "assigned_by", "assigned_at",
                   "qualification", "qualified_by", "qualified_at"):
            row.setdefault(_f, None)
        articles.append(row)

    # Stats — always across full country/search/date/triage-filter scope, ignoring enriched/pending tab
    stats_where, stats_params = _build_where(country, search, "all", date_from, date_to, sector, priority, region, assigned_to, end_usage)
    stats_df = _db.execute_query(
        f"""
        SELECT
            COUNT(*)                                                              AS total,
            SUM(CASE WHEN e.id IS NOT NULL THEN 1 ELSE 0 END)                   AS enriched,
            SUM(CASE WHEN LOWER(e.phase) = 'awarded'     THEN 1 ELSE 0 END)     AS awarded,
            SUM(CASE WHEN LOWER(e.phase) = 'tender'      THEN 1 ELSE 0 END)     AS tender,
            SUM(CASE WHEN LOWER(e.phase) = 'planning'    THEN 1 ELSE 0 END)     AS planning,
            SUM(CASE WHEN LOWER(e.phase) = 'permitting'  THEN 1 ELSE 0 END)     AS permitting,
            SUM(CASE WHEN LOWER(e.phase) = 'announced'   THEN 1 ELSE 0 END)     AS announced,
            SUM(CASE WHEN LOWER(e.phase) = 'construction' THEN 1 ELSE 0 END)    AS construction
        FROM core.news_articles n
        LEFT JOIN core.news_enriched e ON e.source_news_id = n.id
        WHERE {stats_where}
        """,
        tuple(stats_params),
    )
    if not stats_df.empty:
        sr           = {k: _safe_int(v) for k, v in stats_df.iloc[0].to_dict().items()}
        stats: dict  = {
            "total":    sr["total"],
            "enriched": sr["enriched"],
            "pending":  sr["total"] - sr["enriched"],
            "phases": [
                ("Awarded",      sr["awarded"],      "#2E7D32"),
                ("Tender",       sr["tender"],       "#6A1B9A"),
                ("Planning",     sr["planning"],     "#1976D2"),
                ("Permitting",   sr["permitting"],   "#00838F"),
                ("Announced",    sr["announced"],    "#607D8B"),
                ("Construction", sr["construction"], "#E65100"),
            ],
        }
    else:
        stats = {"total": 0, "enriched": 0, "pending": 0, "phases": []}

    return templates.TemplateResponse(request, "index.html", {
        "articles":          articles,
        "country":           country,
        "countries":         _COUNTRIES,
        "search":            search,
        "filt":              filt,
        "page":              page,
        "total_pages":       total_pages,
        "total":             total,
        "date_from":         date_from,
        "date_to":           date_to,
        "stats":             stats,
        "sector":            sector,
        "priority":          priority,
        "region":            region,
        "assigned_to":       assigned_to,
        "end_usage":         end_usage,
        "cols":              cols,
        "sector_options":    valid_sectors(),
        "priority_options":  VALID_PRIORITIES,
        "region_options":    get_country_regions(country) + ("Unknown",),
        "assignee_options":  _fetch_assignees(country),
        "end_usage_options": _fetch_end_usage_options(country),
    })


@app.post("/enrich/{article_id}", response_class=HTMLResponse)
async def enrich_article(article_id: int, request: Request):
    """Scrape + LLM-enrich one article. Returns an HTMX card partial."""
    df = _db.execute_query(
        """
        SELECT id, country, title, source_outlet, url,
               CONVERT(VARCHAR(10), publication_date, 23) AS publication_date
        FROM   core.news_articles
        WHERE  id = ?
        """,
        (article_id,),
    )
    if df.empty:
        raise HTTPException(status_code=404, detail="Article not found")

    row  = _clean(df.iloc[0].to_dict())
    item = NewsItem(
        title   = row.get("title")        or "",
        summary = "",   # not stored in news_articles; enricher falls back to scraped text
        url     = row.get("url")          or "",
        date    = row.get("publication_date") or "",
        source  = row.get("source_outlet") or "",
        country = row.get("country")      or "",
    )

    try:
        loop = asyncio.get_running_loop()
        opp  = await loop.run_in_executor(
            None,
            partial(enrich_single_article, item, _llm, row.get("country", "")),
        )
    except Exception as exc:
        logger.exception("Enrichment failed for article %d", article_id)
        return templates.TemplateResponse(request, "partials/error_card.html", {
            "article_id":       article_id,
            "title":            item.title,
            "url":              item.url,
            "source_outlet":    item.source,
            "publication_date": item.date,
        })

    new_enrichment_id = None
    try:
        persist_enriched_opportunity(_db, article_id, opp, _cfg)
    except Exception:
        logger.exception("DB write failed for enriched article %d — result still returned", article_id)
    # Always look up the id — covers both a fresh INSERT and the case where the
    # INSERT failed because the article was already enriched (duplicate source_news_id
    # from a previous daily pipeline run).
    try:
        id_lookup = _db.execute_query(
            "SELECT TOP 1 id FROM core.news_enriched WHERE source_news_id = ? ORDER BY id DESC",
            (article_id,),
        )
        if not id_lookup.empty:
            new_enrichment_id = int(id_lookup.iloc[0]["id"])
    except Exception:
        logger.exception("DB id lookup failed for article %d", article_id)

    # Re-fetch from the DB so the card carries enrichment_id / opportunity_code /
    # parent_id / linked_count — the same shape every other route uses. Falls back
    # to building the context straight from `opp` only if persistence failed, in
    # which case there's nothing in the DB to link yet (enrichment_id stays None).
    article_ctx = _fetch_enriched_card_ctx(new_enrichment_id) if new_enrichment_id else None
    if article_ctx is None:
        article_ctx = {
            "id":                 article_id,
            "title":              opp.title,
            "url":                opp.url,
            "source_outlet":      opp.source,
            "publication_date":   opp.published_date,
            "country":            opp.country,
            "phase":              opp.phase,
            "company":            opp.company,
            "city":               opp.city,
            "project_type":       opp.project_type,
            "project_value":      opp.project_value,
            "product_fit":       opp.product_fit,
            "why_it_matters":     opp.why_it_matters,
            "recommended_action": opp.recommended_action,
            "priority":           opp.priority,
            "sector":             opp.sector,
            "region":             opp.region,
            "phase_color":        _phase_color(opp.phase),
            "enrichment_id":      None,
            "parent_id":          None,
            "linked_count":       0,
            "opportunity_code":   None,
            "country_prefix":     _COUNTRY_PREFIX.get(opp.country or "", (opp.country or "??")[:2].upper()),
        }
    # Render directly — avoids any TemplateResponse API version sensitivity
    tmpl    = templates.get_template("partials/enriched_card.html")
    content = tmpl.render({"request": request, "article": article_ctx})

    # If the user triggered enrichment from the "To Enrich" (pending) tab,
    # delete the card from the list and show a toast linking to the Enriched tab.
    caller_url = request.headers.get("HX-Current-URL", "")
    caller_qs  = parse_qs(urlparse(caller_url).query)
    caller_filt = (caller_qs.get("filt") or ["all"])[0]
    if caller_filt == "pending":
        country = (caller_qs.get("country") or [""])[0]
        enriched_url = caller_url.replace("filt=pending", "filt=enriched") if "filt=pending" in caller_url \
            else f"/?country={country}&filt=enriched"
        trigger_data = json.dumps({
            "showEnrichToast": {
                "code":    article_ctx.get("opportunity_code") or f"#{new_enrichment_id}",
                "title":   article_ctx.get("title", ""),
                "phase":   article_ctx.get("phase") or "Unknown",
                "color":   article_ctx.get("phase_color") or "#9E9E9E",
                "city":    article_ctx.get("city") or "",
                "company": article_ctx.get("company") or "",
                "url":     enriched_url,
            }
        })
        resp = HTMLResponse(content="", status_code=200)
        resp.headers["HX-Reswap"]  = "delete"
        resp.headers["HX-Trigger"] = trigger_data
        return resp

    # All-tab enrich: return card + fire toast via HX-Trigger (same event as
    # pending tab) so the htmx:afterSwap handler never needs to show toasts.
    caller_country = (caller_qs.get("country") or [_DEFAULT_COUNTRY])[0]
    enriched_url = f"/?country={caller_country}&filt=enriched"
    trigger_data = json.dumps({
        "showEnrichToast": {
            "code":    article_ctx.get("opportunity_code") or f"#{new_enrichment_id}",
            "title":   article_ctx.get("title", ""),
            "phase":   article_ctx.get("phase") or "Unknown",
            "color":   article_ctx.get("phase_color") or "#9E9E9E",
            "city":    article_ctx.get("city") or "",
            "company": article_ctx.get("company") or "",
            "url":     enriched_url,
        }
    })
    resp = HTMLResponse(content=content)
    resp.headers["HX-Trigger"] = trigger_data
    return resp


_ACTIVITY_SQL_FULL = """
    SELECT TOP 40
        group_id, country, author_name, comment_text, created_at, opportunity_title
    FROM (
        -- Event notes written by the new tracking system
        SELECT
            n.group_id,
            n.country,
            n.author_name,
            n.comment_text,
            n.created_at,
            ISNULL(e.title, CAST(n.group_id AS NVARCHAR(20))) AS opportunity_title
        FROM  core.opportunity_notes n
        LEFT  JOIN core.news_enriched e ON e.id = n.group_id
        WHERE n.note_type = 'event'

        UNION ALL

        -- Historical assignments stored on news_enriched (backfill).
        -- NOT EXISTS guard avoids duplicates once the event-note system is running.
        SELECT
            e.id,
            e.country,
            e.assigned_by,
            CONCAT('Assigned to ', ISNULL(e.assigned_to_name, ''),
                   ' (', ISNULL(e.assigned_to_email, ''), ')'),
            e.assigned_at,
            e.title
        FROM  core.news_enriched e
        WHERE e.assigned_at IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM core.opportunity_notes n2
              WHERE n2.group_id = e.id AND n2.note_type = 'event'
                AND n2.comment_text LIKE 'Assigned to%'
          )

        UNION ALL

        -- Historical qualifications stored on news_enriched (backfill).
        SELECT
            e.id,
            e.country,
            e.qualified_by,
            CONCAT('Marked as ', e.qualification),
            e.qualified_at,
            e.title
        FROM  core.news_enriched e
        WHERE e.qualified_at IS NOT NULL
          AND e.qualification IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM core.opportunity_notes n2
              WHERE n2.group_id = e.id AND n2.note_type = 'event'
                AND n2.comment_text LIKE 'Marked as%'
          )

        UNION ALL

        -- Historical link events: items currently in a group (backfill).
        -- Uses enriched_at as proxy timestamp; skipped once a real link event note exists.
        -- Correlated subquery retrieves the author from the group's link event note if present.
        SELECT
            e.parent_id                                                           AS group_id,
            e.country,
            (SELECT TOP 1 n_lnk.author_name
             FROM   core.opportunity_notes n_lnk
             WHERE  n_lnk.group_id    = e.parent_id
               AND  n_lnk.note_type   = 'event'
               AND  n_lnk.comment_text LIKE '%linked%'
             ORDER BY n_lnk.created_at DESC)                                     AS author_name,
            CONCAT('#', CAST(e.id AS NVARCHAR(10)), ' linked — ',
                   LEFT(ISNULL(p.title, 'group #' + CAST(p.id AS NVARCHAR(10))), 48)) AS comment_text,
            e.created_at,
            p.title                                                               AS opportunity_title
        FROM  core.news_enriched e
        JOIN  core.news_enriched p ON p.id = e.parent_id
        WHERE e.parent_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM core.opportunity_notes n2
              WHERE n2.group_id = e.parent_id AND n2.note_type = 'event'
                AND n2.comment_text LIKE '%linked%'
          )
    ) AS all_events
    {country_clause}
    ORDER BY created_at DESC
"""

_ACTIVITY_SQL_HISTORY_ONLY = """
    SELECT TOP 40
        group_id, country, author_name, comment_text, created_at, opportunity_title
    FROM (
        SELECT
            e.id                                                  AS group_id,
            e.country,
            e.assigned_by                                         AS author_name,
            CONCAT('Assigned to ', ISNULL(e.assigned_to_name, ''),
                   ' (', ISNULL(e.assigned_to_email, ''), ')')   AS comment_text,
            e.assigned_at                                         AS created_at,
            e.title                                               AS opportunity_title
        FROM  core.news_enriched e
        WHERE e.assigned_at IS NOT NULL

        UNION ALL

        SELECT
            e.id,
            e.country,
            e.qualified_by,
            CONCAT('Marked as ', e.qualification),
            e.qualified_at,
            e.title
        FROM  core.news_enriched e
        WHERE e.qualified_at IS NOT NULL
          AND e.qualification IS NOT NULL

        UNION ALL

        SELECT
            e.parent_id,
            e.country,
            (SELECT TOP 1 n_lnk.author_name
             FROM   core.opportunity_notes n_lnk
             WHERE  n_lnk.group_id    = e.parent_id
               AND  n_lnk.note_type   = 'event'
               AND  n_lnk.comment_text LIKE '%linked%'
             ORDER BY n_lnk.created_at DESC),
            CONCAT('#', CAST(e.id AS NVARCHAR(10)), ' linked — ',
                   LEFT(ISNULL(p.title, 'group #' + CAST(p.id AS NVARCHAR(10))), 48)),
            e.created_at,
            p.title
        FROM  core.news_enriched e
        JOIN  core.news_enriched p ON p.id = e.parent_id
        WHERE e.parent_id IS NOT NULL
    ) AS h
    {country_clause}
    ORDER BY created_at DESC
"""


@app.get("/activity", response_class=HTMLResponse)
async def activity_feed(request: Request, country: str = ""):
    """Recent activity: event notes + historical assignments/qualifications from news_enriched."""
    from datetime import datetime, timezone
    _tmpl_ctx = {"request": request, "events": []}
    try:
        country_upper  = country.strip().upper() if country.strip() else ""
        country_clause = "WHERE country = ?" if country_upper else ""
        sql_params     = (country_upper,) if country_upper else ()
        try:
            df = _db.execute_query(
                _ACTIVITY_SQL_FULL.format(country_clause=country_clause),
                sql_params,
            )
        except Exception:
            # note_type column may not exist yet — fall back to history-only
            logger.warning("Activity full query failed, using history-only fallback")
            df = _db.execute_query(
                _ACTIVITY_SQL_HISTORY_ONLY.format(country_clause=country_clause),
                sql_params,
            )

        events = []
        if not df.empty:
            now = datetime.now(timezone.utc)
            for row in df.to_dict("records"):
                created = row["created_at"]
                if created is not None:
                    if hasattr(created, "tzinfo") and created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                    secs = max(0, int((now - created).total_seconds()))
                    if secs < 60:
                        time_str = "just now"
                    elif secs < 3600:
                        time_str = f"{secs // 60}m ago"
                    elif secs < 86400:
                        time_str = f"{secs // 3600}h ago"
                    else:
                        time_str = f"{secs // 86400}d ago"
                else:
                    time_str = ""
                _a = row.get("author_name")
                author_name = "" if (_a is None or (isinstance(_a, float) and math.isnan(_a))) else str(_a)
                events.append({
                    "group_id":          row.get("group_id"),
                    "country":           (row.get("country") or "").upper(),
                    "author_name":       author_name,
                    "comment_text":      row.get("comment_text") or "",
                    "time_str":          time_str,
                    "opportunity_title": row.get("opportunity_title") or f"#{row.get('group_id', '?')}",
                })
        _tmpl_ctx["events"] = events
    except Exception:
        logger.exception("Failed to load activity feed")
    return templates.TemplateResponse(request, "partials/activity_feed.html", _tmpl_ctx)


@app.get("/search-link-candidates", response_class=HTMLResponse)
async def search_link_candidates(
    request:            Request,
    current_id:         int,    # enrichment_id of the card the user is linking FROM
    current_article_id: int,    # news_articles.id of that same card — needed for the HTMX target
    country:            str,
    mode:               str = "title",   # "id" | "title" — set explicitly by which modal tab is active
    q:                  str = "",
):
    """Live search box backing the 'Link to another opportunity' modal."""
    cur_df = _db.execute_query("SELECT id, parent_id FROM core.news_enriched WHERE id = ?", (current_id,))
    if cur_df.empty:
        return HTMLResponse('<div class="p-3 text-[11px] text-slate-400">Opportunity not found.</div>')
    cur_row  = _clean(cur_df.iloc[0].to_dict())
    cur_root = int(cur_row["parent_id"]) if cur_row.get("parent_id") else int(cur_row["id"])

    q = q.strip()
    if mode == "id":
        if not q.isdigit():
            return HTMLResponse('<div class="p-3 text-[11px] text-slate-400">Type the numeric ID to search…</div>')
        where, params = "e.id = ? AND e.country = ?", (int(q), country)
    else:
        if not q:
            return HTMLResponse('<div class="p-3 text-[11px] text-slate-400">Type a title to search…</div>')
        where, params = "e.title LIKE ? AND e.country = ?", (f"%{q}%", country)

    df = _db.execute_query(
        f"""
        SELECT e.id, e.parent_id, e.title, e.phase, e.city,
               CONVERT(VARCHAR(10), e.publication_date, 23) AS publication_date
        FROM   core.news_enriched e
        WHERE  {where}
        ORDER  BY e.publication_date DESC
        """,
        params,
    )

    results = []
    for r in df.to_dict("records"):
        row      = _clean(r)
        row_id   = int(row["id"])
        row_root = int(row["parent_id"]) if row.get("parent_id") else row_id
        if row_id == current_id or row_root == cur_root:
            continue   # itself, or already in the same group
        row["phase_color"]      = _phase_color(row.get("phase"))
        row["opportunity_code"] = _opportunity_code(country, row_id)
        results.append(row)

    tmpl = templates.get_template("partials/link_search_results.html")
    return HTMLResponse(content=tmpl.render({
        "request":            request,
        "results":            results[:8],
        "current_id":         current_id,
        "current_article_id": current_article_id,
    }))


@app.post("/link/{from_id}/{to_id}", response_class=HTMLResponse)
async def link_opportunities(from_id: int, to_id: int, request: Request):
    """
    Merge the groups containing ``from_id`` and ``to_id``. The oldest
    publication_date across every member of both groups becomes the single
    representative (parent_id = NULL); everyone else points at it directly —
    never through an intermediate member, so the structure stays one level deep
    no matter how many times opportunities get linked together.
    """
    df = _db.execute_query(
        "SELECT id, parent_id FROM core.news_enriched WHERE id IN (?, ?)",
        (from_id, to_id),
    )
    rows_by_id = {int(r["id"]): _clean(r) for r in df.to_dict("records")}
    if from_id not in rows_by_id or to_id not in rows_by_id:
        raise HTTPException(status_code=404, detail="One or both opportunities not found")

    from_row, to_row = rows_by_id[from_id], rows_by_id[to_id]
    root_from = int(from_row["parent_id"]) if from_row.get("parent_id") else from_id
    root_to   = int(to_row["parent_id"])   if to_row.get("parent_id")   else to_id

    if root_from != root_to:
        group_df = _db.execute_query(
            "SELECT id, CONVERT(VARCHAR(10), publication_date, 23) AS publication_date "
            "FROM   core.news_enriched WHERE id IN (?, ?) OR parent_id IN (?, ?)",
            (root_from, root_to, root_from, root_to),
        )
        members     = [_clean(r) for r in group_df.to_dict("records")]
        new_root    = min(members, key=lambda m: (m["publication_date"] or "9999-99-99", int(m["id"])))
        new_root_id = int(new_root["id"])

        for m in members:
            mid = int(m["id"])
            if mid == new_root_id:
                _db.execute_non_query("UPDATE core.news_enriched SET parent_id = NULL WHERE id = ?", (mid,))
            else:
                _db.execute_non_query("UPDATE core.news_enriched SET parent_id = ? WHERE id = ?", (new_root_id, mid))

        actor = signed_in_user(request)
        merged_in = root_from if new_root_id == root_to else root_to
        _insert_event_note(
            new_root_id,
            f"#{merged_in} linked into this group",
            author=actor,
        )

    updated_ctx = _fetch_enriched_card_ctx(from_id)
    if updated_ctx is None:
        raise HTTPException(status_code=404, detail="Opportunity disappeared during link")

    tmpl    = templates.get_template("partials/enriched_card.html")
    content = tmpl.render({"request": request, "article": updated_ctx})

    if _is_group_detail(request):
        actual_root = new_root_id if root_from != root_to else root_from
        content += _children_section_oob(actual_root, request)
        content += _notes_oob(actual_root, request)

    return HTMLResponse(content=content)


@app.post("/unlink/{enrichment_id}", response_class=HTMLResponse)
async def unlink_opportunity(enrichment_id: int, request: Request):
    """Remove one opportunity from its group. The rest of the group is unaffected."""
    df = _db.execute_query(
        "SELECT id, parent_id FROM core.news_enriched WHERE id = ?", (enrichment_id,)
    )
    old_root_id: int | None = None
    if not df.empty:
        row = _clean(df.iloc[0].to_dict())
        old_root_id = int(row["parent_id"]) if row.get("parent_id") else None

    _db.execute_non_query("UPDATE core.news_enriched SET parent_id = NULL WHERE id = ?", (enrichment_id,))

    actor = signed_in_user(request)
    if old_root_id and old_root_id != enrichment_id:
        # Copy the old group's note history into the newly standalone item
        _copy_notes_as_history(from_group_id=old_root_id, to_group_id=enrichment_id)
        # System event on the old group
        _insert_event_note(
            old_root_id,
            f"Opportunity #{enrichment_id} removed from this group",
            author=actor,
        )
        # System event on the now-standalone item
        _insert_event_note(
            enrichment_id,
            f"Removed from opportunity group #{old_root_id}",
            author=actor,
        )

    updated_ctx = _fetch_enriched_card_ctx(enrichment_id)
    if updated_ctx is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    tmpl    = templates.get_template("partials/enriched_card.html")
    content = tmpl.render({"request": request, "article": updated_ctx})

    caller_url = request.headers.get("HX-Current-URL", "")
    if "/group/" in caller_url and old_root_id:
        content += _children_section_oob(old_root_id, request)
        content += _notes_oob(old_root_id, request)

    return HTMLResponse(content=content)


@app.get("/group/{enrichment_id}", response_class=HTMLResponse)
async def view_group(enrichment_id: int, request: Request):
    """Detail page: the group's representative (oldest) plus all linked opportunities, oldest first."""
    try:
        df = _db.execute_query("SELECT id, parent_id FROM core.news_enriched WHERE id = ?", (enrichment_id,))
    except Exception:
        logger.exception("DB error fetching group %s", enrichment_id)
        raise HTTPException(status_code=404, detail="Opportunity not found")

    if df.empty:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    row     = _clean(df.iloc[0].to_dict())
    root_id = int(row["parent_id"]) if row.get("parent_id") else int(row["id"])

    try:
        representative = _fetch_enriched_card_ctx(root_id)
    except Exception:
        logger.exception("DB error fetching representative %s for group %s", root_id, enrichment_id)
        raise HTTPException(status_code=404, detail="Group representative not found")

    if representative is None:
        raise HTTPException(status_code=404, detail="Group representative not found")

    children_df = _db.execute_query(
        "SELECT id FROM core.news_enriched WHERE parent_id = ? ORDER BY publication_date ASC, id ASC",
        (root_id,),
    )
    children = []
    if not children_df.empty:
        for cid in children_df["id"]:
            ctx = _fetch_enriched_card_ctx(int(cid))
            if ctx is not None:
                children.append(ctx)

    tmpl = templates.get_template("group.html")
    return HTMLResponse(content=tmpl.render({
        "request":        request,
        "representative": representative,
        "children":       children,
        "notes":          _fetch_notes(root_id),
        "country":        representative["country"],
        "countries":      _COUNTRIES,
        "filt":           "all",
        "search":         "",
        "date_from":      "",
        "date_to":        "",
        "contacts_json":  json.dumps([{"name": c["name"], "email": c["email"]} for c in get_contacts_for_country(representative["country"])]),
    }))


@app.get("/contacts/{enrichment_id}", response_class=HTMLResponse)
async def contacts_modal(enrichment_id: int, request: Request):
    """Opens the Enrich Contact modal -- a cheap read (cached results, or a
    'not searched yet' prompt). Never triggers a live Clay search itself."""
    ctx = _fetch_enriched_card_ctx(enrichment_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    searched, domain, contacts = _fetch_group_contacts(enrichment_id)
    tmpl = templates.get_template("partials/contacts_modal.html")
    return HTMLResponse(content=tmpl.render({
        "request":       request,
        "enrichment_id": enrichment_id,
        "company":       ctx.get("company"),
        "country":       ctx.get("country"),
        "searched":      searched,
        "domain":        domain,
        "contacts":      contacts,
    }))


def _contact_context(ctx: dict[str, Any]) -> str:
    """One-line description of the opportunity, given to the contact finder so
    its LLM can rank candidates by relevance to this specific lead."""
    where = f" in {ctx['city']}" if ctx.get("city") else ""
    what  = ctx.get("project_type") or "project"
    fit   = f" {get_profile().fit_label}: {ctx['product_fit']}" if ctx.get("product_fit") else ""
    return f"{ctx.get('title') or ''} ({what}{where}).{fit}"


def _resolved_domain(result) -> str | None:
    dr = result.domain_resolution
    return (dr.canonical_domain or dr.domain) if dr else None


@app.post("/contacts/{enrichment_id}/search", response_class=HTMLResponse)
async def contacts_search(enrichment_id: int, request: Request):
    """Runs a live ClayContactFinder search for this opportunity's company and
    persists whatever it finds. Only fires on explicit user action (the 'Find
    Contacts' / 'Search again' button inside the modal) -- never automatically."""
    ctx = _fetch_enriched_card_ctx(enrichment_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    # Idempotency guard: the results view has no re-search control, so the only
    # way this route fires while contacts already exist is a double-click or the
    # modal being reopened mid-search -- short-circuit to the existing results
    # instead of burning a second live Clay search and inserting duplicates.
    # A genuine "Search again" (searched=True but 0 contacts) is still allowed through.
    already_searched, existing_domain, existing_contacts = _fetch_group_contacts(enrichment_id)
    if already_searched and existing_contacts:
        tmpl = templates.get_template("partials/contacts_body.html")
        return HTMLResponse(content=tmpl.render({
            "request": request, "enrichment_id": enrichment_id,
            "company": ctx.get("company"), "country": ctx.get("country"),
            "searched": already_searched, "domain": existing_domain, "contacts": existing_contacts,
        }))

    company = ctx.get("company")
    country = ctx.get("country") or ""
    actor   = signed_in_user(request)

    found: list = []
    domain: str | None = None
    if company and _contact_finder is not None:
        try:
            loop = asyncio.get_running_loop()
            on_progress = lambda stage: _search_progress.__setitem__(enrichment_id, stage)
            result = await loop.run_in_executor(
                _io_executor, partial(_contact_finder.find, company, country, _contact_context(ctx), on_progress=on_progress)
            )
            found = result.contacts
            domain = _resolved_domain(result)
        except Exception:
            logger.exception("Contact search failed for enrichment %d (%r)", enrichment_id, company)
        finally:
            _search_progress.pop(enrichment_id, None)
    elif _contact_finder is None:
        logger.warning("Contact search requested for enrichment %d but ClayContactFinder is unavailable", enrichment_id)

    _persist_search_outcome(enrichment_id, country, domain, found, actor)

    root_id = _group_root_id(enrichment_id)
    _insert_event_note(root_id, f"Contact search run — {len(found)} found", author=actor)

    searched, domain, contacts = _fetch_group_contacts(enrichment_id)
    tmpl    = templates.get_template("partials/contacts_body.html")
    content = tmpl.render({
        "request":       request,
        "enrichment_id": enrichment_id,
        "company":       company,
        "country":       country,
        "searched":      searched,
        "domain":        domain,
        "contacts":      contacts,
    })
    if _is_group_detail(request):
        content += _notes_oob(root_id, request)
    return HTMLResponse(content=content)


@app.post("/contacts/{enrichment_id}/domain-override", response_class=HTMLResponse)
async def contacts_domain_override(enrichment_id: int, request: Request, domain: str = Form(...)):
    """Lets a rep correct the domain -- shown after every search, not just an
    empty one, so a rep who thinks the automated pick is wrong can fix it
    even when contacts were found. Runs find() again trusting the typed
    domain directly, skipping resolution entirely (see
    ClayContactFinder.find's known_domain parameter). Not idempotency-guarded
    like contacts_search() -- retrying with a different domain is exactly the
    point. _persist_search_outcome deactivates whatever was previously
    active for this enrichment_id first, so a correction replaces the prior
    outcome rather than mixing a possibly-wrong domain's contacts in with
    the corrected ones."""
    ctx = _fetch_enriched_card_ctx(enrichment_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    company        = ctx.get("company")
    country        = ctx.get("country") or ""
    actor          = signed_in_user(request)
    cleaned_domain = _clean_domain_input(domain)

    found: list = []
    if company and cleaned_domain and _contact_finder is not None:
        try:
            loop = asyncio.get_running_loop()
            on_progress = lambda stage: _search_progress.__setitem__(enrichment_id, stage)
            result = await loop.run_in_executor(
                _io_executor, partial(_contact_finder.find, company, country, _contact_context(ctx),
                                       known_domain=cleaned_domain, on_progress=on_progress)
            )
            found = result.contacts
        except Exception:
            logger.exception("Domain-override search failed for enrichment %d (%r, domain=%r)",
                              enrichment_id, company, cleaned_domain)
        finally:
            _search_progress.pop(enrichment_id, None)

    _persist_search_outcome(enrichment_id, country, cleaned_domain, found, actor)

    root_id = _group_root_id(enrichment_id)
    searched, domain, contacts = _fetch_group_contacts(enrichment_id)
    tmpl    = templates.get_template("partials/contacts_body.html")
    content = tmpl.render({
        "request":       request,
        "enrichment_id": enrichment_id,
        "company":       company,
        "country":       country,
        "searched":      searched,
        "domain":        domain,
        "contacts":      contacts,
    })
    if _is_group_detail(request):
        content += _notes_oob(root_id, request)
    return HTMLResponse(content=content)


@app.get("/contacts/{enrichment_id}/progress", response_class=HTMLResponse)
async def contacts_progress(enrichment_id: int):
    """Cheap, in-memory-only read of the current search stage, polled by the
    frontend roughly every second while a search is in flight (see
    _search_progress's module-level docstring for why this is safe to poll
    frequently -- it never touches the DB, Clay, or Azure)."""
    return HTMLResponse(content=_search_progress.get(enrichment_id, "Searching…"))


async def _run_enrich_and_persist(contact_id: int, row: dict[str, Any], field: str) -> dict[str, Any]:
    """The actual Clay lookup + database write for one contact field. Split
    out from _enrich_one_contact_field so the caller can run it as a
    shielded background task -- see that function's docstring for why."""
    value = None
    if row.get("linkedin_url") and _contact_finder is not None:
        contact_obj = ClayContact(
            name=row["full_name"], title=row.get("job_title"), company_name=row.get("company_name"),
            domain=row.get("company_domain"), linkedin_url=row.get("linkedin_url"), city=row.get("city"),
            source=row.get("source") or "domain_search",
        )
        try:
            loop = asyncio.get_running_loop()
            # Faster polling than the package defaults (4s/5s) -- those are
            # tuned for bulk batch use (many contacts per Clay submission,
            # where polling overhead is amortized); a single contact here
            # only pays that overhead once, so polling more often shaves the
            # average post-completion wait without changing the actual
            # ceiling (max_polls scaled up to match, same ~120s/~300s cap).
            if field == "email":
                await loop.run_in_executor(
                    _io_executor, partial(_contact_finder.enrich_email, [contact_obj], max_polls=60, poll_interval=2.0)
                )
                value = contact_obj.work_email
            else:
                await loop.run_in_executor(
                    _io_executor, partial(_contact_finder.enrich_phone, [contact_obj], max_polls=100, poll_interval=3.0)
                )
                value = contact_obj.work_phone
        except Exception:
            logger.exception("%s enrichment failed for contact %d", field, contact_id)

    column, ts_column = (("work_email", "email_enriched_at") if field == "email" else ("work_phone", "phone_enriched_at"))
    _db.execute_non_query(
        f"UPDATE core.opportunity_contacts SET {column} = ?, {ts_column} = SYSDATETIMEOFFSET() WHERE id = ?",
        (value, contact_id),
    )
    return _fetch_contact_row(contact_id)


async def _enrich_one_contact_field(contact_id: int, request: Request, field: str) -> HTMLResponse:
    """Shared body for the per-contact 'Get email' / 'Get phone' actions --
    field is 'email' or 'phone'.

    Confirmed live: closing the contacts modal and reopening it (even for
    the same opportunity) resets the modal's inner HTML, which removes the
    button that issued this request -- htmx aborts the underlying fetch when
    its triggering element leaves the DOM, and that abort reaches the server
    as this request's task being cancelled. Since the database write used to
    happen after awaiting the Clay lookup, a cancellation there silently
    discarded an already-successful result -- the lookup would finish, but
    the row never got updated, so reopening showed the "Get email"/"Get
    phone" button again as if nothing had been tried. Slower ("not found")
    lookups hit this far more often just because they run longer, giving
    more time to close/reopen before completion -- not because "not found"
    is handled differently.

    The fix: run the actual lookup + write as a separate task, shielded from
    this request's own cancellation, so it always finishes and persists
    once started, regardless of whether the client is still around to
    receive the response.
    """
    row = _fetch_contact_row(contact_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Contact not found")

    key = (contact_id, field)
    if key in _enrich_in_flight:
        # Already running (e.g. the modal was closed and reopened mid-lookup) --
        # don't start a second concurrent search for the same contact+field.
        # row's own {field}_in_flight is already True (see _clean_contact_row),
        # so the template renders the same "looking up" state, not a fresh button.
        tmpl = templates.get_template(f"partials/contact_{field}_block.html")
        return HTMLResponse(content=tmpl.render({"request": request, "c": row}))

    _enrich_in_flight.add(key)
    task = asyncio.create_task(_run_enrich_and_persist(contact_id, row, field))
    # Tied to the task's own completion, not this route handler's -- if the
    # route's await below gets cancelled (client disconnected), the task
    # keeps running (shielded) and must stay marked in-flight until IT
    # actually finishes, not until this handler stops watching it.
    task.add_done_callback(lambda _: _enrich_in_flight.discard(key))
    row = await asyncio.shield(task)

    # Render just this field's own block, not the whole row -- getting email
    # and phone used to both swap the entire #contact-row-{id}, so whichever
    # finished first clobbered the other's still-in-flight button/spinner
    # with a fresh one. Each field now has its own swap target, see
    # contact_email_block.html / contact_phone_block.html.
    tmpl = templates.get_template(f"partials/contact_{field}_block.html")
    return HTMLResponse(content=tmpl.render({"request": request, "c": row}))


@app.post("/contacts/{contact_id}/email", response_class=HTMLResponse)
async def contact_get_email(contact_id: int, request: Request):
    return await _enrich_one_contact_field(contact_id, request, "email")


@app.post("/contacts/{contact_id}/phone", response_class=HTMLResponse)
async def contact_get_phone(contact_id: int, request: Request):
    return await _enrich_one_contact_field(contact_id, request, "phone")


def _send_assignment_notification(
    cfg: AppConfig,
    assignee_email: str,
    assignee_name: str,
    actor: str,
    opp_title: str,
    group_url: str,
    actor_email: str = "",
) -> None:
    import threading
    initials   = html_lib.escape("".join(p[0].upper() for p in (actor or "?").split()[:2]))
    actor_html = html_lib.escape(actor or "Someone")
    title_html = html_lib.escape(opp_title or "an opportunity")
    html = f"""
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#f1f5f9;padding:40px 0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
  <tr><td align="center">
  <table width="680" cellpadding="0" cellspacing="0"
         style="background:#ffffff;border-radius:12px;overflow:hidden;
                box-shadow:0 4px 16px rgba(0,0,0,.10);">
    <tr>
      <td style="background:#0d1f45;padding:26px 40px;">
        <span style="color:#ffffff;font-size:16px;font-weight:700;letter-spacing:.4px;">LeadRadar</span>
        <span style="color:rgba(255,255,255,.45);font-size:12px;margin-left:10px;">Intelligence Platform</span>
      </td>
    </tr>
    <tr><td style="background:#2E7D32;height:4px;font-size:0;line-height:0;">&nbsp;</td></tr>
    <tr>
      <td style="padding:36px 40px 32px;">
        <p style="margin:0 0 24px;font-size:11px;font-weight:700;text-transform:uppercase;
                  letter-spacing:1.5px;color:#2E7D32;">Opportunity assigned to you</p>
        <table cellpadding="0" cellspacing="0" style="margin:0 0 26px;">
          <tr>
            <td style="vertical-align:middle;padding-right:14px;">
              <div style="width:44px;height:44px;border-radius:50%;background:#0d1f45;
                          text-align:center;line-height:44px;
                          font-size:15px;font-weight:700;color:#ffffff;
                          font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
                {initials}
              </div>
            </td>
            <td style="vertical-align:middle;">
              <span style="font-size:15px;font-weight:700;color:#0f172a;">{actor_html}</span>
              <span style="font-size:15px;color:#475569;"> assigned you to</span><br>
              <span style="font-size:14px;font-weight:600;color:#2E7D32;">{title_html}</span>
            </td>
          </tr>
        </table>
        <a href="{group_url}"
           style="display:inline-block;background:#0d1f45;color:#ffffff;
                  font-size:14px;font-weight:700;padding:13px 28px;border-radius:7px;
                  text-decoration:none;letter-spacing:.3px;">
          Open in LeadRadar &rarr;
        </a>
      </td>
    </tr>
    <tr>
      <td style="border-top:1px solid #e2e8f0;padding:20px 40px;background:#f8fafc;">
        <p style="margin:0;font-size:11px;color:#94a3b8;line-height:1.6;">
          You received this notification because an opportunity was assigned to you on LeadRadar.
        </p>
      </td>
    </tr>
  </table>
  </td></tr>
</table>"""
    subject = f"{actor or 'Someone'} assigned you an opportunity on LeadRadar"

    def _send() -> None:
        try:
            cc = [actor_email] if actor_email and actor_email != assignee_email else []
            send_email(cfg, subject, html, recipients=[assignee_email], cc=cc)
            logger.info("Assignment notification sent to %s", assignee_email)
        except Exception:
            logger.exception("Failed to send assignment notification to %s", assignee_email)

    threading.Thread(target=_send, daemon=True).start()


@app.post("/group/{group_id}/assign", response_class=HTMLResponse)
async def assign_opportunity(
    group_id:    int,
    request:     Request,
    assignee_email: str = Form(...),
    assignee_name:  str = Form(""),
):
    """Assign the group (root row) to a team member."""
    actor = signed_in_user(request)
    name  = assignee_name.strip() or assignee_email.strip()
    email = assignee_email.strip()
    _db.execute_non_query(
        """UPDATE core.news_enriched
           SET assigned_to_email = ?, assigned_to_name = ?,
               assigned_by = ?, assigned_at = SYSDATETIMEOFFSET()
           WHERE id = ? AND (parent_id IS NULL OR id = ?)""",
        (email, name, actor, group_id, group_id),
    )
    _insert_event_note(group_id, f"Assigned to {name} ({email})", author=actor)
    ctx = _fetch_enriched_card_ctx(group_id)
    if ctx is None:
        raise HTTPException(status_code=404)
    if email:
        actor_user = current_user(request)
        _send_assignment_notification(
            _cfg, email, name, actor or "Someone",
            ctx.get("title", ""),
            f"{_cfg.app_base_url.rstrip('/')}/group/{group_id}",
            actor_email=(actor_user or {}).get("email", ""),
        )
    body = _render_assignment_html(ctx)
    if _is_group_detail(request):
        body += _notes_oob(group_id, request)
    return HTMLResponse(body)


@app.delete("/group/{group_id}/assign", response_class=HTMLResponse)
async def unassign_opportunity(group_id: int, request: Request):
    """Clear the assignment on a group."""
    actor = signed_in_user(request)
    _db.execute_non_query(
        "UPDATE core.news_enriched SET assigned_to_email = NULL, assigned_to_name = NULL, "
        "assigned_by = NULL, assigned_at = NULL WHERE id = ?",
        (group_id,),
    )
    _insert_event_note(group_id, "Assignment removed", author=actor)
    ctx = _fetch_enriched_card_ctx(group_id)
    if ctx is None:
        raise HTTPException(status_code=404)
    body = _render_assignment_html(ctx)
    if _is_group_detail(request):
        body += _notes_oob(group_id, request)
    return HTMLResponse(body)


@app.post("/group/{group_id}/qualify", response_class=HTMLResponse)
async def qualify_opportunity(
    group_id: int,
    request:  Request,
    status:   str = Form(""),   # 'Qualified' | 'Disqualified' | '' (clear)
):
    """Set or clear the qualification flag on a group."""
    actor  = signed_in_user(request)
    status = status.strip()
    if status not in ("Qualified", "Disqualified", ""):
        raise HTTPException(status_code=422, detail="Invalid qualification status")
    if status:
        _db.execute_non_query(
            "UPDATE core.news_enriched SET qualification = ?, qualified_by = ?, "
            "qualified_at = SYSDATETIMEOFFSET() WHERE id = ?",
            (status, actor, group_id),
        )
        _insert_event_note(group_id, f"Marked as {status}", author=actor)
    else:
        _db.execute_non_query(
            "UPDATE core.news_enriched SET qualification = NULL, qualified_by = NULL, "
            "qualified_at = NULL WHERE id = ?",
            (group_id,),
        )
        _insert_event_note(group_id, "Qualification cleared", author=actor)
    ctx = _fetch_enriched_card_ctx(group_id)
    if ctx is None:
        raise HTTPException(status_code=404)
    body = _render_qualification_html(ctx)
    if _is_group_detail(request):
        body += _notes_oob(group_id, request)
    return HTMLResponse(body)


def _render_assignment_html(ctx: dict) -> str:
    tmpl = templates.get_template("partials/assignment_panel.html")
    return tmpl.render({"article": ctx})


def _render_qualification_html(ctx: dict) -> str:
    tmpl = templates.get_template("partials/qualification_panel.html")
    return tmpl.render({"article": ctx})


def _render_priority_html(ctx: dict) -> str:
    tmpl = templates.get_template("partials/priority_panel.html")
    return tmpl.render({"article": ctx})


def _render_card_status_html(ctx: dict) -> str:
    tmpl = templates.get_template("partials/card_status_bar.html")
    return tmpl.render({"request": None, "article": ctx})


@app.post("/group/{group_id}/priority", response_class=HTMLResponse)
async def set_priority(
    group_id: int,
    request:  Request,
    priority: str = Form(""),
):
    """Edit the priority on a group representative."""
    priority = priority.strip()
    if priority and priority not in ("High", "Medium", "Low"):
        raise HTTPException(status_code=422, detail="Invalid priority value")
    actor = signed_in_user(request)
    if priority:
        _db.execute_non_query(
            "UPDATE core.news_enriched SET priority = ? WHERE id = ?",
            (priority, group_id),
        )
        try:
            _db.execute_non_query(
                "UPDATE core.news_articles SET priority = ? "
                "WHERE id = (SELECT source_news_id FROM core.news_enriched WHERE id = ?)",
                (priority, group_id),
            )
        except Exception:
            logger.exception("Failed to sync priority to news_articles for enrichment_id=%d", group_id)
        _insert_event_note(group_id, f"Priority changed to {priority}", author=actor)
    ctx = _fetch_enriched_card_ctx(group_id)
    if ctx is None:
        raise HTTPException(status_code=404)
    body = _render_priority_html(ctx)
    if _is_group_detail(request):
        body += _notes_oob(group_id, request)
    return HTMLResponse(body)


@app.post("/enrich/{enrichment_id}/qualify", response_class=HTMLResponse)
async def card_qualify(
    enrichment_id: int,
    request:       Request,
    status:        str = Form(""),
):
    """Quick qualify/disqualify from a card — updates just the card status bar."""
    df = _db.execute_query(
        "SELECT id, parent_id FROM core.news_enriched WHERE id = ?", (enrichment_id,)
    )
    if df.empty:
        raise HTTPException(status_code=404)
    row     = _clean(df.iloc[0].to_dict())
    root_id = int(row["parent_id"]) if row.get("parent_id") else int(row["id"])

    actor  = signed_in_user(request)
    status = status.strip()
    if status not in ("Qualified", "Disqualified", ""):
        raise HTTPException(status_code=422, detail="Invalid qualification status")

    if status:
        _db.execute_non_query(
            "UPDATE core.news_enriched SET qualification = ?, qualified_by = ?, "
            "qualified_at = SYSDATETIMEOFFSET() WHERE id = ?",
            (status, actor, root_id),
        )
        _insert_event_note(root_id, f"Marked as {status}", author=actor)
    else:
        _db.execute_non_query(
            "UPDATE core.news_enriched SET qualification = NULL, qualified_by = NULL, "
            "qualified_at = NULL WHERE id = ?",
            (root_id,),
        )
        _insert_event_note(root_id, "Qualification cleared", author=actor)

    ctx = _fetch_enriched_card_ctx(enrichment_id)
    if ctx is None:
        raise HTTPException(status_code=404)
    # If this is a child card, also pull the root's qualification to display it
    if root_id != enrichment_id:
        root_ctx = _fetch_enriched_card_ctx(root_id)
        if root_ctx:
            ctx["qualification"] = root_ctx.get("qualification")
            ctx["qualified_by"]  = root_ctx.get("qualified_by")
            ctx["qualified_at"]  = root_ctx.get("qualified_at")
    return HTMLResponse(_render_card_status_html(ctx))


def _mention_notification_html(author: str, note_text: str, opp_title: str, group_url: str) -> str:
    safe_author   = html_lib.escape(author or "Someone")
    safe_title    = html_lib.escape(opp_title or "an opportunity")
    safe_text     = html_lib.escape(note_text[:500] + ("…" if len(note_text) > 500 else ""))
    initials      = html_lib.escape("".join(p[0].upper() for p in (author or "Someone").split()[:2]))
    return f"""
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#f1f5f9;padding:40px 0;font-family:'Segoe UI',Helvetica,Arial,sans-serif;">
  <tr><td align="center">
  <table width="680" cellpadding="0" cellspacing="0"
         style="background:#ffffff;border-radius:12px;overflow:hidden;
                box-shadow:0 4px 16px rgba(0,0,0,.10);">

    <!-- Header -->
    <tr>
      <td style="background:#0d1f45;padding:26px 40px;">
        <span style="color:#ffffff;font-size:16px;font-weight:700;letter-spacing:.4px;">LeadRadar</span>
        <span style="color:rgba(255,255,255,.45);font-size:12px;margin-left:10px;">Intelligence Platform</span>
      </td>
    </tr>
    <tr><td style="background:#1565C0;height:4px;font-size:0;line-height:0;">&nbsp;</td></tr>

    <!-- Body -->
    <tr>
      <td style="padding:36px 40px 32px;">

        <p style="margin:0 0 24px;font-size:11px;font-weight:700;text-transform:uppercase;
                  letter-spacing:1.5px;color:#1565C0;">You were mentioned</p>

        <!-- Author row -->
        <table cellpadding="0" cellspacing="0" style="margin:0 0 24px;">
          <tr>
            <td style="vertical-align:middle;padding-right:14px;">
              <div style="width:44px;height:44px;border-radius:50%;background:#0d1f45;
                          display:inline-flex;align-items:center;justify-content:center;
                          font-size:15px;font-weight:700;color:#ffffff;
                          font-family:'Segoe UI',Helvetica,Arial,sans-serif;
                          text-align:center;line-height:44px;">
                {initials}
              </div>
            </td>
            <td style="vertical-align:middle;">
              <span style="font-size:15px;font-weight:700;color:#0f172a;">{safe_author}</span>
              <span style="font-size:15px;color:#475569;"> mentioned you in a note on</span><br>
              <span style="font-size:14px;font-weight:600;color:#1565C0;">{safe_title}</span>
            </td>
          </tr>
        </table>

        <!-- Note quote -->
        <table width="100%" cellpadding="0" cellspacing="0" style="margin:0 0 32px;">
          <tr>
            <td style="border-left:4px solid #1565C0;padding:14px 20px;
                       background:#f8fafc;border-radius:0 8px 8px 0;">
              <p style="margin:0;font-size:14px;color:#334155;line-height:1.7;font-style:italic;">
                {safe_text}
              </p>
            </td>
          </tr>
        </table>

        <!-- CTA -->
        <a href="{group_url}"
           style="display:inline-block;background:#0d1f45;color:#ffffff;
                  font-size:14px;font-weight:700;padding:13px 28px;border-radius:7px;
                  text-decoration:none;letter-spacing:.3px;">
          Open in LeadRadar &rarr;
        </a>

      </td>
    </tr>

    <!-- Footer -->
    <tr>
      <td style="border-top:1px solid #e2e8f0;padding:20px 40px;background:#f8fafc;">
        <p style="margin:0;font-size:11px;color:#94a3b8;line-height:1.6;">
          You received this notification because you were mentioned in a note on LeadRadar.
        </p>
      </td>
    </tr>

  </table>
  </td></tr>
</table>"""


def _send_mention_notifications(
    cfg: AppConfig,
    mention_emails: list[str],
    author: str,
    note_text: str,
    opp_title: str,
    group_url: str,
) -> None:
    import threading
    html = _mention_notification_html(author, note_text, opp_title, group_url)
    subject = f"{author} mentioned you on LeadRadar"

    def _send() -> None:
        for email in mention_emails:
            try:
                send_email(cfg, subject, html, recipients=[email], cc=[])
                logger.info("Mention notification sent to %s", email)
            except Exception:
                logger.exception("Failed to send mention notification to %s", email)

    threading.Thread(target=_send, daemon=True).start()


@app.post("/group/{group_id}/notes", response_class=HTMLResponse)
async def create_note(
    group_id:     int,
    request:      Request,
    author_name:  str = Form(""),
    comment_text: str = Form(...),
):
    """Append one comment to a group's thread. Returns the full refreshed list."""
    author_name = signed_in_user(request) or author_name.strip()
    text = comment_text.strip()
    if text:
        country = _get_enrichment_country(group_id)
        _db.execute_non_query(
            "INSERT INTO core.opportunity_notes (group_id, country, author_name, comment_text) VALUES (?, ?, ?, ?)",
            (group_id, country, author_name.strip() or None, text),
        )

    # @mention notifications — fire and forget, never blocks the response
    form_data    = await request.form()
    raw_mentions = form_data.getlist("mention")
    unique_emails = list({e for e in raw_mentions if e})
    if unique_emails and text:
        try:
            title_df  = _db.execute_query(
                "SELECT title FROM core.news_enriched WHERE id = ?", (group_id,)
            )
            opp_title = title_df.iloc[0]["title"] if not title_df.empty else ""
        except Exception:
            opp_title = ""
        group_url = f"{_cfg.app_base_url.rstrip('/')}/group/{group_id}"
        _send_mention_notifications(_cfg, unique_emails, author_name or "Someone", text, opp_title, group_url)

    return HTMLResponse(content=_render_notes_list_html(group_id, request))


@app.put("/notes/{note_id}", response_class=HTMLResponse)
async def edit_note(note_id: int, request: Request, comment_text: str = Form(...)):
    """Edit one comment in place. Returns just that comment's re-rendered HTML."""
    caller = signed_in_user(request)
    row_df = _db.execute_query("SELECT author_name FROM core.opportunity_notes WHERE id = ?", (note_id,))
    if row_df.empty:
        raise HTTPException(status_code=404, detail="Note not found")
    if caller and row_df.iloc[0]["author_name"] != caller:
        raise HTTPException(status_code=403, detail="You can only edit your own notes")
    text = comment_text.strip()
    if text:
        _db.execute_non_query(
            "UPDATE core.opportunity_notes SET comment_text = ?, updated_at = SYSDATETIMEOFFSET() WHERE id = ?",
            (text, note_id),
        )

    df = _db.execute_query(
        """
        SELECT id, author_name, comment_text,
               CONVERT(VARCHAR(16), created_at AT TIME ZONE 'UTC', 120) AS created_at,
               updated_at
        FROM   core.opportunity_notes
        WHERE  id = ?
        """,
        (note_id,),
    )
    if df.empty:
        raise HTTPException(status_code=404, detail="Note not found")

    note             = _clean(df.iloc[0].to_dict())
    note["was_edited"] = bool(note.get("updated_at"))

    user = current_user(request)
    tmpl = templates.get_template("partials/note_item.html")
    return HTMLResponse(content=tmpl.render({
        "request":              request,
        "note":                 note,
        "current_user_display": user["display"] if user else "",
    }))


@app.delete("/notes/{note_id}", response_class=HTMLResponse)
async def delete_note(note_id: int, request: Request):
    """Remove one comment. Returns empty content so the HTMX outerHTML swap deletes the element."""
    caller = signed_in_user(request)
    row_df = _db.execute_query("SELECT author_name FROM core.opportunity_notes WHERE id = ?", (note_id,))
    if row_df.empty:
        raise HTTPException(status_code=404, detail="Note not found")
    if caller and row_df.iloc[0]["author_name"] != caller:
        raise HTTPException(status_code=403, detail="You can only delete your own notes")
    _db.execute_non_query("DELETE FROM core.opportunity_notes WHERE id = ?", (note_id,))
    return HTMLResponse(content="")


@app.post("/share/{enrichment_id}", response_class=HTMLResponse)
async def share_opportunity(
    enrichment_id: int,
    request:       Request,
    to_email:      str = Form(...),
    message:       str = Form(""),
):
    """Email one enriched opportunity to one or more comma-separated recipients, with a personal note on top."""
    tmpl = templates.get_template("partials/share_status.html")

    ctx = _fetch_enriched_card_ctx(enrichment_id)
    if ctx is None:
        return HTMLResponse(tmpl.render({"request": request, "success": False, "error": "Opportunity not found."}))

    recipients = [e.strip() for e in to_email.split(",") if e.strip()]
    recipients = [e for e in recipients if "@" in e]
    if not recipients:
        return HTMLResponse(tmpl.render({"request": request, "success": False, "error": "Enter at least one valid email address."}))

    opp               = _to_enriched_opportunity(ctx)
    html_body, subject = compose_share_email(opp, message, enrichment_id=enrichment_id, app_base_url=_cfg.app_base_url)
    sender_user = current_user(request)
    sender_cc   = [sender_user["email"]] if sender_user and sender_user.get("email") else []
    sent = send_email(_cfg, subject, html_body, recipients=recipients, cc=sender_cc)

    if sent:
        return HTMLResponse(tmpl.render({"request": request, "success": True, "to_email": ", ".join(recipients)}))
    return HTMLResponse(tmpl.render({
        "request": request, "success": False, "error": "Failed to send — check the MAIL_BACKEND settings.",
    }))


# ── AI Chat ───────────────────────────────────────────────────────────────────

_CHAT_SCRAPED_TEXT_LIMIT = 8_000   # chars — keeps system prompt within a safe token budget


def _build_chat_system_prompt(
    ctx:          dict[str, Any],
    scraped_text: str,
    linked:       list[dict[str, Any]],
) -> str:
    profile = get_profile()
    parts = [
        f"You are a sharp commercial assistant helping a {profile.company_name} sales rep evaluate one "
        f"specific business opportunity. {' '.join(profile.description.split())}",
        f"{profile.company_name}'s offer: {'; '.join(profile.products)}.",
        "",
        "STYLE:",
        "- Be concise. Answer only what was asked — do not add unrequested sections, lists, or analyses.",
        "- Write like a knowledgeable colleague, not a consultant report. Prefer short paragraphs over "
        "nested bullet structures.",
        "- No headers unless the answer genuinely needs them. No 'If you want I can also provide…' filler.",
        "- Do not label or announce when you switch between article context and general knowledge — "
        "just blend it naturally into your answer.",
        "- Respond in the same language the user writes in.",
        "",
        "KNOWLEDGE:",
        "- For opportunity-specific facts (phase, value, timeline, contacts stated in the article): "
        "use only what is in the context below.",
        "- For anything else (company background, ownership, financials, reputation; local market, "
        "regulations, infrastructure; industry trends, competitors): draw on your training knowledge "
        "and give a direct, useful answer. Never refuse because something is absent from the context.",
        "",
        f"OPPORTUNITY: {ctx.get('opportunity_code', '')}",
        f"TITLE: {ctx.get('title', '')}",
        f"SOURCE: {ctx.get('source_outlet', '')} | DATE: {ctx.get('publication_date', '')}",
        f"URL: {ctx.get('url', '')}",
        "",
        f"PHASE: {ctx.get('phase', 'Unknown')}  |  SECTOR: {ctx.get('sector') or 'Unknown'}  |  "
        f"PRIORITY: {ctx.get('priority') or 'Unknown'}  |  REGION: {ctx.get('region') or 'Unknown'}",
        f"COMPANY: {ctx.get('company') or 'Not stated'}",
        f"CITY: {ctx.get('city') or 'Not stated'}",
        f"PROJECT TYPE: {ctx.get('project_type') or 'Not stated'}",
        f"PROJECT VALUE: {ctx.get('project_value') or 'Not stated'}",
        "",
        "COMMERCIAL ANALYSIS:",
        f"  {profile.fit_label}: {ctx.get('product_fit') or ''}",
        f"  Why it matters: {ctx.get('why_it_matters') or ''}",
        f"  Recommended action: {ctx.get('recommended_action') or ''}",
    ]

    if ctx.get("qualification"):
        parts.append(
            f"\nQUALIFICATION: {ctx['qualification']}"
            + (f" (by {ctx['qualified_by']})" if ctx.get("qualified_by") else "")
        )
    if ctx.get("assigned_to_name"):
        parts.append(
            f"ASSIGNED TO: {ctx['assigned_to_name']}"
            + (f" <{ctx['assigned_to_email']}>" if ctx.get("assigned_to_email") else "")
        )

    if scraped_text:
        text = scraped_text[:_CHAT_SCRAPED_TEXT_LIMIT]
        if len(scraped_text) > _CHAT_SCRAPED_TEXT_LIMIT:
            text += "\n[article truncated]"
        parts += ["", "FULL ARTICLE TEXT:", text]

    if linked:
        parts += ["", f"LINKED OPPORTUNITIES ({len(linked)}):"]
        for lk in linked:
            parts += [
                f"  [{lk.get('opportunity_code', '')}] {lk.get('title', '')}",
                f"    Phase: {lk.get('phase', 'Unknown')} | Date: {lk.get('publication_date', '')} | "
                f"Company: {lk.get('company') or 'N/A'} | City: {lk.get('city') or 'N/A'}",
                f"    Why it matters: {lk.get('why_it_matters') or ''}",
            ]

    return "\n".join(parts)


@app.post("/chat/{enrichment_id}")
async def chat_opportunity(enrichment_id: int, request: Request):
    """Conversational AI about one enriched opportunity. Returns {response: str}."""
    body    = await request.json()
    history: list[dict] = body.get("history", [])
    message: str        = (body.get("message") or "").strip()

    if not message:
        return JSONResponse({"error": "Empty message"}, status_code=400)

    ctx = _fetch_enriched_card_ctx(enrichment_id)
    if ctx is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")

    scraped_text = ""
    try:
        df = _db.execute_query(
            "SELECT TOP 1 scraped_text FROM core.news_enriched WHERE id = ?", (enrichment_id,)
        )
        if not df.empty:
            scraped_text = str(df.iloc[0].get("scraped_text") or "")
    except Exception:
        logger.debug("scraped_text not available for enrichment_id=%d (pre-migration row)", enrichment_id)

    # Linked opportunities — children of the group root
    root_id = ctx.get("parent_id") or enrichment_id
    linked: list[dict] = []
    try:
        df = _db.execute_query(
            "SELECT id FROM core.news_enriched WHERE parent_id = ? ORDER BY publication_date ASC",
            (root_id,),
        )
        if not df.empty:
            for cid in df["id"]:
                child_id = int(cid)
                if child_id != enrichment_id:
                    child_ctx = _fetch_enriched_card_ctx(child_id)
                    if child_ctx:
                        linked.append(child_ctx)
    except Exception:
        logger.exception("Linked opportunities fetch failed for chat, root=%d", root_id)

    system_prompt = _build_chat_system_prompt(ctx, scraped_text, linked)

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(history[-20:])   # cap to prevent token overflow
    messages.append({"role": "user", "content": message})

    try:
        response = _llm.chat_with_opportunity(messages)
        return JSONResponse({"response": response})
    except Exception:
        logger.exception("Chat LLM error for enrichment_id=%d", enrichment_id)
        return JSONResponse({"error": "LLM error — please try again."}, status_code=500)
