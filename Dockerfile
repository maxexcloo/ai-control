# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13 AS uv
FROM docker.io/library/node:26.12.1-trixie AS ui
WORKDIR /ui
COPY control/web/package*.json ./
RUN npm ci --no-audit --no-fund
COPY control/web/ ./
RUN npm run build

FROM docker.io/library/python:3.15.1-slim-trixie
WORKDIR /app
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install --yes --no-install-recommends pci.ids \
    && rm -rf /var/lib/apt/lists/*
ENV PATH=/app/.venv/bin:$PATH
RUN --mount=from=uv,source=/uv,target=/usr/local/bin/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-cache
COPY control/*.py ./control/
COPY LICENSE ./LICENSE
COPY --from=ui /ui/dist/ ./control/web/dist/
EXPOSE 1337
CMD ["python", "-m", "control.server"]
