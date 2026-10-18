# One image, both platforms.
#
# Render and Cloud Run agree on the only two things this app needs from its
# host: a $PORT to bind and a writable filesystem. app.py already treats the
# presence of PORT as "you are hosted" (binds 0.0.0.0, serves via waitress,
# hides the operator-only UI), so the same image runs unmodified on Render
# today and Cloud Run when a search storm outgrows it — no second build, no
# "works on staging" drift between the two. See docs/mobile-platform-plan.md §4.
#
# Constraint that outlives the platform: ONE process, ONE container, until the
# state refactor in §4.2 lands. The stats fact store and every in-flight search
# live in this process's memory, so a second replica would answer with a
# different market and lose half the polls. Cloud Run enforces that with
# max-instances=1 (deploy/cloudrun/service.yaml); do not raise it before the
# externalization work is done.

FROM python:3.13-slim AS base

# curl: not a convenience — fetcher.py shells out to the curl BINARY because
#   guns.com (Akamai) fingerprints Python's TLS stack and 403s `requests`.
#   Without it those clients silently fall back to the blocked path.
# ca-certificates: TLS trust for every outbound scrape.
# tini: PID 1 that reaps zombies and forwards SIGTERM, so Cloud Run's 10s
#   shutdown grace actually reaches waitress instead of being swallowed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first: this layer is cached across every code-only deploy.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Non-root. The stats DB is the only thing written at runtime, and it lives on
# a mounted volume (Render disk / GCS-backed path), so the image itself stays
# read-only in practice.
RUN useradd --create-home --uid 10001 scout \
    && mkdir -p /data && chown scout:scout /data
USER scout

# Default DB location. Both platforms should mount durable storage here —
# without it the fact store starts empty after every deploy.
ENV GUN_SCOUT_DB=/data/gun_scout.db

# Documentation only; the platform's injected $PORT is what actually binds.
EXPOSE 8777

# Cloud Run's own startup probe (deploy/cloudrun/service.yaml) is the real
# gate. This one keeps `docker run` and Render honest.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8777}/api/v1/healthz" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "app.py"]
