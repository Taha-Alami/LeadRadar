"""``ClayContactFinder`` -- the main pipeline class.

Given a company name and country, finds real decision-maker contacts (name,
title, LinkedIn URL) and optionally their work email/phone, using Clay's
Public API (https://api.clay.com/public/v0) directly -- no Clay table or
webhook involved.

Pipeline (see the README for the full rationale behind each stage):

1. An LLM canonicalizes the input name into the company's real name.
2. Two independent methods each try to resolve a domain: a country-biased,
   search-grounded LLM resolver, and Clay's native Find Domain routine.
3. Both candidates are cross-checked through Clay's Enrich Company routine.
   If they agree, that's accepted directly (no LLM call). If only one
   resolver found anything, it's trusted directly too (also no LLM call --
   nothing to arbitrate between). Only when both disagree does an LLM decide
   which is more likely right -- always forced to pick one, never "neither."
   Whatever domain this settles on is trusted directly; there's no further
   plain-text confirmation step.
4. If a domain was found, a country-filtered people search on it finds
   contacts -- these have a LinkedIn URL and are eligible for email/phone
   enrichment. This itself is two-step: a precise, keyword-filtered title
   search first, and only if that finds nobody, a broader unfiltered pull of
   everyone Clay has indexed at the domain with an LLM picking the best 3-10
   -- a resolved domain shouldn't come back empty just because its
   employees' real job titles don't match a fixed keyword list.
5. That's it -- one search attempt, no automatic fallback to a second search
   mechanism if it comes back empty (an earlier version had one; removed by
   explicit request in favor of a human reviewing the result and correcting
   the domain directly, see ``find``'s ``known_domain`` parameter).
6. ``enrich_email`` / ``enrich_phone`` submit contacts to Clay's Work Email /
   Work Phone routines -- every contact returned by ``find()`` is eligible.
"""

from __future__ import annotations

import concurrent.futures
import functools
import json
import logging
import re
import time
from collections.abc import Callable
from urllib.parse import quote, urlsplit, urlunsplit

import pycountry
import requests

from .exceptions import ClayAPIError, ConfigurationError
from .llm import azure_openai_complete
from .models import Contact, DomainResolution, EnrichmentResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults -- all overridable per-instance via ClayContactFinder(...) kwargs.
# ---------------------------------------------------------------------------

#: Roles most likely to own a decision to buy whatever it is you're selling.
#: Replace with your own list -- this default is generic facilities / operations /
#: procurement titles, a reasonable starting point for B2B sales to project
#: owners and contractors.
DEFAULT_TARGET_TITLES = [
    "Facilities Manager", "Operations Director", "Site Manager",
    "Procurement Manager", "Head of Real Estate",
    "Director General", "Directeur Général",
]


# pycountry's fuzzy lookup misses a handful of extremely common colloquial
# names that don't match any of its official/common name fields at all --
# confirmed by testing, not speculative (e.g. "Russia" only matches
# "Russian Federation", "UK" doesn't match "United Kingdom" as a substring
# the way "USA" happens to). This is a targeted patch for real, observed
# gaps, tried only after the library's own lookup fails -- not a substitute
# for it.
_COUNTRY_ALIAS_FALLBACK = {
    "UK": "gb", "HOLLAND": "nl", "RUSSIA": "ru", "UAE": "ae",
    "IVORY COAST": "ci", "COTE D'IVOIRE": "ci", "CÔTE D'IVOIRE": "ci",
    "CAPE VERDE": "cv", "BURMA": "mm", "MACAU": "mo", "VATICAN": "va",
    "PALESTINE": "ps", "SWAZILAND": "sz", "ENGLAND": "gb", "SCOTLAND": "gb",
    "WALES": "gb", "TURKIYE": "tr",
}


@functools.lru_cache(maxsize=256)
def _lookup_country(country: str):
    """Single shared country lookup backing both _country_to_gl and
    _canonical_country_name below, so they can't drift out of sync with each
    other (they did, briefly -- see the latter's docstring). Tries
    pycountry's own fuzzy match first (handles "France", "United States",
    "USA", "Czechia", official names, ...), then a small alias table for the
    handful of common colloquial names pycountry itself doesn't catch (e.g.
    "UK"). Returns a ``pycountry`` Country object, or ``None`` if nothing
    matches -- callers degrade gracefully, this is never fatal.
    """
    if not country or not country.strip():
        return None
    cleaned = country.strip()
    try:
        return pycountry.countries.lookup(cleaned)
    except LookupError:
        pass
    alpha_2 = _COUNTRY_ALIAS_FALLBACK.get(cleaned.upper())
    return pycountry.countries.get(alpha_2=alpha_2) if alpha_2 else None


def _country_to_gl(country: str) -> str | None:
    """ISO 3166-1 alpha-2 code for ``country``, doubling as Google's "gl"
    search-bias parameter. ``None`` if unresolved -- see _lookup_country."""
    match = _lookup_country(country)
    if match:
        return match.alpha_2.lower()
    logger.debug("could not resolve country %r to an ISO code -- searching without geo-bias", country)
    return None


def _canonical_country_name(country: str) -> str:
    """Normalize any casing/form of a country name ("POLAND", "poland",
    "Poland") to pycountry's standard English name ("Poland").

    Confirmed live: Clay's people-search country filters (both
    ``location_countries_include`` in filters-mode and ``location_country``
    in query-mode) are case-sensitive and silently return zero results for
    "POLAND" where "Poland" works -- not an error, just an empty result,
    which is what makes it easy to miss. Since callers (a database column
    storing an ALL-CAPS country code, for one real example) can't be trusted
    to already pass the exact casing Clay expects, ``find()`` normalizes
    through this before it reaches any Clay call. Falls back to the raw
    input, stripped, if unresolved -- better than crashing, though contact
    search will likely come back empty in that case too.
    """
    match = _lookup_country(country)
    if match:
        return match.name
    logger.warning(
        "could not normalize country %r -- passing it through as-is, Clay's contact "
        "search may silently return nothing if the casing doesn't match what it expects",
        country,
    )
    return (country or "").strip()

# Aggregators / social / directories that are never a company's own site.
_NON_OFFICIAL_DOMAINS = (
    "linkedin.com", "wikipedia.org", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "tiktok.com", "pinterest.com", "reddit.com",
    "crunchbase.com", "bloomberg.com", "europages.com", "kompass.com", "dnb.com",
    "zoominfo.com", "google.com", "yelp.com", "indeed.com", "ampliz.com",
)

def _domain_from_url(url: str | None) -> str | None:
    m = re.search(r"https?://([^/]+)", url or "")
    if not m:
        return None
    host = m.group(1).lower()
    return host[4:] if host.startswith("www.") else host


def _norm_domain(d: str | None) -> str:
    d = (d or "").lower()
    return d[4:] if d.startswith("www.") else d


def encode_linkedin_url(url: str | None) -> str | None:
    """Percent-encode a LinkedIn URL's path only, preserving scheme/host/query.

    Needed because Clay's "Linked In Profile" routine input validates as a
    strict URI, and raw Unicode characters in vanity slugs (accented names,
    etc.) fail that check.
    """
    if not url:
        return url
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, quote(parts.path, safe="/"), parts.query, parts.fragment))


class ClayContactFinder:
    """Finds and enriches B2B contacts for a company name + country, via Clay's Public API.

    All configuration -- API credentials and your Clay workspace's Function
    (routine) IDs alike -- is passed in explicitly at construction time. Never
    hardcode any of these values in source code that gets committed or
    distributed: load them from environment variables (a ``.env`` file kept
    out of version control, a secrets manager, your platform's config system)
    in the calling code, and pass the resulting values here. This class never
    reads the environment itself and ships with no default credentials or
    routine IDs of its own -- it has no dependency on, or knowledge of, any
    particular caller's configuration system or Clay workspace.

    Example:
        >>> import os
        >>> from dotenv import load_dotenv
        >>> load_dotenv()  # reads a local .env file that is NOT committed to version control
        >>> finder = ClayContactFinder(
        ...     clay_api_key=os.environ["CLAY_API_KEY"],
        ...     azure_api_key=os.environ["AZURE_API_KEY"],
        ...     azure_endpoint=os.environ["AZURE_ENDPOINT"],
        ...     azure_deployment=os.environ["AZURE_DEPLOYMENT"],
        ...     serper_api_key=os.environ.get("SERPER_API_KEY"),  # optional, see below
        ...     domain_routine_id=os.environ["CLAY_DOMAIN_ROUTINE_ID"],
        ...     enrich_company_routine_id=os.environ["CLAY_ENRICH_COMPANY_ROUTINE_ID"],
        ...     email_routine_id=os.environ["CLAY_EMAIL_ROUTINE_ID"],
        ...     phone_routine_id=os.environ["CLAY_PHONE_ROUTINE_ID"],
        ... )
        >>> result = finder.find("CPK", "Poland")
        >>> result.resolved_name
        'Centralny Port Komunikacyjny'
        >>> result.domain_resolution.domain
        'portpolska.pl'

    Args:
        clay_api_key: Clay Public API key (workspace-scoped).
        azure_api_key: Azure OpenAI API key.
        azure_endpoint: Azure OpenAI resource base URL, e.g.
            ``"https://<resource>.services.ai.azure.com/openai/v1"``.
        azure_deployment: Azure deployment name used for every LLM call this
            class makes (name canonicalization, domain resolution, arbitration).
            All calls use a low temperature and expect structured JSON output --
            a small/cheap deployment is sufficient, no need for a large model.
        serper_api_key: Optional. Enables the search-grounded domain resolver
            (§2). Without it, domain resolution falls back to Clay's Find
            Domain routine alone, which was measurably less accurate in
            testing (see README) -- a warning is logged once at construction.
        target_titles: Job titles to search for, the base list both the
            domain-based search (§4) and the name-search fallback (§5) start
            from. Defaults to a generic facilities/procurement list -- replace
            with whatever titles matter for your own use case.
        fallback_extra_titles: Extra titles added on top of ``target_titles``
            for BOTH search paths, used for every country. If omitted (the
            default), the LLM instead localizes generic role words ("manager",
            "director", ...) into each target country's own business language
            on first use per country, and caches the result -- necessary
            because both §4 and §5 match job titles as literal/keyword text,
            not semantically, and most real job titles outside
            English/French-speaking countries aren't written in either
            language (confirmed live: without this, a domain-based search on a
            verified, correct Polish company domain found 0 contacts; with it,
            2 real, LinkedIn-eligible ones that were there the whole time).
            Pass this explicitly only if you want the same fixed extra terms
            for every country regardless of language.
        domain_routine_id: Clay Function routine ID for name -> domain resolution.
        enrich_company_routine_id: Clay Function routine ID for domain -> canonical name/website.
        email_routine_id: Clay Function routine ID for work email resolution.
        phone_routine_id: Clay Function routine ID for mobile phone resolution.
        request_timeout: Per-HTTP-request timeout in seconds, for both Clay
            and Serper calls.

    Note:
        A Clay routine ID is workspace-specific -- it's generated when you
        publish a table column/action as a reusable Function in Clay's UI
        ("..." menu -> Publish as Function). There is no API to discover or
        list them, so all four IDs above are **required, with no default**:
        publish the four equivalent Functions in your own Clay workspace
        (see the README's Requirements section for each one's expected Setup
        Inputs and output field), then supply their IDs here. Passing an ID
        from a workspace your API key can't access fails loudly with a clear
        "Invalid tool id" error from Clay -- it does not silently misbehave.
    """

    CLAY_BASE_URL = "https://api.clay.com/public/v0"
    SERPER_URL = "https://google.serper.dev/search"

    def __init__(
        self,
        *,
        clay_api_key: str,
        azure_api_key: str,
        azure_endpoint: str,
        azure_deployment: str,
        domain_routine_id: str,
        enrich_company_routine_id: str,
        email_routine_id: str,
        phone_routine_id: str,
        serper_api_key: str | None = None,
        target_titles: list[str] | None = None,
        fallback_extra_titles: list[str] | None = None,
        request_timeout: float = 60.0,
    ) -> None:
        if not clay_api_key:
            raise ConfigurationError("clay_api_key is required")
        if not (azure_api_key and azure_endpoint and azure_deployment):
            raise ConfigurationError("azure_api_key, azure_endpoint, and azure_deployment are all required")
        if not (domain_routine_id and enrich_company_routine_id and email_routine_id and phone_routine_id):
            raise ConfigurationError(
                "domain_routine_id, enrich_company_routine_id, email_routine_id, and "
                "phone_routine_id are all required -- each is specific to your Clay "
                "workspace and has no default. See the class docstring / README."
            )

        self._clay_headers = {"clay-api-key": clay_api_key, "Content-Type": "application/json"}
        self._azure_api_key = azure_api_key
        self._azure_endpoint = azure_endpoint
        self._azure_deployment = azure_deployment
        self._serper_api_key = serper_api_key
        if not serper_api_key:
            logger.warning(
                "ClayContactFinder created without serper_api_key -- domain resolution will "
                "rely on Clay's Find Domain routine alone (no search-grounded resolver). "
                "This was measurably less accurate in testing; see the README."
            )

        self.target_titles = list(target_titles or DEFAULT_TARGET_TITLES)
        # None means "localize dynamically per country" -- see _get_search_titles().
        # An explicit list (even []) means "always use exactly this, skip localization".
        # Used by BOTH the domain-based search (§4) and the name-search fallback (§5).
        self._fallback_extra_titles_override = list(fallback_extra_titles) if fallback_extra_titles is not None else None
        self._localized_titles_cache: dict[str, list[str]] = {}

        self._domain_routine_id = domain_routine_id
        self._enrich_company_routine_id = enrich_company_routine_id
        self._email_routine_id = email_routine_id
        self._phone_routine_id = phone_routine_id
        self._timeout = request_timeout

        self._session = requests.Session()

    # =========================================================== low-level HTTP

    def _clay_request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.CLAY_BASE_URL}{path}"
        response = None
        for attempt in range(1, 4):
            response = self._session.request(method, url, headers=self._clay_headers, timeout=self._timeout, **kwargs)
            if response.status_code == 429:
                wait = float(response.headers.get("Retry-After", 2 * attempt))
                logger.info("Clay API rate-limited on %s %s, waiting %.1fs (attempt %d/3)", method, path, wait, attempt)
                time.sleep(wait)
                continue
            return response
        return response

    def _create_search(self, source_type: str, filters: dict) -> str:
        r = self._clay_request("POST", "/search/filters-mode", json={"source_type": source_type, "filters": filters})
        if r.status_code != 200:
            raise ClayAPIError(f"create_search failed [{r.status_code}]: {r.text[:300]}")
        return r.json()["search_id"]

    def _run_search(self, search_id: str, limit: int = 10) -> dict:
        r = self._clay_request("POST", f"/search/filters-mode/{search_id}/run", json={"limit": limit})
        if r.status_code != 200:
            raise ClayAPIError(f"run_search failed [{r.status_code}]: {r.text[:300]}")
        return r.json()

    def _run_single_item_routine(
        self, routine_id: str, inputs: dict, *, max_polls: int = 15, poll_interval: float = 3.0,
    ) -> dict | None:
        """Submit one item to a Clay Function and poll for its result.

        Returns the item's ``result`` dict, or ``None`` on any failure --
        submission rejected, routine errored on this item, or timed out.
        Never raises; a failed enrichment call is not a reason to crash a batch.
        """
        r = self._clay_request("POST", f"/routines/{routine_id}/run", json={"items": [{"id": "item", "inputs": inputs}]})
        if r.status_code != 202:
            logger.debug("routine %s submit failed [%d]: %s", routine_id, r.status_code, r.text[:200])
            return None
        run_id = r.json()["routine_run_id"]
        for _ in range(max_polls):
            rr = self._clay_request("GET", f"/routines/run/{run_id}/results")
            d = rr.json()
            if rr.status_code == 200 and d.get("status") == "complete":
                item = d["data"][0]
                return item.get("result") if item["status"] == "complete" else None
            time.sleep(poll_interval)
        logger.warning("routine %s run %s did not complete after %ds", routine_id, run_id, int(max_polls * poll_interval))
        return None

    _MAX_ROUTINE_BATCH_SIZE = 100  # Clay's own hard cap per /routines/{id}/run call -- confirmed live via a 400 "Too big" error on a 257-item submission

    def _run_batch_routine(
        self, routine_id: str, items: list[dict], *, max_polls: int, poll_interval: float,
    ) -> dict[str, dict]:
        """Submit many items to a Clay Function and poll for all results, chunking
        into <=100-item sub-batches as separate routine runs if needed -- Clay
        rejects a single submission larger than that outright (confirmed live:
        "Too big: expected array to have <=100 items" on a real 257-contact
        batch). Each chunk is submitted and polled independently and the
        results merged; one slow/failed chunk doesn't block the others.

        Returns ``{item_id: {"result": {...}} or {"error": "..."}}``.
        """
        if not items:
            return {}
        out: dict[str, dict] = {}
        for i in range(0, len(items), self._MAX_ROUTINE_BATCH_SIZE):
            chunk = items[i:i + self._MAX_ROUTINE_BATCH_SIZE]
            out.update(self._run_batch_routine_chunk(routine_id, chunk, max_polls=max_polls, poll_interval=poll_interval))
        return out

    def _run_batch_routine_chunk(
        self, routine_id: str, items: list[dict], *, max_polls: int, poll_interval: float,
    ) -> dict[str, dict]:
        """Submit and poll a single routine run for <=100 items. See _run_batch_routine."""
        r = self._clay_request("POST", f"/routines/{routine_id}/run", json={"items": items})
        if r.status_code != 202:
            raise ClayAPIError(f"routine submit failed [{r.status_code}]: {r.text[:300]}")
        run_id = r.json()["routine_run_id"]

        for _ in range(max_polls):
            rr = self._clay_request("GET", f"/routines/run/{run_id}/results", params={"limit": 100})
            if rr.status_code not in (200, 202):
                raise ClayAPIError(f"routine poll failed [{rr.status_code}]: {rr.text[:300]}")
            d = rr.json()
            if rr.status_code == 200 and d.get("status") == "complete":
                # Follow pagination explicitly -- the results endpoint defaults to a
                # 20-item page and silently truncates larger batches otherwise. This
                # loop is about paging through ONE chunk's results (<=100 items), a
                # separate concern from the outer chunking of the submission itself.
                all_items = list(d["data"])
                cursor = d.get("cursor")
                while cursor:
                    rr2 = self._clay_request("GET", f"/routines/run/{run_id}/results", params={"limit": 100, "cursor": cursor})
                    d2 = rr2.json()
                    all_items.extend(d2.get("data", []))
                    cursor = d2.get("cursor")
                out = {}
                for item in all_items:
                    if item["status"] == "complete":
                        out[item["id"]] = {"result": item["result"]}
                    else:
                        out[item["id"]] = {"error": (item.get("error") or {}).get("message", "unknown error")}
                return out
            time.sleep(poll_interval)
        raise ClayAPIError(f"routine run {run_id} did not complete after {int(max_polls * poll_interval)}s")

    def _llm_json(self, messages: list[dict], *, temperature: float) -> dict:
        raw = azure_openai_complete(
            messages, api_key=self._azure_api_key, endpoint=self._azure_endpoint,
            deployment=self._azure_deployment, temperature=temperature, json_mode=True,
        )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("LLM returned unparseable JSON (truncated): %s", raw[:200])
            return {}

    # =========================================================== §1 name canonicalization

    def _canonicalize_company_name(self, company_name: str, country: str) -> dict:
        system = (
            "You resolve a company, organization, or municipality name so it can be "
            "used to look up its real website domain. Given a possibly informal, "
            "abbreviated, or acronym name and its country, return the real, "
            "official, or most commonly used full name -- the name someone would "
            "search for to find its actual website. Expand unclear abbreviations/ "
            "acronyms into the full name (e.g. a ministry acronym into its full "
            "ministry name). Do NOT expand a name that is genuinely known primarily "
            "by an abbreviation in its own country/language (e.g. IBM, NASA, BASF, "
            "PKO BP, SNCF) -- keep those as-is; this applies equally regardless of "
            "which country the entity is in. If the input is already clear, return "
            "it unchanged. Respond ONLY with strict JSON: {\"resolved_name\": \"...\"}"
        )
        user = f'Name: "{company_name}"\nCountry: "{country}"'
        parsed = self._llm_json(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.1,
        )
        if not parsed.get("resolved_name"):
            return {"resolved_name": company_name}
        return parsed

    # =========================================================== §2 two domain candidates

    def _serper_search(self, query: str, country: str) -> tuple[str, str | None]:
        """Live Google search via Serper, country-biased. Returns (evidence_text, knowledge_graph_website)."""
        if not self._serper_api_key:
            return "", None
        gl = _country_to_gl(country)
        payload = {"q": query, "num": 8}
        if gl:
            payload["gl"] = gl
        try:
            r = requests.post(
                self.SERPER_URL,
                headers={"X-API-KEY": self._serper_api_key, "Content-Type": "application/json"},
                json=payload, timeout=20,
            )
        except requests.RequestException as exc:
            return f"(Serper request failed: {exc})", None
        if r.status_code != 200:
            return f"(Serper returned HTTP {r.status_code})", None

        data = r.json()
        lines = []
        kg = data.get("knowledgeGraph")
        kg_website = kg.get("website") if kg else None
        if kg:
            lines.append(f"[Knowledge Graph] {kg.get('title')} | {kg.get('type')} | website={kg_website}")
        for hit in data.get("organic", [])[:6]:
            dom = _domain_from_url(hit.get("link", ""))
            flag = " (directory/social -- likely NOT the official site)" if dom in _NON_OFFICIAL_DOMAINS else ""
            lines.append(f"- {hit.get('title')} | {hit.get('link')} | {(hit.get('snippet') or '')[:150]}{flag}")
        return "\n".join(lines), kg_website

    def _resolve_domain_search_grounded(self, company_name: str, country: str) -> dict | None:
        """Candidate #1: country-biased live search + LLM judgment."""
        evidence, kg_website = self._serper_search(f'"{company_name}" official website', country)
        system = (
            "You resolve a company name + country to its correct, real corporate domain, "
            "using live web search evidence below -- judge critically, evidence from news "
            "articles, business directories, and lead-gen sites is NOT the company's own "
            "domain even when it ranks highly. Prefer the company's actual real domain: "
            "return the GLOBAL/parent domain if that's genuinely what the company uses "
            "everywhere (many multinationals have no distinct country-specific site), and "
            "only return a country-specific domain if that specific country's entity "
            "genuinely operates its own distinct site. Many countries run a single shared "
            "portal domain that hosts pages for many different government agencies/ministries "
            "at once (regardless of its specific naming pattern -- e.g. gov.pl, gov.uk, "
            "gouv.fr, bund.de, and equivalents elsewhere) -- that shared portal is never a "
            "valid domain for one specific agency; set domain to null in that case. If the "
            "evidence doesn't clearly show an "
            "official site, set domain to null rather than guessing. Respond ONLY with "
            'strict JSON: {"domain": "..." or null, "confidence": "high|medium|low", '
            '"notes": "..."}. domain must be a bare hostname (no scheme, no www.).'
        )
        user = (
            f"Company name: {company_name}\nCountry: {country}\n\n"
            f"Knowledge Graph website (if any): {kg_website}\n\n"
            f"Web search evidence:\n{evidence or '(no search evidence available)'}"
        )
        parsed = self._llm_json(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.1,
        )
        domain = parsed.get("domain")
        if not domain:
            return None
        return {"source": "serper_llm", "domain": domain, "confidence": parsed.get("confidence"),
                 "notes": parsed.get("notes", "")}

    def _find_domain_clay(self, company_name: str) -> str | None:
        """Clay's native Find Domain routine."""
        result = self._run_single_item_routine(self._domain_routine_id, {"Company Name": company_name})
        return (result or {}).get("Domain")

    def _resolve_domain_clay(self, company_name: str) -> dict | None:
        """Candidate #2, wrapped in the same shape as candidate #1."""
        domain = self._find_domain_clay(company_name)
        if not domain:
            return None
        return {"source": "clay_find_domain", "domain": domain, "confidence": None, "notes": ""}

    # =========================================================== §3 arbitration + verification

    def _enrich_company_domain(self, domain: str | None) -> dict | None:
        """Clay's Enrich Company routine -- canonicalizes a domain and returns its Name/Website."""
        if not domain:
            return None
        return self._run_single_item_routine(self._enrich_company_routine_id, {"Domain": domain})

    def _arbitrate_domain(self, resolved_name: str, country: str, candidate_a: dict, candidate_b: dict) -> dict:
        """Decide which of two disagreeing candidates is more likely the
        target's real domain -- always picks one, deliberately no "reject
        both" option. An earlier version could reject both (e.g. for a
        shared government portal) and return no domain at all; removed for
        speed and simplicity, and because a human reviewing the result can
        always see which domain was used and correct it directly
        (``find(..., known_domain=...)``, wired to the web app's "wrong?
        enter a domain" form shown after every search) -- a more reliable
        fix than chasing every automated edge case, and better than leaving
        the caller with nothing to search or correct at all.

        Only called when both resolvers found something and disagreed -- a
        single candidate is trusted directly with no LLM call at all, see
        _resolve_domain.
        """
        candidates_desc = [
            f"- Source: {c['source']}\n"
            f"  Domain: {c['domain']}\n"
            f"  Clay's own name for that domain (via Enrich Company): {c.get('enriched_name')!r}\n"
            f"  Notes: {c.get('notes', '')}"
            for c in (candidate_a, candidate_b)
        ]
        system = (
            "You are given a target company name/country and two CANDIDATE domains found by "
            "two independent methods (a search-grounded resolver, and Clay's own company-domain "
            "lookup tool), each cross-checked against Clay's own company database (Enrich Company). "
            "They disagree -- decide which one is more likely to be the target company's real "
            "domain, and always pick one, even if your confidence is low; a human will review the "
            "result afterward and can correct it directly if you're wrong, so picking the more "
            "plausible candidate is always better than refusing to choose. "
            "Prefer the company's actual real domain: a GLOBAL/parent domain if that's genuinely "
            "what the company uses everywhere (many multinationals have no distinct country-specific "
            "site), a country-specific domain/subdomain only if that country's entity genuinely "
            "operates its own distinct site. When one candidate is a shared portal domain hosting "
            "many different organizations at once (e.g. gov.pl, gov.uk, gouv.fr, bund.de, and "
            "equivalents elsewhere), prefer the other candidate if it's more specific to the target "
            "-- but still pick between them if both are shared portals or you're otherwise unsure. "
            'Respond ONLY with strict JSON: {"domain": "...", "source": '
            '"serper_llm"|"clay_find_domain", "reason": "..."}'
        )
        user = f'Target company: "{resolved_name}"\nCountry: "{country}"\n\nCandidates:\n' + "\n".join(candidates_desc)
        return self._llm_json(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.0,
        )

    def _resolve_domain(self, resolved_name: str, country: str) -> DomainResolution:
        """§2 + §3 combined: run both candidates, Enrich-Company-check each,
        and always settle on one trusted domain if either resolver found
        anything at all -- never a rejected/withheld result. Downstream,
        find() no longer runs a separate plain-text name-match check either;
        whatever domain this method settles on is trusted directly. See
        _arbitrate_domain's docstring for why: automated over-caution here
        was a repeated source of real false negatives this pipeline hit in
        practice (an LLM's own confidence isn't a reliable enough signal to
        withhold a result), and a human reviewing the outcome is a more
        dependable safety net than another automated check.

        The two candidate resolvers are independent of each other (one calls
        Serper + Azure OpenAI, the other calls Clay's Find Domain routine), and
        so are their two Enrich Company checks once domains are known -- this is
        the dominant cost in find(), so both pairs run concurrently via a thread
        pool rather than back-to-back. Sharing self._session across threads here
        is intentional: it's never mutated after construction (headers are
        passed per-call, not set on the session), which is the documented safe
        pattern for concurrent use of a single requests.Session.
        """
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(self._resolve_domain_search_grounded, resolved_name, country)
            future_b = pool.submit(self._resolve_domain_clay, resolved_name)
            cand_a = future_a.result()
            cand_b = future_b.result()

        def _enrich(c: dict | None) -> dict | None:
            if not c:
                return None
            e = self._enrich_company_domain(c["domain"])
            c["enriched_name"] = e.get("Name") if e else None
            c["canonical_domain"] = c["domain"]
            if e:
                m = re.match(r"https?://([^/]+)", e.get("Website") or "")
                if m:
                    c["canonical_domain"] = m.group(1).removeprefix("www.")
            return c

        if cand_a and cand_b:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                future_a = pool.submit(_enrich, cand_a)
                future_b = pool.submit(_enrich, cand_b)
                cand_a = future_a.result()
                cand_b = future_b.result()
        else:
            cand_a = _enrich(cand_a)
            cand_b = _enrich(cand_b)

        if cand_a and cand_b and _norm_domain(cand_a["canonical_domain"]) == _norm_domain(cand_b["canonical_domain"]):
            return DomainResolution(
                domain=cand_a["domain"], canonical_domain=cand_a["canonical_domain"],
                enriched_name=cand_a["enriched_name"], source="agreement",
                reason="both resolvers independently agreed",
            )

        if cand_a and cand_b:
            verdict = self._arbitrate_domain(resolved_name, country, cand_a, cand_b)
            chosen_domain = verdict.get("domain")
            chosen = None
            if chosen_domain:
                cd = _norm_domain(chosen_domain)
                for c in (cand_a, cand_b):
                    if c and _norm_domain(c["domain"]) == cd:
                        chosen = c
                        break
            if chosen is None:
                # arbiter returned something outside the two actual candidates
                # (shouldn't happen -- forced to name one of them -- but stay
                # defensive) -- default to the search-grounded candidate, the
                # one with the stronger independently-verified track record
                # (see the README's design rationale numbers).
                chosen = cand_a
            return DomainResolution(
                domain=chosen["domain"], canonical_domain=chosen["canonical_domain"],
                enriched_name=chosen["enriched_name"], source=chosen["source"],
                reason=verdict.get("reason", ""),
            )

        single = cand_a or cand_b
        if single:
            return DomainResolution(
                domain=single["domain"], canonical_domain=single["canonical_domain"],
                enriched_name=single["enriched_name"], source=single["source"],
                reason="only one resolver found a candidate, trusted directly",
            )

        return DomainResolution(domain=None, canonical_domain=None, enriched_name=None,
                                  source=None, reason="neither resolver found a domain")

    # =========================================================== §4 domain-based contact search

    def _find_contacts(
        self, domain: str | None, country: str, company_name: str | None,
        project_context: str | None = None,
    ) -> list[Contact]:
        """Country-filtered people search on a confirmed domain. Two-step:
        a precise keyword-filtered title search first, and only if that
        finds nobody, an unfiltered broad pull + LLM pick (see
        _pick_contacts_broad) -- a domain we already know is correct
        shouldn't come back empty just because its employees' real job
        titles don't match a fixed keyword list.
        """
        if not domain:
            return []
        try:
            search_id = self._create_search("people", {
                "company_identifier": [domain],
                "location_countries_include": [country],
                "job_title_keywords": self._get_search_titles(country),
            })
            results = self._run_search(search_id, limit=8).get("data", [])
        except ClayAPIError as exc:
            logger.debug("contact search failed for domain %r: %s", domain, exc)
            results = []

        contacts = [self._domain_result_to_contact(p, domain, company_name) for p in results]
        if contacts:
            return contacts

        logger.debug("keyword search found nobody on confirmed domain %r -- trying broad pick", domain)
        return self._pick_contacts_broad(domain, country, company_name, project_context)

    def _domain_result_to_contact(self, p: dict, domain: str, company_name: str | None) -> Contact:
        matched = p.get("matched_experience") or {}
        return Contact(
            name=p.get("name"),
            title=matched.get("job_title") or p.get("latest_experience_title"),
            company_name=company_name,
            domain=domain,
            linkedin_url=p.get("url"),
            city=(p.get("structured_location") or {}).get("city"),
            source="domain_search",
        )

    # =========================================================== §4b broad search + LLM pick

    #: How many people to pull, unfiltered by title, when the keyword search
    #: (§4 or §5) finds nobody. Deliberately generous -- this only runs on the
    #: minority of cases where the keyword search already failed, so the cost
    #: of being thorough here is paid rarely. Confirmed live that Clay's
    #: search endpoint accepts this in one call with no hard cap (has_more was
    #: still true at 200 for a large company -- there was more available).
    #: Still country-filtered, same as the keyword search -- only the title
    #: filter is dropped, never the country one.
    _BROAD_SEARCH_LIMIT = 200

    def _pick_contacts_broad(
        self, domain: str, country: str, company_name: str | None, project_context: str | None,
    ) -> list[Contact]:
        """Last resort before giving up on a confirmed-correct domain: pull
        up to _BROAD_SEARCH_LIMIT people Clay has indexed there in this
        country (no title filter) and let the LLM pick, rather than
        requiring a literal/keyword title match. Only called from
        _find_contacts when its keyword search already found nobody.
        """
        try:
            search_id = self._create_search("people", {
                "company_identifier": [domain],
                "location_countries_include": [country],
            })
            results = self._run_search(search_id, limit=self._BROAD_SEARCH_LIMIT).get("data", [])
        except ClayAPIError as exc:
            logger.debug("broad contact search failed for domain %r: %s", domain, exc)
            return []
        contacts = [self._domain_result_to_contact(p, domain, company_name) for p in results]
        return self._llm_pick_from_contacts(company_name or domain, contacts, project_context)

    def _llm_pick_from_contacts(
        self, company_name: str, contacts: list[Contact], project_context: str | None,
    ) -> list[Contact]:
        """Given an unfiltered pool of real Contact candidates (still
        country-filtered, just not title-filtered), ask the LLM to pick the
        best 3-10 as initial points of contact (see _pick_best_contacts_llm)
        and return just those, in the LLM's ranked order. Matches picks back
        to contacts by index, not name text -- deliberate, given this
        pipeline's history of literal-text-matching bugs elsewhere. Shared by
        both the domain-based broad pick (§4b) and the name-search broad pick
        (§5b).
        """
        if not contacts:
            return []
        candidates = [
            {"index": i, "name": c.name or "Unknown", "title": c.title or "Unknown"}
            for i, c in enumerate(contacts)
        ]
        picks = self._pick_best_contacts_llm(company_name, candidates, project_context)
        picked: list[Contact] = []
        seen: set[int] = set()
        for pick in picks:
            idx = pick.get("index") if isinstance(pick, dict) else None
            if not isinstance(idx, int) or idx in seen or not (0 <= idx < len(contacts)):
                continue
            seen.add(idx)
            picked.append(contacts[idx])
        if contacts and not picked:
            logger.warning(
                "broad pick for %r returned %d real candidates but the LLM picked none -- "
                "expected it to always pick the closest available people when the pool isn't empty",
                company_name, len(contacts),
            )
        return picked

    def _pick_best_contacts_llm(
        self, company_name: str, candidates: list[dict], project_context: str | None,
    ) -> list[dict]:
        """Ask the LLM to rank 3-10 of the given (real, numbered) candidates
        as initial points of contact -- fewer only if the candidate pool
        itself has fewer than 3 people. Deliberately told to always pick the
        closest available people rather than return fewer/nothing when
        relevance is weak -- any real employee at a confirmed-correct company
        is a better starting point for outreach than no contact at all, since
        they can redirect internally. Only an empty candidate list should
        produce an empty result.
        """
        if not candidates:
            return []
        candidate_lines = "\n".join(f'{c["index"]}: {c["name"]} -- {c["title"]}' for c in candidates)
        context_line = f"\nSpecific opportunity: {project_context}" if project_context else ""
        system = (
            "You are picking initial points of contact at a company for a B2B sales "
            "outreach about a specific project or business need (described below when "
            "known). You are given a numbered list of real people who work at the "
            "target company, with their job titles. Pick between 3 and 10, ranked by "
            "how likely each is to be a useful contact -- someone with real influence "
            "over or need related to the opportunity (operations, facilities, site or "
            "project management, procurement, or the relevant technical function) is "
            "a strong match; people in clearly unrelated functions "
            "(legal, marketing, finance, HR, communications, etc.) are weak matches but "
            "still usable as a last resort. "
            "IMPORTANT: pick at least 3 whenever at least 3 real candidates exist -- if "
            "none of the candidates are a strong topical match, still pick the closest "
            "available people rather than returning fewer; any real employee is a "
            "better starting point than no contact at all. Only return fewer than 3 if "
            "the candidate list itself has fewer than 3 people, and only return an "
            'empty list if it is empty. Respond ONLY with strict JSON: {"picks": '
            '[{"index": N, "reason": "..."}, ...]}'
        )
        user = f"Target company: {company_name}{context_line}\n\nCandidates:\n{candidate_lines}"
        parsed = self._llm_json(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.1,
        )
        picks = parsed.get("picks")
        return picks if isinstance(picks, list) else []

    # =========================================================== title localization (§4)

    def _localize_title_terms(self, titles: list[str], country: str) -> list[str]:
        """Ask the LLM for generic, idiomatic local-language role words (not full
        translations of each title) that broaden §4's job-title search in this
        country. Needed because job titles are matched as literal/keyword text,
        not semantically -- an English/French-only title list silently misses
        most real job titles in, say, Germany or Poland, where "Director
        General" never appears as that exact phrase. Confirmed live: widening
        a verified company's domain-based search from target_titles alone (0 results) to
        include localized terms found 2 real, LinkedIn-eligible contacts that
        were there the whole time -- a resolved, verified domain doesn't by
        itself find anyone if the title filter applied to it doesn't match how
        people locally describe their job."""
        system = (
            "You help broaden a literal text search over job titles for a specific "
            "country. Given a list of target job titles and a country, return a short "
            "list (3-6 items) of generic, idiomatic words or short phrases that "
            "commonly appear INSIDE real job titles in that country's dominant "
            "business language(s) for similar roles (manager, director, head of "
            "department, etc.) -- not literal translations of the input titles, just "
            "the generic role-level words themselves that a literal substring match "
            "would need (e.g. for Poland: \"Kierownik\", \"Dyrektor\"; for Germany: "
            "\"Leiter\", \"Geschäftsführer\"; for Japan, transliterate if that's how "
            "titles typically appear in Latin-script profile data). If the country's "
            "dominant business language is already covered by the input titles' own "
            "language, return an empty list -- no extra terms are needed. Respond "
            'ONLY with strict JSON: {"local_title_terms": ["...", ...]}'
        )
        user = f"Target job titles: {titles}\nCountry: {country}"
        parsed = self._llm_json(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.1,
        )
        terms = parsed.get("local_title_terms")
        return [t for t in terms if isinstance(t, str) and t.strip()] if isinstance(terms, list) else []

    def _get_search_titles(self, country: str) -> list[str]:
        """target_titles plus this country's localized extra terms, cached per
        country. Used by §4's keyword-filtered domain search.

        If ``fallback_extra_titles`` was passed at construction time, that fixed
        list is used for every country and localization is skipped entirely --
        an explicit override always wins over the dynamic default.
        """
        if self._fallback_extra_titles_override is not None:
            return self.target_titles + self._fallback_extra_titles_override
        if country not in self._localized_titles_cache:
            extras = self._localize_title_terms(self.target_titles, country)
            logger.debug("localized search title terms for %s: %s", country, extras)
            self._localized_titles_cache[country] = self.target_titles + extras
        return self._localized_titles_cache[country]

    # =========================================================== public API

    def find(
        self, company_name: str, country: str, project_context: str | None = None,
        *, known_domain: str | None = None, on_progress: Callable[[str], None] | None = None,
    ) -> EnrichmentResult:
        """Run the pipeline for one name: canonicalize, resolve a domain, search it once.

        known_domain: skip name canonicalization and domain resolution
        entirely and trust this domain directly -- for callers who already
        know the right domain (a human confirming or correcting a prior
        result -- the web app's "wrong? enter a domain" form, shown after
        every search, uses this). ``domain_resolution.source`` comes back as
        ``"manual_override"``.

        on_progress: optional callback invoked with a short human-readable
        label at each stage, for a caller that wants to show live progress
        during the ~15-60s a call typically takes (almost entirely network
        wait, not local compute). Defaults to a no-op; every existing caller
        is unaffected.

        Domain resolution always runs -- there is no upfront "is this a real
        company" judgment call gating it. That gate existed in an earlier
        version and was removed: the actual goal is finding contacts, and the
        downstream check (does the country-filtered people search actually
        return anyone) is the real, evidence-based gate. A pre-filter based on
        the LLM's opinion of whether a name "looks like" a real company caused
        real misses -- confirmed live: "Port Polska" was skipped by that gate
        even though the identical domain (portpolska.pl) resolves correctly
        and returns 8 real contacts under a different name for the same
        entity ("Centralny Port Komunikacyjny"). Worst case without the gate
        is a wasted call on a genuinely unresolvable input, not a wrong
        result -- the country-filtered contact search already fails safely on
        its own for those.

        Deliberately simple, one attempt, no automatic fallback: domain
        resolution always settles on one domain if either resolver found
        anything at all (see _resolve_domain -- it no longer withholds a
        result), that domain is trusted directly with no further plain-text
        confirmation step, and §4's contact search (keyword-filtered first,
        broadened + LLM-picked if that finds nobody) is tried exactly once on
        it. If that comes back empty -- or no domain was found at all -- this
        returns ``contacts=[]`` rather than trying a second, different search
        mechanism automatically. An earlier version had a whole second
        fallback tier (a name-based search with its own confirmation step);
        it was removed by explicit request in favor of this: automated
        guessing has a ceiling, a human reviewing the result and correcting
        the domain directly (``known_domain``) doesn't.

        ``project_context`` (e.g. the specific opportunity's project type or
        need) sharpens the broadened-search LLM pick when supplied, optional.

        Never raises for "no results" -- simply comes back with
        ``contacts=[]``. Can raise if Azure OpenAI or Clay are unreachable/
        misconfigured, or a response Clay is contractually expected to return
        is malformed.
        """
        logger.info("find(%r, %r, known_domain=%r)", company_name, country, known_domain)
        report = on_progress or (lambda _stage: None)

        # Normalize whatever casing/form the caller passed ("POLAND" from a
        # database column, "Poland", "poland", ...) to what Clay's contact
        # search actually requires -- see _canonical_country_name's docstring
        # for the live-confirmed bug this fixes. EnrichmentResult.input_country
        # keeps the original, unnormalized value for traceability.
        country_norm = _canonical_country_name(country)

        if known_domain:
            # Caller already knows the right domain (e.g. a human correcting
            # a prior automated miss) -- trust it directly, skip
            # canonicalization and resolution entirely.
            resolved_name = company_name
            domain_resolution = DomainResolution(
                domain=known_domain, canonical_domain=known_domain, enriched_name=None,
                source="manual_override", reason="domain provided directly by caller, resolution skipped",
            )
        else:
            report("Canonicalizing company name…")
            canon = self._canonicalize_company_name(company_name, country_norm)
            resolved_name = canon.get("resolved_name") or company_name
            logger.debug("canonicalized -> %r", resolved_name)

            report("Resolving domain…")
            domain_resolution = self._resolve_domain(resolved_name, country_norm)

        domain = domain_resolution.canonical_domain or domain_resolution.domain
        name_confirmed = bool(domain)
        logger.debug("domain resolution -> %r (source=%s)", domain, domain_resolution.source)

        contacts: list[Contact] = []
        if name_confirmed:
            report("Searching contacts…")
            contacts = self._find_contacts(domain, country_norm, resolved_name, project_context)

        return EnrichmentResult(
            input_name=company_name,
            input_country=country,
            resolved_name=resolved_name,
            domain_resolution=domain_resolution,
            name_confirmed=name_confirmed,
            contacts=contacts,
        )

    def find_batch(self, companies: list[tuple[str, str]]) -> list[EnrichmentResult]:
        """Run :meth:`find` for a list of ``(company_name, country)`` pairs, sequentially."""
        return [self.find(name, country) for name, country in companies]

    def enrich_email(
        self, contacts: list[Contact], *, max_polls: int = 30, poll_interval: float = 4.0,
    ) -> list[Contact]:
        """Resolve a work email for each email-eligible contact (mutates and returns the same list).

        In practice every contact ``find()`` returns is eligible now (see
        Contact.source's docstring) -- the ``email_eligible`` filter here is
        a defensive check (``bool(linkedin_url)``), not expected to actually
        exclude anything for contacts that came from this package's own
        ``find()``. A contact built by hand without a ``linkedin_url`` is
        simply skipped -- ``work_email`` stays ``None``, no error recorded.
        """
        return self._enrich_batch(
            contacts, routine_id=self._email_routine_id, result_field="Work Email",
            success_attr="work_email", error_attr="work_email_error",
            build_inputs=lambda c: {
                "Company Domain": c.domain,
                "First Name": c.name.split()[0],
                "Last Name": " ".join(c.name.split()[1:]) or c.name,
                "Linked In Profile": encode_linkedin_url(c.linkedin_url),
                "Company Name": c.company_name,
            },
            max_polls=max_polls, poll_interval=poll_interval,
        )

    def enrich_phone(
        self, contacts: list[Contact], *, max_polls: int = 60, poll_interval: float = 5.0,
    ) -> list[Contact]:
        """Resolve a mobile phone number for each email-eligible contact (mutates and returns the same list).

        Same eligibility rule as :meth:`enrich_email`. Uses a larger default
        poll budget -- this routine was observed to take longer per batch.
        """
        return self._enrich_batch(
            contacts, routine_id=self._phone_routine_id, result_field="Mobile Phone",
            success_attr="work_phone", error_attr="work_phone_error",
            build_inputs=lambda c: {
                "Full Name": c.name,
                "First Name": c.name.split()[0],
                "Last Name": " ".join(c.name.split()[1:]) or c.name,
                "Company Domain": c.domain,
                "Linked In Profile": encode_linkedin_url(c.linkedin_url),
                "Company Name": c.company_name,
            },
            max_polls=max_polls, poll_interval=poll_interval,
        )

    def _enrich_batch(
        self, contacts: list[Contact], *, routine_id: str, result_field: str,
        success_attr: str, error_attr: str, build_inputs, max_polls: int, poll_interval: float,
    ) -> list[Contact]:
        eligible = [(i, c) for i, c in enumerate(contacts) if c.email_eligible]
        if not eligible:
            return contacts
        items = [{"id": str(i), "inputs": build_inputs(c)} for i, c in eligible]
        results = self._run_batch_routine(routine_id, items, max_polls=max_polls, poll_interval=poll_interval)
        for i, c in eligible:
            outcome = results.get(str(i), {})
            if "result" in outcome:
                # Clay returns "" (not null) for some not-found cases -- normalize
                # to None so callers can rely on a single falsy check either way.
                setattr(c, success_attr, outcome["result"].get(result_field) or None)
            elif "error" in outcome:
                setattr(c, error_attr, outcome["error"])
        return contacts
