"""
RSS / Atom feed reader for LeadRadar.

Uses ``feedparser`` for XML parsing — it automatically handles RSS 2.0, RSS 1.0,
Atom 1.0, Atom 0.3, and RDF feeds without any manual namespace logic.

HTTP fetching is handled by ``requests`` (not feedparser's built-in urllib) so
we keep control over timeout and the User-Agent header.  The raw response bytes
are passed directly to ``feedparser.parse()``.
"""
from __future__ import annotations

import logging
import re

import feedparser
import requests

from core.news_models import NewsItem

logger      = logging.getLogger(__name__)
_USER_AGENT = "LeadRadar/1.0"


def fetch_rss_feed(
    url:         str,
    source_name: str,
    *,
    max_items:   int | None = None,
    timeout_s:   int = 12,
    split_gnews_title: bool = True,
) -> list[NewsItem]:
    """
    Fetch an RSS or Atom feed and return a list of ``NewsItem`` objects.

    Args:
        url:         Full URL of the feed.
        source_name: Human-readable label for the feed, used as ``NewsItem.source``
                     unless the entry title contains a Google News outlet suffix
                     (``"Headline - Publisher"``), in which case the publisher
                     name takes precedence.
        max_items:   Maximum number of items to return. ``None`` returns all entries.
        timeout_s:   HTTP request timeout in seconds.
        split_gnews_title: Split ``"Headline - Publisher"`` titles when the entry
                     has no ``<source>`` element. Pass ``False`` for a publisher's
                     own (non-Google News) feed, whose headlines may legitimately
                     contain `` - `` and must be kept intact.

    Returns:
        Parsed list of ``NewsItem`` objects. Empty list on any error — a single
        broken feed never aborts a collection run.
    """
    try:
        response = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=timeout_s,
            allow_redirects=True,
        )
        if response.status_code != 200:
            logger.debug("Feed '%s' returned HTTP %d — skipping.", source_name, response.status_code)
            return []
        feed = feedparser.parse(response.content)
    except requests.RequestException as exc:
        logger.debug("Feed '%s' fetch failed: %s", source_name, exc)
        return []

    entries = feed.entries[:max_items] if max_items is not None else feed.entries

    results: list[NewsItem] = []
    for entry in entries:
        title   = (entry.get("title") or "").strip()
        url_    = entry.get("link") or ""
        date    = entry.get("published") or entry.get("updated") or ""
        summary = _extract_summary(entry)

        # Prefer the <source> element text when present, and only fall back
        # to title splitting when it is absent.
        # feedparser maps <source url="...">Name</source> → entry.source["title"].
        source_from_element = _get_source_element(entry)
        if source_from_element:
            outlet = source_from_element
            # Keep the full title — the outlet is already known from <source>
        elif split_gnews_title:
            outlet, title = _split_google_news_title(title)
        else:
            outlet = ""

        if title:
            results.append(NewsItem(
                title   = title,
                summary = summary,
                url     = url_,
                date    = date,
                source  = outlet or source_name,
            ))

    return results


def _get_source_element(entry) -> str:
    """
    Return the publisher name from a feedparser ``<source>`` element, if present.

    Google News RSS uses ``<source url="...">Publisher</source>`` on every entry.
    feedparser maps this to ``entry.source`` as a dict with a ``"title"`` key.
    """
    src = entry.get("source")
    if isinstance(src, dict):
        return (src.get("title") or "").strip()
    return ""


def _extract_summary(entry) -> str:
    """Pull the best available text and strip HTML tags."""
    raw = (
        entry.get("summary")
        or (entry.get("content") or [{}])[0].get("value")
        or ""
    )
    return re.sub(r"<[^>]+>", "", raw)[:400]


def _split_google_news_title(title: str) -> tuple[str, str]:
    """
    Split a ``"Headline - Publisher"`` title into ``(publisher, headline)``.

    Google News RSS feeds embed the source publication as a suffix separated
    by `` - ``.  Splitting on the *last* occurrence avoids breaking headlines
    that legitimately contain `` - ``.

    Returns:
        ``(publisher, headline)``.  If no `` - `` is found, returns
        ``("", title)`` so the caller falls back to the feed name.
    """
    if " - " in title:
        headline, publisher = title.rsplit(" - ", 1)
        return publisher.strip(), headline.strip()
    return "", title
