FROM node:24.18.0-bookworm-slim AS web
WORKDIR /build/web
COPY services/app/web/package.json services/app/web/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY services/app/web/ ./
RUN npm run build

FROM python:3.12.11-slim-bookworm AS runtime
ARG UID=1000
ARG GID=1000
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# Add PostgreSQL repository and install client-16 alongside ffmpeg
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /etc/apt/trusted.gpg.d/postgresql.gpg \
 && echo "deb http://apt.postgresql.org/pub/repos/apt/ bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg postgresql-client-16 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY services/app/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir setuptools==80.9.0 wheel==0.45.1 \
 && pip install --no-cache-dir -r /tmp/requirements.txt
COPY packages/pipeline-core /opt/pipeline-core
RUN pip install --no-cache-dir --no-build-isolation /opt/pipeline-core
COPY services/app/run.py ./run.py
COPY services/app/server/ ./server/
COPY --from=web /build/static/ ./static/
COPY services/app/entrypoint.py services/app/worker.py services/app/backup.py services/app/restore.py /opt/service/
COPY migrations/ /opt/migrations/
COPY scripts/ /opt/scripts/
RUN groupadd --gid ${GID} pipeline \
 && useradd --uid ${UID} --gid ${GID} --no-create-home --home-dir /config --shell /usr/sbin/nologin pipeline \
 && mkdir -p /config /workspace /tmp/pipeline \
 && chown -R ${UID}:${GID} /config /tmp/pipeline
ENV HOME=/config XDG_CONFIG_HOME=/config MST_FFMPEG=/usr/bin/ffmpeg MST_FFPROBE=/usr/bin/ffprobe
USER pipeline
EXPOSE 8000
ENTRYPOINT ["python", "/opt/service/entrypoint.py"]
CMD ["app"]