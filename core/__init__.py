"""
core — shared platform layer for LeadRadar.

Infrastructure only, no lead-finding logic: talking to LLMs, reading RSS feeds,
sending email, holding configuration, and describing the business the leads
are for. Lead-finding logic lives in ``pipeline/``.

Modules:
    config           Runtime configuration, loaded once from .env at startup.
    business_profile Loads the TOML profile every LLM prompt is built from.
    news_models      Shared data classes: NewsItem and EnrichedOpportunity.
    llm_client       LLMClient — thin, injectable wrapper over Azure OpenAI / Mistral.
    feed_reader      fetch_rss_feed() — generic RSS / Atom parser.
    mailer           send_email() / send_error_alert() — file, SMTP, or Graph backends.
    sql_connector    SQLConnector — pyodbc / SQLAlchemy access to SQL Server / Azure SQL.
"""
