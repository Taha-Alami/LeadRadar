# clay-contact-finder

Given a company name and country, finds real decision-maker contacts (name,
title, LinkedIn URL) and — optionally — their work email and mobile phone,
using [Clay](https://www.clay.com/)'s Public API directly. No Clay table, no
webhook, no manual enrichment step in Clay's UI.

```python
import os
from dotenv import load_dotenv
from clay_contact_finder import ClayContactFinder

load_dotenv()  # reads a local .env file that is NOT committed to version control

finder = ClayContactFinder(
    clay_api_key=os.environ["CLAY_API_KEY"],
    azure_api_key=os.environ["AZURE_API_KEY"],
    azure_endpoint=os.environ["AZURE_ENDPOINT"],
    azure_deployment=os.environ["AZURE_DEPLOYMENT"],
    domain_routine_id=os.environ["CLAY_DOMAIN_ROUTINE_ID"],
    enrich_company_routine_id=os.environ["CLAY_ENRICH_COMPANY_ROUTINE_ID"],
    email_routine_id=os.environ["CLAY_EMAIL_ROUTINE_ID"],
    phone_routine_id=os.environ["CLAY_PHONE_ROUTINE_ID"],
    serper_api_key=os.environ.get("SERPER_API_KEY"),  # optional, see "Why two domain resolvers" below
)

result = finder.find("CPK", "Poland")
print(result.resolved_name)          # "Centralny Port Komunikacyjny"
print(result.domain_resolution.domain)  # "portpolska.pl"
for c in result.contacts:
    print(c.name, "|", c.title, "|", c.linkedin_url)

finder.enrich_email(result.contacts)   # mutates each Contact.work_email in place
finder.enrich_phone(result.contacts)   # mutates each Contact.work_phone in place
```

This design was arrived at empirically — several alternatives for each stage
were tested head-to-head on a real dataset of company names before this one
was kept. That evidence is summarized under [Design rationale](#design-rationale)
below, with real numbers, not just claims.

---

## Table of contents

- [Installation](#installation)
- [Requirements — what your Clay workspace needs](#requirements--what-your-clay-workspace-needs)
- [Configuration reference](#configuration-reference)
- [The pipeline, stage by stage](#the-pipeline-stage-by-stage)
- [Data model](#data-model)
- [Multi-country support](#multi-country-support)
- [Design rationale](#design-rationale)
- [Known limitations](#known-limitations)
- [Batch usage](#batch-usage)

---

## Installation

```bash
pip install -e ./clay_contact_finder     # editable install from a local checkout
```

or copy the `clay_contact_finder/` package directory into any other project —
it has no dependency on the project it originated in, only on its own three
declared dependencies: `requests`, `openai`, and `pycountry`.

---

## Requirements — what your Clay workspace needs

This package calls **four Clay Functions** via `POST /routines/{routine_id}/run`.
A routine ID is workspace-specific — it's generated when you publish a Clay
table column/action as a reusable Function ("..." menu → **Publish as
Function**) — there is no API to list or discover them. All four IDs are
**required constructor arguments with no default**: publish the four
equivalent Functions in your own Clay workspace, then pass their IDs in.
Never hardcode a routine ID (or any credential) directly in source code —
load it from an environment variable / secrets manager in your own calling
code, the same way you'd handle an API key. Passing an ID from a workspace
your API key can't access fails loudly with a clear "Invalid tool id" error
from Clay; it will not silently misbehave or leak data cross-workspace.

| Purpose | Constructor arg | Required Setup Inputs | Output field used |
|---|---|---|---|
| Name → domain | `domain_routine_id` | `Company Name` | `Domain` |
| Canonicalize/verify a domain | `enrich_company_routine_id` | `Domain` | `Name`, `Website` |
| Domain-based people search | *(not a routine — uses `/search/filters-mode`)* | — | — |
| Work email | `email_routine_id` | `Company Domain`, `First Name`, `Last Name`, `Linked In Profile`, `Company Name` | `Work Email` |
| Work phone | `phone_routine_id` | `Full Name`, `First Name`, `Last Name`, `Company Domain`, `Linked In Profile`, `Company Name` | `Mobile Phone` |

You also need:
- A **Clay Public API key** (`clay_api_key`) — Clay workspace settings → API.
- An **Azure OpenAI** deployment (`azure_api_key`, `azure_endpoint`,
  `azure_deployment`) — any small/cheap chat model works; every call this
  package makes uses low temperature and expects structured JSON output, never
  creative generation.
- Optionally, a **Serper** API key (`serper_api_key`, [serper.dev](https://serper.dev)) —
  strongly recommended, see [Why two domain resolvers](#why-two-independent-domain-resolvers-not-one).

---

## Configuration reference

Every argument below is passed to the `ClayContactFinder` constructor.
**Nothing is read from environment variables internally** — this class has no
knowledge of `.env` files, variable names, or any particular secrets-management
approach. That's a deliberate boundary: the *caller* decides how credentials
and workspace IDs are sourced (a `.env` file kept out of version control, a
secrets manager, a platform's config system) and passes the resulting plain
values in. Treat `domain_routine_id` / `enrich_company_routine_id` /
`email_routine_id` / `phone_routine_id` with the same care as an API key —
they identify configuration specific to your Clay workspace and should never
be hardcoded in source you commit or share.

| Argument | Required | Default | Notes |
|---|---|---|---|
| `clay_api_key` | ✅ | — | |
| `azure_api_key` | ✅ | — | |
| `azure_endpoint` | ✅ | — | |
| `azure_deployment` | ✅ | — | |
| `domain_routine_id` | ✅ | — | Your Clay workspace's Function ID for name → domain |
| `enrich_company_routine_id` | ✅ | — | Your Clay workspace's Function ID for domain → canonical name/website |
| `email_routine_id` | ✅ | — | Your Clay workspace's Function ID for work email resolution |
| `phone_routine_id` | ✅ | — | Your Clay workspace's Function ID for mobile phone resolution |
| `serper_api_key` | — | `None` | Without it, domain resolution uses Clay's Find Domain routine alone (see rationale below) |
| `target_titles` | — | generic facilities/procurement titles | Job titles searched for in the domain search's keyword-filtered step |
| `fallback_extra_titles` | — | `None` (dynamic per-country) | Added on top of `target_titles`. Name kept from an earlier version that also had a name-based fallback search — still just "extra title terms" today. If unset, the LLM localizes generic role words into each target country's own business language on first use, cached per country — see [Multi-country support](#multi-country-support). Pass a fixed list only if you want the same extra terms for every country. |
| `request_timeout` | — | `60.0` | Seconds, per HTTP request |

---

## The pipeline, stage by stage

### 1. Name canonicalization (LLM)
Input names are often informal or abbreviated (`"CPK"`, `"MON"`, `"PKP"`) —
not what actually appears as a company's registered or commonly-used name,
and the next stage resolves a domain *from a name*, so a bad name produces a
bad domain. An LLM call resolves the input into the real company name,
explicitly prompted with *why*: the output feeds a domain lookup. Genuine
abbreviations a company is actually known by (IBM, NASA) are left alone.

Domain resolution always runs unconditionally after this — there is no
upfront "is this a real company" judgment call gating it. An earlier version
had one, and it was removed: the actual goal is finding contacts, and the
downstream checks (does a domain actually get confirmed, does the
country-filtered people search actually return anyone) are the real,
evidence-based gates. A pre-filter based on the LLM's opinion of whether a
name "looks like" a real company caused real misses — confirmed live: a real
input was skipped by that gate even though its domain resolves correctly and
returns real, verified contacts under a different, more official name for the
same entity. Worst case without the gate is a wasted call on a genuinely
unresolvable input, not a wrong result — the country-filtered contact search
already fails safely on its own for those.

### 2. Two independent domain resolvers
Both run unconditionally, in parallel intent (sequentially in the current
implementation):

- **Search-grounded LLM.** A country-biased live Google search (via Serper)
  supplies real evidence — page titles, snippets, knowledge-graph data — and
  an LLM judges which result (if any) is genuinely the official site,
  explicitly told to distrust directories/news/lead-gen sites even when they
  rank highly, and to prefer the company's *real* domain (global if that's
  genuinely what it uses everywhere; country-specific only if that country's
  entity genuinely operates its own site).
- **Clay's native Find Domain routine.** Name in, domain out — a black box.

### 3. Cross-check + a forced pick
Each candidate domain is run through Clay's **Enrich Company** routine, which
canonicalizes it (fixes alias redirects, e.g. `pern.com.pl` → `pern.pl`) and
returns Clay's own name for whatever it found there — turning two bare
hostnames into two evidence-backed candidates.

- If both candidates land on the same canonical domain, that's accepted
  immediately (independent agreement, no LLM call spent).
- If only one resolver found anything, it's trusted directly — also no LLM
  call, there's nothing to arbitrate between.
- Only if both found something and they **disagree** does a dedicated LLM
  call arbitrate: given the target name/country and both candidates (domain +
  Clay's own name for it + supporting notes), it picks whichever is more
  likely correct. It's told to prefer a non-shared-portal candidate when a
  real alternative exists (`gov.pl`, `gov.uk` and equivalents are rarely a
  specific agency's real site), but **it is always forced to name one of the
  two** — there is no "reject both" option.

Whatever domain this settles on is trusted directly and passed straight to
contact search — there is no further plain-text name-match confirmation step.
An earlier version had both a "reject both" option in arbitration and a
separate plain-text check downstream; both were removed by explicit request
in favor of the next section.

### Why no more automated rejection

Automated over-caution here was a repeated, real source of false negatives in
practice — see [Design rationale](#design-rationale) for the specific cases.
An LLM's own confidence about "is this genuinely correct" turned out not to
be a reliable enough signal to withhold a result on. The replacement isn't
"trust everything blindly" — it's that **a human reviewing the outcome is a
more dependable safety net than another automated check**: `find()` accepts
a `known_domain` parameter for exactly this, and the web app this package was
built for shows the domain used after every search (found something or not)
with a "wrong? enter a domain" correction box wired directly to it.

```python
# A human already knows (or is correcting) the right domain -- skips
# canonicalization and resolution entirely, trusted directly.
result = finder.find("Sieć Badawcza Łukasiewicz – Instytut Lotnictwa", "Poland",
                      known_domain="ilot.lukasiewicz.gov.pl")
```

### 4. Contact search (domain-based) — the only search this pipeline runs

If a domain was found, a country-filtered Clay people search on that exact
domain looks for `target_titles` (plus this country's localized extra terms
— see [Multi-country support](#multi-country-support)). These contacts have
a LinkedIn URL (`Contact.source == "domain_search"`) and are eligible for
email/phone enrichment.

If that keyword search finds nobody, this stage doesn't give up on a domain
it already knows is correct — it internally broadens to an unfiltered pull of
up to 200 people Clay has indexed at that domain in that country, and asks an
LLM to pick the best 3-10 as initial points of contact, given `project_context`
(the specific opportunity, if you passed one to `find()`) when available. The
LLM is explicitly instructed to always pick real people rather than return
nothing when relevance is weak — any real employee at a confirmed-correct
company is a better starting point for outreach than no contact at all, since
they can redirect internally.

**If this comes back empty — or no domain was ever found at all — `find()`
stops there.** `contacts` is simply `[]`. An earlier version had a whole
second search mechanism (a name-based fallback with its own confirmation
step) that ran automatically when this one failed; it was removed by explicit
request, in favor of the "let a human correct the domain" approach above —
automated guessing has a ceiling, direct human correction doesn't, and the
old fallback added real latency and complexity chasing a shrinking set of
edge cases. See [Design rationale](#design-rationale) for the trade-off in
full.

### Progress reporting: `on_progress`

A single `find()` call is almost entirely network wait — pass
`on_progress=callback` to get a short label at each stage ("Canonicalizing
company name…", "Resolving domain…", "Searching contacts…") as `find()`
reaches them, useful for showing live progress in a UI. Skipped stages (both,
when `known_domain` is given) are simply never reported. Defaults to a
no-op — omit it and nothing changes.

### 5–6. Email and phone enrichment
`enrich_email()` / `enrich_phone()` take a list of `Contact` objects, filter
to the ones with a LinkedIn URL, and submit them to Clay's Work Email / Work
Phone Functions. Results are written back onto the same `Contact` objects
(`work_email`, `work_phone`, or the corresponding `*_error` field) — nothing
is returned separately to keep in sync.

---

## Data model

```python
@dataclass
class Contact:
    name: str
    title: str | None
    company_name: str | None
    domain: str | None
    linkedin_url: str | None
    city: str | None
    source: str  # "domain_search" (currently the only value produced)
    work_email: str | None = None
    work_email_error: str | None = None
    work_phone: str | None = None
    work_phone_error: str | None = None

    @property
    def email_eligible(self) -> bool: ...  # has both a linkedin_url and a domain

@dataclass
class DomainResolution:
    domain: str | None
    canonical_domain: str | None
    enriched_name: str | None
    source: str | None  # "agreement" | "serper_llm" | "clay_find_domain" | "manual_override" | None
    reason: str

@dataclass
class EnrichmentResult:
    input_name: str
    input_country: str
    resolved_name: str
    domain_resolution: DomainResolution | None
    name_confirmed: bool                          # bool(domain) -- no separate confirmation step
    contacts: list[Contact]

    @property
    def verified(self) -> bool: ...              # at least one contact found
```

---

## Multi-country support

This package isn't tied to any single country — `find(company_name, country)`
takes any country as free text. Three places are country-aware, each handled
differently on purpose:

1. **Search geo-bias (§2).** `country` is converted to an ISO 3166-1 alpha-2
   code (Google's `gl` search-bias parameter) via `pycountry`'s fuzzy name
   lookup, which resolves official names, common names, and existing
   alpha-2/alpha-3 codes alike ("France", "United States", "USA", "Czechia"
   all resolve correctly). A small hardcoded alias table patches a handful of
   very common colloquial names that `pycountry` itself doesn't catch
   (confirmed by testing, not guessed) — e.g. "UK", "Holland", "Russia". An
   unresolved country name degrades gracefully: the search still runs, just
   without geo-targeting, rather than failing.
2. **LLM prompts (§1, §2, §3).** None of the name-canonicalization,
   domain-resolution, or arbitration prompts hardcode any specific country —
   every example given to the model spans multiple countries/languages (e.g.
   "IBM, NASA, BASF, PKO BP, SNCF" for genuine abbreviations; "gov.pl, gov.uk,
   gouv.fr, bund.de" for shared government portals) specifically so the model
   generalizes the *pattern*, not one country's instance of it.
3. **Keyword title matching (§4).** The one place country genuinely changes
   *behavior*, not just bias: the domain search's keyword step matches job
   titles as literal text, so an English/French-only title list silently
   misses most real job titles in a country whose dominant business language
   is something else. Confirmed live: a verified, correct Polish company
   domain returned 0 contacts on `target_titles` alone, and 2 real,
   LinkedIn-eligible ones once localized terms were included. By default,
   `ClayContactFinder` asks the LLM to localize `target_titles` into
   generic, idiomatic local-language role words for whatever country is
   passed to `find()` — not a full translation of each title, just the kind
   of words ("manager", "director", "head of...") that actually show up
   inside real job titles there — the first time that country is seen, and
   caches the result for the life of the instance. Pass `fallback_extra_titles`
   explicitly to skip this and use the same fixed extra terms for every
   country instead. Note this only affects the keyword step — the
   broad+LLM-pick step (see stage 4 above) doesn't filter by title at all, so
   it isn't affected by localization quality one way or the other.

---

## Design rationale

This section exists because every non-obvious decision below was arrived at
by testing an alternative first and finding it worse, on a real 17-company
batch (Polish companies mentioned in industry news articles). If you're tempted to "simplify" one of these, the numbers here
are why it looks the way it does.

### Why two independent domain resolvers, not one

| Method | Verified | Contacts (email-eligible) | Emails found |
|---|---|---|---|
| Clay Find Domain alone | 5/17 | 15 | 6 |
| Search-grounded LLM alone | 7/17 | 17 | 9 |
| Search-grounded LLM + LLM-canonicalized name, plain match | 14/17 | 28 | 17 |
| **Two resolvers + arbitration (current)** | **14/17** | **36** | **24** |

Find Domain alone has no country-awareness and is prone to keyword-collision
false matches — it returned `airportsinternational.com` for "Centralny Port
Komunikacyjny" and `gdynia.pl` (the generic city hall domain) instead of the
port authority's actual `port.gdynia.pl`. The search-grounded resolver alone
was stronger, but not infallible. Running both and reconciling them — verified
live on the same hard cases before shipping — recovers cases neither gets
right alone: it picks the search-grounded resolver's correct answer over
Find Domain's wrong guess for CPK and Gdynia, while still accepting Find
Domain's answer on cases where it happens to be right and the two agree (e.g.
PERN, KGHM). If you don't have a Serper key, the pipeline still works — it
just falls back to Find Domain alone, with the accuracy hit shown above.

### Why the arbiter used to be able to reject both candidates, and no longer can

An earlier version told the arbiter it could answer "neither candidate is
right" rather than being forced to pick — motivated by a real case: three of
an early 17-company test batch are Polish government bodies sharing the
`gov.pl` national portal, and both resolvers either returned nothing or
returned `gov.pl` for all three, which isn't any specific agency's real
domain. Rejecting both and falling through to a second search mechanism
avoided trusting a wrong domain for those three.

That second mechanism (a name-based fallback search) has since been removed
entirely (see below), which removed the safety net "reject and fall through"
depended on — rejecting now would just mean returning nothing at all, with
no way to recover. Combined with repeated real cases where automated
rejection/non-confirmation cost correct results elsewhere in this pipeline
(the plain-name-check history right below is the clearest example), the
arbiter is now **always forced to pick one of the two candidates**, even a
shared-portal one if that's genuinely the only option. The mitigation isn't
automated — it's that the domain used is now always visible to whoever
reviews the result, with a direct way to correct it (`known_domain`).

### Why the plain name check was removed entirely

An earlier version required every resolved domain — even ones two
independent resolvers agreed on — to also pass a plain text match (later
just normalized substring/equality; an even earlier version used a `difflib`
similarity ratio plus a separate LLM semantic-equivalence call, removed for
being reactive complexity nobody asked for) between the target name and
Clay's own name for that domain, before trusting it. This was a repeated,
real source of false negatives on genuinely correct domains:

- **PKP**: Enrich Company returns `"PKP S.A."`; the canonicalized target was
  `"Polskie Koleje Państwowe S.A."` — neither contains the other.
- **Gdynia**: Enrich Company returns the English `"Port of Gdynia Authority
  S.A."`; the target was the Polish `"Zarząd Morskiego Portu Gdynia S.A."`.
- **Ocean Winds**: resolved to `oceanwinds.com` by two independent resolvers
  *agreeing* (25 real Poland employees exist there — about as strong a
  signal as this pipeline ever gets), but Enrich Company's brand-prefixed
  `"OW Ocean Winds"` didn't substring-match the canonicalized
  `"Ocean Winds Polska"` either.

An intermediate version tried threading a needle — trusting agreement
directly while still requiring the plain check for single-candidate/
arbitrated cases — which fixed Ocean Winds but not PKP/Gdynia-shaped cases
(a single correct candidate, or an arbiter's correct pick, still getting
vetoed by a superficial text mismatch). The check added a second automated
judgment on top of an already-evidence-backed domain (two resolvers, cross-
checked via Enrich Company) for marginal protection against a class of error
(hallucinated domains) the resolvers themselves are cross-checked against
already. It was removed entirely: a domain this pipeline settles on is now
trusted directly, and a human reviewing the result is the check that
actually catches the cases that matter.

### Why the name-search fallback was removed entirely

An earlier version had a whole second contact-search mechanism that ran
automatically whenever the domain-based search found nobody (or no domain
was ever found): a query-mode search matching people by *listed employer
text* instead of domain, confirmed against a second per-person filters-mode
lookup (query-mode's response schema has no LinkedIn URL field at all —
confirmed by dumping its raw response — so every candidate needed a second
lookup to become usable). It genuinely worked — it recovered real, verified,
email-eligible contacts at entities with no findable domain that the
domain-based path alone would have missed entirely.

It was removed anyway, by explicit request, for two reasons. First, cost:
discovery-then-per-candidate-confirmation added real latency (up to another
several sequential/parallel search+LLM round-trips) on exactly the cases
that were already the slowest, for a shrinking set of edge cases as the rest
of the pipeline got more reliable. Second, and more fundamentally: chasing
automated recall on the hard remaining cases (acronym mismatches, garbled
consortium names, shared portals) kept costing accuracy elsewhere, case by
case, patch by patch — the plain-name-check history above is the clearest
example of that pattern. The replacement isn't "try harder automatically" —
it's a human who already knows or can look up the right domain in seconds,
correcting it directly, every time, not just for the hard cases anyone
happened to test for.

### Why domain-confirmed searches broaden to an LLM pick instead of giving up

A resolved, verified domain finding zero contacts on the keyword search isn't
necessarily a sign there's nobody there — it can just mean the fixed title
list doesn't match how people at that specific company describe their job.
Confirmed live: a verified Polish company domain returned 0 contacts on
`target_titles` (even after localization) purely because the real job title
present was "Kierownik robót" ("works manager"), a specific compound term
neither the base list nor the localized generic terms happened to include.
Rather than keep chasing every possible title variant by hand, §4 broadens to
an unfiltered pull (up to 200 people, still country-filtered) and lets an LLM
pick the best 3-10 by judgment instead of literal keyword matching, once the
keyword step comes back empty. The LLM is deliberately
instructed to always pick real people rather than return nothing when
relevance looks weak — a facilities lead is a strong match, but even someone
in an unrelated function is a better starting point for outreach than no
contact at all, since they can redirect internally. This only runs on the
minority of cases where the keyword step already failed, so the added cost
(one extra search call + one LLM call) is paid rarely.

---

## Known limitations

- **A wrong domain can now come back as a wrong result, not just a missing
  one.** Since arbitration is always forced to pick and there's no plain-text
  confirmation afterward, a genuinely ambiguous or single-candidate-only case
  can settle on the wrong company's domain and return that wrong company's
  real, correctly-formatted contacts — rather than failing safely with
  `contacts=[]` the way an earlier, more conservative version would have.
  This is a deliberate trade-off (see [Design rationale](#design-rationale)),
  mitigated by always surfacing the domain used to a human and giving them
  `known_domain` to correct it directly — not eliminated by any automated
  check.
- **No automatic fallback if the domain-based search finds nobody.** An
  earlier version had a second, name-based search mechanism for exactly this
  case; it was removed (see Design rationale). `contacts=[]` is now a normal,
  expected outcome for entities with no findable domain or no Clay-indexed
  employees there, not just an edge case.
- **Clay's companies-search index is not perfectly stable run-to-run** for at
  least some domains — the same domain query returned a confirmed match with
  contacts on one run and zero candidates on a later, identical run during
  testing. Treat any single `find()` result as a snapshot, not a permanent fact.
- Some Clay-indexed company records carry unreliable `domain` fields (one
  companies-search hit for a real company's subsidiary carried a completely
  unrelated domain) — this is why the arbiter is only ever given `domain` +
  `enriched_name` as evidence, never trusted as the final answer on its own.
- Domain-based contact search occasionally hits a `400 Invalid company
  identifiers` error from Clay on specific domains for reasons that aren't
  fully understood (observed consistently on one Polish municipal domain
  across every resolution method tried) — handled gracefully (empty
  contacts, not raised), but there's no second mechanism to fall through to
  anymore.

---

## Batch usage

```python
companies = [
    ("Budimex", "Poland"),
    ("KGHM Polska Miedź S.A.", "Poland"),
    ("CPK", "Poland"),
]
results = finder.find_batch(companies)

all_contacts = [c for r in results for c in r.contacts]
finder.enrich_email(all_contacts)
finder.enrich_phone(all_contacts)

for r in results:
    print(f"{r.input_name}: {len(r.contacts)} contact(s), "
          f"{sum(1 for c in r.contacts if c.work_email)} with email")
```

`find_batch` runs sequentially — each `find()` call does up to ~4 sequential
network round-trips (LLM canonicalization, two domain resolvers in parallel,
Enrich Company ×2 in parallel, contact search — arbitration only adds a call
on the minority of cases where the two resolvers actually disagree, and is
skipped entirely on both the agreement and single-candidate paths). **The
34-50s/call figure measured on an earlier version of this pipeline (with a
plain-text confirmation step and a second fallback search mechanism, both
since removed) is now a ceiling, not a typical number** — expect it to be
faster on average, though not re-measured in bulk since the simplification;
plan batch runs as a background job regardless, not a request/response path.

Email and phone enrichment are markedly different in speed: on one real batch
of 8 email-eligible contacts, `enrich_email` took **23s total** (~3s/contact)
while `enrich_phone` took **184s total** (~23s/contact) — the phone routine is
roughly 8x slower per contact. Budget for that difference if you're enriching
both for a large batch; the default `max_polls`/`poll_interval` on
`enrich_phone` are already set higher than `enrich_email`'s to accommodate it,
but very large batches may still need an even larger `max_polls`.

See the module's logging output (`logging.getLogger("clay_contact_finder")`,
set to `INFO` or `DEBUG`) for per-call timing if you need to profile a
specific slow case.
