# syntax=docker/dockerfile:1

# ---- builder: install the app into an isolated venv ----
FROM python:3.14-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Patch the venv's bundled pip before it installs anything
# (CVE-2026-1703: path traversal when extracting a crafted wheel).
RUN pip install --upgrade pip

# Copy only what the build backend needs, then install.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# ---- runtime: slim image with just the venv + migration assets ----
FROM python:3.14-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /app

# Patch the base image's own system pip too (same CVE-2026-1703). The app runs
# from the copied venv and never invokes this pip, but Trivy scans the whole
# filesystem and we surface every finding, so we fix the fixable ones outright.
# This runs before the venv is copied, so `python` is the base interpreter here.
RUN python -m pip install --no-cache-dir --upgrade pip

# Non-root user; pre-create the data dir for the mounted volume.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app

COPY --from=builder /opt/venv /opt/venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
