# syntax=docker/dockerfile:1.7
# Pin by digest in CI (renovate/dependabot) for reproducible, auditable builds.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy AS base

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY rules ./rules
COPY rpa ./rpa

# Non-root runtime user; staging directory is the only writable path.
RUN useradd --system --uid 10001 --home /nonexistent --shell /usr/sbin/nologin stsds \
 && mkdir -p /var/lib/stsds/staging && chown -R stsds:stsds /var/lib/stsds
USER 10001

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"

# One worker only: per-transaction locks and the ledger hash chain live in this process.
CMD ["uvicorn", "stsds.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header"]
