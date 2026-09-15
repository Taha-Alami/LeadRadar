-- ============================================================================
-- LeadRadar — database schema (SQL Server 2019+ / Azure SQL)
--
-- Creates the `core` schema and the four tables used by the pipeline and the
-- web app. Idempotent: every object is created only if it doesn't exist yet,
-- and nothing is ever dropped.
--
--   core.news_articles        every collected article, with its triage tags
--   core.news_enriched        AI-enriched leads (daily picks + on-demand enrichment)
--   core.opportunity_notes    per-lead-group discussion thread + audit events
--   core.opportunity_contacts decision-maker contacts found for a lead
--
-- Tag columns (sector / priority / region) are validated in Python rather
-- than with CHECK constraints: the sector vocabulary comes from the active
-- business profile, and the region vocabulary from pipeline/country_config.py.
-- ============================================================================

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'core')
    EXEC('CREATE SCHEMA core');
GO

-- ── Raw articles ────────────────────────────────────────────────────────────
IF OBJECT_ID('core.news_articles', 'U') IS NULL
CREATE TABLE core.news_articles (
    id               BIGINT IDENTITY(1,1) NOT NULL,
    country          VARCHAR(20)     NOT NULL,   -- e.g. SPAIN (pipeline/country_config.py)
    title            NVARCHAR(MAX)   NOT NULL,
    source_outlet    NVARCHAR(MAX)   NULL,
    url              NVARCHAR(MAX)   NULL,        -- feed URL (may be a Google News redirect)
    llm_provider     VARCHAR(10)     NULL,        -- azure | mistral
    llm_model        NVARCHAR(MAX)   NULL,
    publication_date DATE            NULL,
    created_at       DATETIMEOFFSET  NOT NULL DEFAULT SYSDATETIMEOFFSET(),

    -- Triage tags (pipeline/opportunity_triage.py)
    sector           NVARCHAR(50)    NULL,
    priority         VARCHAR(20)     NULL,        -- High | Medium | Low | Unknown
    region           NVARCHAR(50)    NULL,        -- ASCII region name, e.g. Cataluna

    CONSTRAINT PK_news_articles PRIMARY KEY (id)
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_news_articles_country_created')
    CREATE INDEX IX_news_articles_country_created ON core.news_articles (country, created_at);
GO

-- ── Enriched leads ──────────────────────────────────────────────────────────
-- source_news_id is a *soft* reference to core.news_articles.id (no FK), so an
-- archived raw row never blocks an enriched insert. parent_id groups several
-- articles about the same project under one representative (one level deep).
IF OBJECT_ID('core.news_enriched', 'U') IS NULL
CREATE TABLE core.news_enriched (
    id                  INT IDENTITY(1,1) NOT NULL,
    source_news_id      BIGINT          NULL,
    enriched_at         DATETIMEOFFSET  NOT NULL DEFAULT SYSDATETIMEOFFSET(),
    country             VARCHAR(100)    NOT NULL,
    title               NVARCHAR(MAX)   NOT NULL,
    url                 NVARCHAR(MAX)   NOT NULL,  -- resolved publisher URL
    source_outlet       NVARCHAR(MAX)   NULL,
    publication_date    DATE            NULL,
    phase               VARCHAR(30)     NULL,      -- Announced|Planning|Permitting|Tender|Awarded|Construction|Completed|Unknown
    company             NVARCHAR(500)   NULL,
    city                NVARCHAR(500)   NULL,
    project_type        NVARCHAR(500)   NULL,
    project_value       NVARCHAR(500)   NULL,
    product_fit         NVARCHAR(MAX)   NULL,      -- "why they need us" (label defined by the business profile)
    why_it_matters      NVARCHAR(MAX)   NULL,
    recommended_action  NVARCHAR(MAX)   NULL,
    scrape_method       VARCHAR(20)     NULL,      -- trafilatura | newspaper4k | *_js | failed
    scraped_text        NVARCHAR(MAX)   NULL,      -- full article, used by the chat assistant
    end_usage           NVARCHAR(100)   NULL,
    english_summary     NVARCHAR(MAX)   NULL,
    llm_provider        VARCHAR(20)     NULL,
    llm_model           NVARCHAR(500)   NULL,
    created_at          DATETIMEOFFSET  NOT NULL DEFAULT SYSDATETIMEOFFSET(),
    parent_id           INT             NULL,

    -- Final triage tags after the picker + enricher re-checks
    sector              NVARCHAR(50)    NULL,
    priority            VARCHAR(20)     NULL,
    region              NVARCHAR(50)    NULL,

    -- Assignment (set in the web app on the group representative)
    assigned_to_email   NVARCHAR(200)   NULL,
    assigned_to_name    NVARCHAR(200)   NULL,
    assigned_by         NVARCHAR(200)   NULL,
    assigned_at         DATETIMEOFFSET  NULL,

    -- Qualification decision (set in the web app on the group representative)
    qualification       VARCHAR(20)     NULL,
    qualified_by        NVARCHAR(200)   NULL,
    qualified_at        DATETIMEOFFSET  NULL,

    CONSTRAINT PK_news_enriched PRIMARY KEY (id),
    CONSTRAINT FK_news_enriched_parent FOREIGN KEY (parent_id) REFERENCES core.news_enriched (id),
    CONSTRAINT CK_news_enriched_qualification CHECK (
        qualification IS NULL OR qualification IN ('Qualified', 'Disqualified')
    )
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_news_enriched_source_news_id')
    CREATE INDEX IX_news_enriched_source_news_id ON core.news_enriched (source_news_id);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_news_enriched_parent_id')
    CREATE INDEX IX_news_enriched_parent_id ON core.news_enriched (parent_id);
GO

-- ── Notes / activity ────────────────────────────────────────────────────────
IF OBJECT_ID('core.opportunity_notes', 'U') IS NULL
CREATE TABLE core.opportunity_notes (
    id               INT IDENTITY(1,1)  NOT NULL,
    group_id         INT                NOT NULL,  -- core.news_enriched.id of the group representative
    country          VARCHAR(50)        NULL,
    author_name      NVARCHAR(200)      NULL,
    comment_text     NVARCHAR(MAX)      NOT NULL,
    note_type        VARCHAR(20)        NOT NULL DEFAULT 'comment',
    -- 'comment'  user note (editable / deletable by its author)
    -- 'event'    system-generated on link/unlink/assign/qualify — immutable
    -- 'history'  frozen copy of a comment from a previously-linked group
    source_group_id  INT                NULL,      -- for 'history' rows: which group it was copied from
    created_at       DATETIMEOFFSET     NOT NULL DEFAULT SYSDATETIMEOFFSET(),
    updated_at       DATETIMEOFFSET     NULL,

    CONSTRAINT PK_opportunity_notes PRIMARY KEY (id),
    CONSTRAINT CK_opportunity_notes_type CHECK (note_type IN ('comment', 'event', 'history'))
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_opportunity_notes_group_id')
    CREATE INDEX IX_opportunity_notes_group_id ON core.opportunity_notes (group_id);
GO

-- ── Contacts found by the contact finder ────────────────────────────────────
-- enrichment_id points at the SPECIFIC article that was searched, not the
-- group root: group roots can change when groups are merged, which would
-- orphan rows keyed to "the current root". The web app reads contacts across
-- every current group member at display time instead.
--
-- Each search deactivates the previous active rows for that enrichment_id and
-- inserts its outcome as active (old rows stay as an audit trail). A search
-- that found nobody still stores one row with full_name NULL, holding just the
-- domain that was searched, so the domain stays visible for human correction.
IF OBJECT_ID('core.opportunity_contacts', 'U') IS NULL
CREATE TABLE core.opportunity_contacts (
    id                 INT IDENTITY(1,1)  NOT NULL,
    enrichment_id      INT                NOT NULL,
    country            VARCHAR(50)        NULL,

    full_name          NVARCHAR(200)      NULL,
    job_title          NVARCHAR(300)      NULL,
    company_name       NVARCHAR(300)      NULL,
    company_domain     NVARCHAR(255)      NULL,
    linkedin_url       NVARCHAR(500)      NULL,
    city               NVARCHAR(200)      NULL,
    source             VARCHAR(30)        NULL,      -- 'domain_search'

    work_email         NVARCHAR(255)      NULL,
    work_phone         NVARCHAR(50)       NULL,
    email_enriched_at  DATETIMEOFFSET     NULL,      -- NULL = never attempted
    phone_enriched_at  DATETIMEOFFSET     NULL,

    found_by           NVARCHAR(200)      NULL,
    is_active          BIT                NOT NULL DEFAULT 1,
    created_at         DATETIMEOFFSET     NOT NULL DEFAULT SYSDATETIMEOFFSET(),

    CONSTRAINT PK_opportunity_contacts PRIMARY KEY (id),
    CONSTRAINT CK_opportunity_contacts_source CHECK (
        source IS NULL OR source IN ('domain_search', 'name_search_fallback')
    )
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_opportunity_contacts_enrichment_id')
    CREATE INDEX IX_opportunity_contacts_enrichment_id ON core.opportunity_contacts (enrichment_id);
GO
