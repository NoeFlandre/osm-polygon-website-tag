# syntax=docker/dockerfile:1.10

# Both image references are immutable multi-platform manifest digests. Update
# them deliberately together with the documented reproducibility policy.
FROM python:3.14-slim-bookworm@sha256:c8137f4c460908c8763f281c8f22c431eb5c538514ba9553fc3a89c06b7cfb88 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    XDG_CACHE_HOME=/tmp/.cache \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:/usr/local/bin:${PATH}"

WORKDIR /app

RUN apt-get update \
    && apt-get install --no-install-recommends -y libexpat1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

FROM ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 AS uv

FROM base AS builder

COPY --from=uv /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock .python-version README.md LICENSE ./

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install third-party runtime dependencies before copying source for a stable
# cache. The locked project is installed in the next layer.
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM builder AS dev

COPY . .
RUN uv sync --locked --group dev \
    && chown -R app:app /app

USER app

# The dev target is for local/CI checks; runtime is the smaller production image.
CMD ["uv", "run", "--locked", "pytest"]

FROM base AS runtime

COPY --from=builder --chown=app:app /opt/venv /opt/venv

# The data root every command defaults to: /data/raw holds the read-only PBFs,
# /data/runs the run output, /data/models the downloaded model caches, and
# /data/grid5000 the offline bundles. Mount volumes over them; all must be
# writable by the container user.
ENV OSM_POLY_DATA_DIR=/data
RUN mkdir -p /data/raw /data/runs /data/models /data/grid5000 \
    && chown app:app /data/runs /data/models /data/grid5000

USER app

# A container started without arguments is a harmless CLI help invocation.
ENTRYPOINT ["osm-polygon-website-tag"]
CMD ["--help"]
