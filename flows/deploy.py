"""
Register the LeadRadar deployment with a Prefect server.

Run once (or after any schedule / pool change):
    uv run python flows/deploy.py

Requirements:
    - Prefect server running : uv run prefect server start
    - Work pool exists       : uv run prefect work-pool create leadradar-pool --type process
    - Worker running         : uv run prefect worker start --pool leadradar-pool

Schedule and timezone are read from LEADRADAR_CRON / LEADRADAR_TZ
(default: weekdays at 07:00 UTC).
"""
import os
import sys
from pathlib import Path

root = Path(__file__).parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from prefect.client.schemas.schedules import CronSchedule
from prefect.runner.storage import LocalStorage

from flows.leadradar_flow import run_leadradar

run_leadradar.from_source(
    source     = LocalStorage(path=str(root)),
    entrypoint = "flows/leadradar_flow.py:run_leadradar",
).deploy(
    name           = "leadradar-daily",
    work_pool_name = "leadradar-pool",
    parameters     = {"country_code": "ALL"},
    schedules      = [CronSchedule(
        cron     = os.getenv("LEADRADAR_CRON", "0 7 * * 1-5"),
        timezone = os.getenv("LEADRADAR_TZ", "UTC"),
    )],
    tags           = ["leadradar", "lead-generation"],
)
