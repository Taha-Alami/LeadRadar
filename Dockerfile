# syntax=docker/dockerfile:1.7
# LeadRadar container image.
#
# The default command runs the web app (FastAPI/uvicorn). The daily pipeline
# runs from the same image as a scheduled job, e.g.:
#   docker run --rm --env-file .env leadradar python main.py

# ─── Stage 1: install third-party dependencies with uv ───────────────────────
FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /uvx /usr/local/bin/

WORKDIR /build

# Dependencies are cached independently from source. The local contact-finder
# package is installed in the runtime stage instead.
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --no-emit-package clay-contact-finder \
        --format requirements-txt > /tmp/requirements.txt \
 && uv pip install --system --no-cache -r /tmp/requirements.txt

# ─── Stage 2: runtime ────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# - curl / ca-certificates: healthcheck + TLS.
# - msodbcsql18 + unixodbc (Microsoft's apt repo): pyodbc's SQL Server driver.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -sSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
 && curl -sSL https://packages.microsoft.com/config/debian/12/prod.list -o /etc/apt/sources.list.d/mssql-release.list \
 && apt-get update \
 && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 unixodbc \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 leadradar

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Headless Chromium (+ its OS libraries) for the scraper's JS-rendered fallback.
# Installed to a shared path so the non-root runtime user can find it.
RUN python -m playwright install --with-deps chromium \
 && chmod -R a+rX /opt/ms-playwright

WORKDIR /app
COPY clay_contact_finder/ ./clay_contact_finder/
RUN pip install --no-cache-dir ./clay_contact_finder/

COPY core/     ./core/
COPY pipeline/ ./pipeline/
COPY webapp/   ./webapp/
COPY profiles/ ./profiles/
COPY main.py   ./

RUN chown -R leadradar:leadradar /app
USER leadradar

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl --silent --fail http://127.0.0.1:8000/healthz || exit 1

CMD ["uvicorn", "webapp.app:app", "--host", "0.0.0.0", "--port", "8000"]
