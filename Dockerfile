# syntax=docker/dockerfile:1.10

# Both image references are immutable multi-platform manifest digests. Update
# them deliberately together with the documented reproducibility policy.
FROM python:3.12-slim-bookworm@sha256:a116514e19457bcb7af7efe9c3dd0b9b71e85b317694e7882a1c52aa15a78134 AS base

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

FROM ghcr.io/astral-sh/uv:0.11.16@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d AS uv

FROM base AS builder

COPY --from=uv /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock .python-version README.md LICENSE ./

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

USER app

# A container started without arguments is a harmless CLI help invocation.
ENTRYPOINT ["osm-polygon-website-tag"]
CMD ["--help"]
