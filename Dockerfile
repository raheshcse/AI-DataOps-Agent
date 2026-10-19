# syntax=docker/dockerfile:1
# AI DataOps Agent - container image (Phase 9). SINGLE-HOST: one container, one replica, one volume.
# The SQLite operational database, the SQLite approval store and the JSONL governance ledger are local files and
# are NOT safe to share between replicas (see docs/phase9-end-to-end-validation-cloud-infrastructure.md).
#
#   docker build -t ai-dataops-agent:local .
#   (optional, accepts Microsoft's licence terms - only if you need SQL Server from inside the container)
#   docker build --build-arg INSTALL_MSSQL_DRIVER=true --build-arg ACCEPT_MSODBC_EULA=Y -t ai-dataops-agent:local .

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------- build stage: resolve and install dependencies
FROM python:${PYTHON_VERSION}-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# git is needed ONLY here: the VSL adapter is installed from a pinned git commit (see requirements.txt).
RUN apt-get update \
 && apt-get install -y --no-install-recommends git build-essential unixodbc-dev \
 && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt && pip check

# ---------------------------------------------------------------- runtime stage
FROM python:${PYTHON_VERSION}-slim AS runtime
ARG INSTALL_MSSQL_DRIVER=false
ARG ACCEPT_MSODBC_EULA=N
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/opt/venv/bin:$PATH"

# unixODBC is the generic driver manager pyodbc loads. The Microsoft driver is opt-in (licence acceptance is yours).
RUN apt-get update \
 && apt-get install -y --no-install-recommends unixodbc ca-certificates \
 && if [ "$INSTALL_MSSQL_DRIVER" = "true" ]; then \
      if [ "$ACCEPT_MSODBC_EULA" != "Y" ]; then echo "Set ACCEPT_MSODBC_EULA=Y to install msodbcsql18" >&2; exit 1; fi; \
      apt-get install -y --no-install-recommends curl gnupg \
      && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
      && echo "deb [signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list \
      && apt-get update && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
      && apt-get purge -y curl gnupg && apt-get autoremove -y; \
    fi \
 && rm -rf /var/lib/apt/lists/*

# Non-root user. /data is the ONLY writable application location (mount a volume there).
RUN groupadd --system --gid 10001 dataops \
 && useradd --system --uid 10001 --gid dataops --home-dir /nonexistent --shell /usr/sbin/nologin dataops \
 && mkdir -p /data/logs && chown -R dataops:dataops /data

COPY --from=build /opt/venv /opt/venv
WORKDIR /srv/app
COPY app ./app
COPY scripts ./scripts

# Non-secret defaults only. Secrets (APPROVAL_SIGNING_KEY, GITHUB_TOKEN, OPENAI_API_KEY, ...) are NEVER baked into the
# image: pass them at run time. DB_NAME is deliberately NOT defaulted: production start-up refuses to run without it.
ENV APP_ENV=production \
    DATABASE_URL=sqlite:////data/operations.sqlite3 \
    APPROVAL_STORE_PATH=/data/approvals.sqlite3 \
    GOVERNANCE_LEDGER_PATH=/data/governance_ledger.jsonl \
    SIM_LOG_DIR=/data/logs \
    API_HOST=0.0.0.0 \
    API_PORT=8000

VOLUME ["/data"]
USER 10001:10001
EXPOSE 8000

# Liveness only (touches no dependency). Readiness (/api/v1/ready) is for the orchestrator or operator.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status == 200 else 1)"]

# Exec form: uvicorn receives SIGTERM and shuts down gracefully. ONE worker only (SQLite and the ledger are single-writer).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-server-header"]
