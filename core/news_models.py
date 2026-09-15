"""
Data models shared across the LeadRadar pipeline and web app.

``NewsItem`` is the canonical representation of a collected news article,
whether it came from a Google News search, a publisher's own RSS feed, or an
official procurement feed.

``EnrichedOpportunity`` is a ``NewsItem`` that survived triage and picking and
was scraped + analysed in full — i.e. an actual sales lead.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class NewsItem:
    """
    A single news item as collected from any data source.

    Attributes:
        title:    Headline text, with the publisher name stripped when the
                  source uses Google News' "Headline - Publisher" format.
        summary:  Body snippet, HTML tags removed, capped at 400 characters.
        url:      URL of the article (may be a Google News redirect link).
        date:     Publication date string as found in the feed (RFC 2822 for
                  RSS, ISO 8601 for Atom). May be empty if the feed omits it.
        source:   Publisher / outlet name, or the feed label as a fallback.
        country:  Country code the item was collected for (e.g. ``"SPAIN"``).
        sector:   Triage tag — one of the active business profile's sectors,
                  or ``None`` before triage / when not identifiable.
        priority: Triage tag — ``High``, ``Medium``, ``Low``, ``Unknown``, or
                  ``None`` before triage has run.
        region:   Triage tag — administrative region the project is located
                  in (e.g. ``"Cataluna"``), or ``None`` if not identifiable.
        location: Triage verdict — ``"in_country"``, ``"abroad"`` (dropped
                  before persistence/picking), or ``"unclear"`` (demoted to Low).
    """

    title:    str
    summary:  str
    url:      str
    date:     str
    source:   str
    country:  str = ""
    sector:   str | None = None
    priority: str | None = None
    region:   str | None = None
    location: str | None = None

    def to_scoring_prompt_line(self, index: int) -> str:
        """
        Format this item as a compact one-liner for an LLM triage/picking prompt.

        The LLM receives a numbered list of these lines and returns a JSON array
        referencing them by index *and* URL. Field labels are always English —
        this is just a tag scheme for the LLM, independent of whatever language
        the article's own title/summary happens to be in.

        When ``sector``/``priority``/``region`` have already been assigned
        (i.e. triage has run), they are appended so the picker can use them as
        context.

        Args:
            index: 1-based position in the batch sent to the LLM.

        Returns:
            A pipe-delimited string such as::

                [3] SOURCE:Some Outlet | TITLE:... | SUMMARY:... | URL:...
        """
        line = (
            f"[{index}] SOURCE:{self.source}"
            f" | TITLE:{self.title[:130]}"
            f" | SUMMARY:{self.summary[:250]}"
            f" | URL:{self.url}"
        )
        if self.sector or self.priority or self.region:
            line += (
                f" | SECTOR:{self.sector or 'Unknown'}"
                f" | PRIORITY:{self.priority or 'Unknown'}"
                f" | REGION:{self.region or 'Unknown'}"
            )
        if self.location == "unclear":
            line += " | LOCATION:unclear"
        return line


@dataclass
class EnrichedOpportunity:
    """
    A fully enriched commercial lead.

    Built from the *full scraped article text* — the pipeline scrapes the
    article page specifically to recover the details RSS summaries omit
    (project phase, company, city, value) so a sales rep can judge
    actionability at a glance.

    Produced by ``pipeline.opportunity_enricher.enrich_opportunities()`` (daily
    run) and ``enrich_single_article()`` (web app, on demand).

    Attributes:
        title:              Article headline.
        url:                Resolved publisher URL.
        source:             Publisher/outlet name.
        country:            Country code (e.g. ``"SPAIN"``).
        published_date:     Publication date string as found in the feed.
        phase:              Project stage: ``Announced``, ``Planning``,
                            ``Permitting``, ``Tender``, ``Awarded``,
                            ``Construction``, ``Completed``, or ``Unknown``.
                            Only the first five are actionable — see
                            ``is_actionable``.
        company:            Developer / contractor / main stakeholder, or ``None``.
        city:               City or municipality of the project, or ``None``.
        project_type:       Short description of what is being built/bought.
        project_value:      Value exactly as stated in the article, or ``None``.
        product_fit:        Why this project plausibly needs the business'
                            products (definition comes from the business profile).
        why_it_matters:     1-2 sentence rationale for the sales team.
        recommended_action: Concrete next step for the sales rep.
        scrape_method:      Which extractor produced the text, or ``"failed"``
                            (extraction fell back to the RSS title+summary).
        sector / priority / region: Final, re-checked triage tags.
        expansion_signal:   Set only when ``phase == "Construction"`` AND the
                            article describes a sudden expansion or additional
                            need beyond what's already underway — the one
                            exception to "already under construction = too late".
        scraped_text:       Full article text (kept for the web-app chat).
        end_usage:          What the finished project will be used for.
        english_summary:    2-3 sentence English summary.
        source_url:         Original RSS URL (may be a Google News redirect) —
                            used to look up the source row in ``core.news_articles``.
    """

    title:               str
    url:                 str
    source:              str
    country:             str
    published_date:      str
    phase:               str
    company:             str | None
    city:                str | None
    project_type:        str | None
    project_value:       str | None
    product_fit:         str
    why_it_matters:      str
    recommended_action:  str
    scrape_method:       str
    sector:              str | None = None
    priority:            str | None = None
    region:              str | None = None
    expansion_signal:    str | None = None
    scraped_text:        str | None = None
    end_usage:           str | None = None
    english_summary:     str | None = None
    source_url:          str | None = None

    @property
    def is_actionable(self) -> bool:
        """
        ``False`` for completed projects always, and for construction-phase
        projects UNLESS ``expansion_signal`` is set — a sudden expansion or
        subcontractor-driven additional need can still make an already-started
        project worth a call.
        """
        phase = self.phase.lower()
        if phase == "completed":
            return False
        if phase == "construction":
            return bool(self.expansion_signal)
        return True

    @property
    def phase_color(self) -> str:
        """CSS hex color for the phase badge / card border."""
        return PHASE_COLORS.get(self.phase.lower(), "#9E9E9E")


PHASE_COLORS: dict[str, str] = {
    "announced":  "#607D8B",
    "planning":   "#1976D2",
    "permitting": "#00838F",
    "tender":     "#6A1B9A",
    "awarded":    "#2E7D32",
}
