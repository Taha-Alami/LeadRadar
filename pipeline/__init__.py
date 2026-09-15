"""
pipeline — country-by-country B2B lead discovery from the news.

For one country per run, the pipeline collects fresh project news, triages
every article with an LLM (sector / priority / region / in-country), picks the
3-5 best still-actionable opportunities, scrapes the full article for the
details RSS summaries omit (phase, company, city, value), and emails a short
card-based digest. Everything is persisted for the web app.

Entry point: ``pipeline.lead_pipeline.run_country_pipeline()``.
"""
