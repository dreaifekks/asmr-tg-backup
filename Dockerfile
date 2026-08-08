FROM python:3.12-slim-bookworm AS runtime-base

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/app \
    PUID=${APP_UID} \
    PGID=${APP_GID}

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl ffmpeg gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home app

WORKDIR /app

RUN mkdir -p /config /data \
    && chown -R app:app /config /data

COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/asmr-tg-backup-entrypoint

ENTRYPOINT ["/usr/local/bin/asmr-tg-backup-entrypoint"]
CMD ["asmr-tg-backup", "run", "--config", "/config/config.toml"]

FROM runtime-base AS source-install

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install --no-cache-dir ".[performance]"

FROM source-install AS source-runtime

FROM runtime-base AS wheel-install

# Official images build this target after downloading the exact wheel produced
# by the PyPI release job. Local Compose builds use source-runtime instead.
COPY dist/*.whl /tmp/dist/

RUN python -m pip install --no-cache-dir "cryptg>=0.5,<1" /tmp/dist/*.whl \
    && rm -rf /tmp/dist

FROM wheel-install AS wheel-runtime

# Keep a plain `docker build .` useful for source checkouts. The release
# workflow explicitly selects wheel-runtime.
FROM source-runtime AS runtime
