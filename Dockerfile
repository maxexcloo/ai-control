# syntax=docker/dockerfile:1
FROM docker.io/library/node:26.12.0-trixie AS ui
WORKDIR /ui
COPY control/web/package*.json ./
RUN npm ci --no-audit --no-fund
COPY control/web/ ./
RUN npm run build

FROM docker.io/library/python:3.15.0-slim-trixie
WORKDIR /app
COPY control/requirements.txt ./
RUN pip install --no-cache-dir --require-hashes --requirement requirements.txt
COPY control/*.py ./control/
COPY LICENSE ./LICENSE
COPY --from=ui /ui/dist/ ./control/web/dist/
EXPOSE 1337
CMD ["python", "-m", "control.server"]
