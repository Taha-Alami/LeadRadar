"""
Article scraper for the LeadRadar pipeline.

RSS summaries are too short to tell a sales rep whether a project is worth
calling about (no phase, no company, no city). This module fetches the full
article page so the enrichment LLM call has real text to extract those
details from.

Two failure modes that look like success unless explicitly guarded against:

1. **Google News redirect links.** RSS ``<link>`` values are obfuscated
   redirect URLs (``news.google.com/rss/articles/...``), not the publisher's
   actual URL. Fetching them directly, without a Google session, frequently
   returns Google's own GDPR consent interstitial ("Before you continue to
   Google...") with HTTP 200 instead of redirecting to the real article.
   Fixed by resolving the real publisher URL via ``googlenewsdecoder`` first.
2. **JS-rendered publisher sites.** Some news sites are single-page apps —
   a plain HTTP fetch only gets an empty shell ("You need to enable
   JavaScript to run this app."). This is checked on the *extracted* text,
   not the raw HTML — many ordinary, fully-scrapable pages contain an
   unrelated ``<noscript>`` tag with similar wording for an ad/analytics
   script, which extractors normally discard as boilerplate, so a raw-HTML
   substring match would false-positive on perfectly good articles. Gating
   on a short extracted result avoids that. Either failure mode triggers a
   re-fetch with a headless Chromium (Playwright) as a last resort.

Extraction itself: trafilatura first (the best extractor for most news
sites), newspaper4k only if trafilatura produces nothing — no character-count
heuristics, just "did the best tool actually produce text." If everything
fails, the caller degrades to the RSS title+summary rather than dropping the
opportunity — see ``ScrapedArticle.success``.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import dataclass

import warnings

import requests
import trafilatura
from googlenewsdecoder import gnewsdecoder
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message="nltk is not installed")
    from newspaper import Article

logger = logging.getLogger(__name__)

_USER_AGENT        = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
_DEFAULT_TIMEOUT_S = 180   # generous — scraping only runs on 3-5 picks/day, speed is not a concern
_FETCH_HEADERS     = {
    "User-Agent":      _USER_AGENT,
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "nl,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
}

_GOOGLE_CONSENT_MARKER = "before you continue to google"

_JS_REQUIRED_MARKERS = (
    "you need to enable javascript to run this app",
    "please enable javascript",
    "javascript is required to use this",
    "this website requires javascript",
    "enable javascript and cookies to continue",
)
_JS_WALL_MAX_CHARS = 200   # only treat a JS-required match as a real wall if it dominates a short extraction


@dataclass
class ScrapedArticle:
    """Result of scraping one article URL."""

    text:         str
    method:       str   # "trafilatura" | "newspaper4k" | "trafilatura_js" | "newspaper4k_js" | "failed"
    char_count:   int
    resolved_url: str = ""   # real publisher URL after Google News redirect is decoded

    @property
    def success(self) -> bool:
        return self.method != "failed"


def scrape_article(url: str, *, timeout_s: int = _DEFAULT_TIMEOUT_S) -> ScrapedArticle:
    """
    Scrape full article text from a URL.

    If ``url`` is a Google News redirect link, the real publisher URL is
    resolved first. Tries a plain HTTP fetch + trafilatura/newspaper4k; if
    that yields nothing (blocked page, JS-only shell, or both extractors
    come up empty), retries with a headless-browser render as a last resort.

    Args:
        url:       Article URL to scrape (may be a Google News redirect link).
        timeout_s: Timeout in seconds, applied to both the HTTP fetch and the
                   headless-browser render.

    Returns:
        ``ScrapedArticle`` with ``method="failed"`` and empty text if nothing
        worked at all.
    """
    real_url = _resolve_real_url(url)

    html   = _fetch_html(real_url, timeout_s)
    result = _extract_from_html(html, real_url, rendered=False)
    if result:
        result.resolved_url = real_url
        return result

    rendered_html = _fetch_html_with_playwright(real_url, timeout_s)
    result = _extract_from_html(rendered_html, real_url, rendered=True)
    if result:
        result.resolved_url = real_url
        return result

    return ScrapedArticle(text="", method="failed", char_count=0, resolved_url=real_url)


def _resolve_real_url(url: str, attempts: int = 2) -> str:
    """
    Decode a Google News redirect link to the real publisher URL.

    Google intermittently rate-limits the decode request itself (an
    HTTP 429 "sorry" page) when called repeatedly in a short window — observed
    in practice when scraping several picks back-to-back. A short retry
    clears most of these transient failures. Returns ``url`` unchanged if it
    isn't a Google News link, or if every attempt fails — ``_looks_blocked()``
    catches the resulting failure mode where that leaves us fetching Google's
    own interstitial instead of an article.
    """
    if "news.google.com" not in url:
        return url

    for attempt in range(1, attempts + 1):
        try:
            result = gnewsdecoder(url, interval=1)
            if result.get("status") and result.get("decoded_url"):
                return result["decoded_url"]
            logger.debug("googlenewsdecoder could not resolve '%s' (attempt %d/%d): %s", url, attempt, attempts, result.get("message"))
        except Exception as exc:
            logger.debug("googlenewsdecoder raised for '%s' (attempt %d/%d): %s", url, attempt, attempts, exc)
        if attempt < attempts:
            time.sleep(1.5)
    return url


def _extract_from_html(html: str | None, url: str, *, rendered: bool) -> ScrapedArticle | None:
    """Run the extractor chain on ``html``; ``None`` if blocked or nothing extractable."""
    if not html or _GOOGLE_CONSENT_MARKER in html.lower():
        return None

    text = _try_trafilatura(html, url)
    if text and not _is_js_wall(text):
        return ScrapedArticle(text=text, method=("trafilatura_js" if rendered else "trafilatura"), char_count=len(text))

    text = _try_newspaper(html, url)
    if text and not _is_js_wall(text):
        return ScrapedArticle(text=text, method=("newspaper4k_js" if rendered else "newspaper4k"), char_count=len(text))

    return None


def _is_js_wall(text: str | None) -> bool:
    """
    ``True`` only if a short extraction is dominated by a "please enable
    JavaScript" message — i.e. the extractor found nothing but the SPA shell
    itself. Gated on length so the same phrase buried in an unrelated
    ``<noscript>`` tag on an otherwise normal, long article never matches.
    """
    if not text or len(text) > _JS_WALL_MAX_CHARS:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _JS_REQUIRED_MARKERS)


def _fetch_html(url: str, timeout_s: int) -> str | None:
    try:
        response = requests.get(url, headers=_FETCH_HEADERS, timeout=timeout_s, allow_redirects=True)
        if response.status_code != 200:
            logger.debug("Scrape '%s' returned HTTP %d — skipping.", url, response.status_code)
            return None
        return response.text
    except requests.RequestException as exc:
        logger.debug("Scrape fetch failed for '%s': %s", url, exc)
        return None


def _fetch_html_with_playwright(url: str, timeout_s: int) -> str | None:
    """
    Last-resort fetch: render the page with headless Chromium.

    Playwright's sync API cannot run inside an existing asyncio event loop
    (Prefect runs its task executor in one). The fix is to launch Playwright
    in a dedicated daemon thread which has no event loop of its own.
    """
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401 — check availability
    except Exception as exc:
        logger.debug("Playwright not available: %s", exc)
        return None

    result:   list[str | None] = [None]
    tb_store: list[str] = []

    def _run_in_thread() -> None:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-dev-shm-usage",
                    ],
                )
                try:
                    ctx = browser.new_context(
                        user_agent=_USER_AGENT,
                        viewport={"width": 1280, "height": 800},
                        extra_http_headers={
                            "Accept-Language": "nl-BE,nl;q=0.9,fr-BE;q=0.8,en;q=0.7",
                        },
                    )
                    page = ctx.new_page()
                    try:
                        page.goto(url, timeout=timeout_s * 1000, wait_until="domcontentloaded")
                    except PlaywrightError as exc:
                        logger.debug("Playwright goto failed for '%s' (using whatever loaded): %s", url, exc)
                    page.wait_for_timeout(2000)
                    result[0] = page.content()
                finally:
                    browser.close()
        except Exception:
            tb_store.append(traceback.format_exc())

    thread = threading.Thread(target=_run_in_thread, daemon=True)
    thread.start()
    thread.join(timeout=timeout_s + 15)

    if tb_store:
        logger.debug("Playwright render failed for '%s':\n%s", url, tb_store[0])
        return None
    if thread.is_alive():
        logger.debug("Playwright render timed out for '%s'", url)
        return None
    return result[0]


def _try_trafilatura(html: str, url: str) -> str | None:
    try:
        return trafilatura.extract(html, url=url, include_comments=False, include_tables=False)
    except Exception as exc:
        logger.debug("trafilatura.extract failed for '%s': %s", url, exc)
        return None


def _try_newspaper(html: str, url: str) -> str | None:
    try:
        article = Article(url)
        article.download(input_html=html)   # supplies HTML directly — Article has no set_html()
        article.parse()
        return article.text or None
    except Exception as exc:
        logger.debug("newspaper4k parse failed for '%s': %s", url, exc)
        return None
