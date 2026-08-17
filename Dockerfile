# syntax=docker/dockerfile:1

# ---- web: build the typed SPA (ADR-0009) into the package's static dir ----
# Node lives ONLY in this throwaway stage; the runtime image stays Node-free, so
# bit-identical :edge -> :stable promotion (ADR-0004) is preserved. The committed
# generated client (frontend/src/api/schema.d.ts) is used as-is — no openapi.json
# needed here (docs/ is .dockerignore'd anyway).
FROM node:26-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# vite's outDir is ../src/plex_manager/web/static (relative to /web -> /src/...).
RUN npm run build

# ---- builder: install the app into an isolated venv ----
FROM cgr.dev/chainguard/wolfi-base:latest@sha256:0a8fd427de5882aed77471b0a432c3675eda6b6a0ae952b5d640b46da628cdbe AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

RUN apk add --no-cache \
        python-3.14=3.14.6-r4 \
        py3.14-pip=26.1.2-r1 \
    && python3.14 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Patch the venv's bundled pip before it installs anything
# (CVE-2026-1703: path traversal when extracting a crafted wheel).
RUN pip install --upgrade pip==26.1.2

# Copy only what the build backend needs, then install against the committed
# runtime constraints used by CI/audit.
COPY pyproject.toml README.md ./
COPY requirements ./requirements
COPY src ./src
# Drop the built SPA in BEFORE the install so hatchling packages it into the
# wheel (via [tool.hatch.build.targets.wheel].artifacts) and it ships in the image.
COPY --from=web /src/plex_manager/web/static ./src/plex_manager/web/static
RUN pip install -c requirements/runtime-constraints.txt ".[postgres]"

# ---- runtime: Wolfi/glibc image with just the venv + migration assets ----
FROM cgr.dev/chainguard/wolfi-base:latest@sha256:0a8fd427de5882aed77471b0a432c3675eda6b6a0ae952b5d640b46da628cdbe AS runtime
ARG PLEX_MANAGER_BUILD_ID=0.0.0
# The app's config default is loopback (safe for bare-metal first runs); inside
# the container the ONLY way in is the published port, so bind all interfaces
# here or `docker run -p` would map to a dead socket (the healthcheck, probing
# 127.0.0.1 from INSIDE, would still pass -- an unreachable-but-green trap).
# Overridable per-deployment via the environment like any other setting.
#
# PLEX_MANAGER_IN_CONTAINER is the image-baked "this process runs inside the
# shipped container image" sentinel (Settings.in_container). The startup
# setup-URL hint requires it before trusting the compose-published
# PLEX_MANAGER_HOST_BIND/HOST_PORT values -- mount-point names alone are not
# evidence of being inside this container (a bare-metal media server can have
# real disks mounted at both /media and /downloads). Baked here, deliberately
# NOT listed in .env.example, so a bare-metal install can never copy it by
# accident.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PLEX_MANAGER_BUILD_ID=${PLEX_MANAGER_BUILD_ID} \
    PLEX_MANAGER_HOST=0.0.0.0 \
    PLEX_MANAGER_IN_CONTAINER=1
WORKDIR /app

# Validate downloaded candidates by their actual media container/streams before
# they can enter a Plex library. The versioned ffmpeg package supplies ffprobe.
RUN apk add --no-cache \
        python-3.14=3.14.6-r4 \
        ffmpeg-8.1=8.1.2-r2 \
        tzdata=2026c-r0 \
    && mkdir -p /app/data \
    && chown 10001:10001 /app/data

COPY --from=builder /opt/venv /opt/venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request,sys; p=os.environ.get('PLEX_MANAGER_PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=4).status==200 else 1)"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
