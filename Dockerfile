# Stdio MCP server image (used by the Glama directory and for local container runs).
FROM python:3.13-slim

# Runtime libraries for WeasyPrint (pango, harfbuzz) plus fontconfig and a default font.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fontconfig fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.30 /uv /usr/local/bin/uv

RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin ucetka
ENV HOME=/home/ucetka \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable \
    && chown -R ucetka:ucetka /app

USER ucetka
WORKDIR /home/ucetka

# Speaks MCP over stdio; no credentials or environment variables required.
ENTRYPOINT ["ucetka-mcp"]
