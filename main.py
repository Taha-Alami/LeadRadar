"""
LeadRadar — command-line entry point.

Loads configuration from .env, initialises the LLM client, and runs the lead
pipeline for one or more countries. If a country fails, an error alert is
emailed and the run continues with the next country; the process exits with
code 1 if any country failed (so a scheduler can flag the run).

Usage::

    python main.py                 # ENABLED_COUNTRIES from .env, else every configured country
    python main.py SPAIN           # one country
    python main.py SPAIN ITALY     # several

Schedule it daily with cron / Windows Task Scheduler, or use the Prefect flow
in ``flows/`` for retries and a run-history UI.
"""
import logging
import os
import sys
import traceback

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S",
)

from core.config import AppConfig
from core.llm_client import LLMClient
from core.mailer import send_error_alert
from pipeline.country_config import configured_countries
from pipeline.lead_pipeline import run_country_pipeline


def _countries_for(args: list[str]) -> list[str]:
    """
    Resolve CLI arguments to the list of countries to run.

    No arguments (or ``ALL``) → the ``ENABLED_COUNTRIES`` env var when set,
    otherwise every configured country. Keeps a configured-but-not-yet-launched
    country from emailing its recipients. Must be called after
    ``AppConfig.from_env()`` so ``.env`` is loaded.
    """
    requested = [a.upper() for a in args]
    if requested and requested != ["ALL"]:
        return requested
    enabled = [c.strip().upper() for c in os.getenv("ENABLED_COUNTRIES", "").split(",") if c.strip()]
    return enabled or list(configured_countries())


def main() -> None:
    cfg = AppConfig.from_env()
    llm = LLMClient(cfg)

    countries = _countries_for(sys.argv[1:])
    logging.info("LeadRadar countries: %s", ", ".join(countries))

    failures = []
    for country_code in countries:
        try:
            run_country_pipeline(country_code, cfg, llm)
        except Exception as exc:
            logging.exception("LeadRadar failed for %s: %s", country_code, exc)
            send_error_alert(cfg, f"{country_code} LeadRadar", str(exc), traceback.format_exc())
            failures.append(country_code)

    if failures:
        logging.error("LeadRadar finished with failures: %s", ", ".join(failures))
        sys.exit(1)


if __name__ == "__main__":
    main()
