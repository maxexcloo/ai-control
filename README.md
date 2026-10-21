# AI Control

AI Control is a web UI and authenticated API for manually allocating two GPUs. It previews changes, checks the current allocation before applying them and manages workload containers through a mounted Docker-compatible engine socket. Deployment configuration supplies the workload catalogue and fully resolved container specifications.

Docker and Podman sockets use the same REST interface. AI Control does not invoke SSH, package a container CLI or manage models, downloads or generation profiles.

## External Deployment Configuration

Host tools live in `deployment/`, separately from the application image. They read
private configuration from an external directory containing `compose.yaml`,
`workloads.json` and `.env`. Docker deployments use `docker compose`; Podman
deployments additionally supply `compose.podman.yaml` and use `podman-compose`.

Run from this checkout:

```sh
uv run --locked --project deployment python -m deployment --root /srv/ai prepare
uv run --locked --project deployment python -m deployment --root /srv/ai pull
uv run --locked --project deployment python -m deployment --root /srv/ai deploy
uv run --locked --project deployment python -m deployment --root /srv/ai boot
uv run --locked --project deployment python -m deployment --root /srv/ai gpu status
```

`prepare` creates missing runtime directories and copies the GPU lease helpers,
preserving existing lease files. `deploy` verifies installed worker images and
Compose, retains restored owner accounts, resolves workload placements and
recreates Control without changing GPU allocation. `boot` restores the saved
allocation; `AI_BOOT_ALLOCATION` supplies the initial allocation when no saved
allocation exists. `check` and `configure` expose deployment steps separately.
Worker images may use an exact release or a full `sha-` plus 40 lowercase hex
CI build tag; Studio itself must use an exact release. Both worker pins retain
the SDK file fingerprint comparison and packaged ComfyUI core version check.

The private `.env` supplies the explicitly selected `AI_CONTAINER_ENGINE`, private
host addresses, absolute `AI_DATA_DIR`, `AI_MODELS_DIR` and
`CONTROL_ENGINE_SOCKET` paths, `CONTROL_PORT` and service credentials. Explicit
process environment values take precedence. These tools read literal dotenv
values without shell expansion. Preserve the existing state paths when adopting
them: Control accounts must already exist at `control/data/control.json` beneath
`AI_DATA_DIR`. Credentials are consumed locally and must never be printed.

The workload catalogue owns container identities, allowed GPU placements, service
links and placement-specific `environment` settings. Native HTTPS aliases come
from the optional `AI_PROXY_ROUTES` absolute path to the host's Traefik routes.
Without it, links use the configured private host and service ports.
Runtime checks require the actual deployment host;
they do not establish successful GPU generation or account isolation.

## Run

Install the [shared Mise tools and tasks](https://github.com/maxexcloo/dotfiles#shared-tools--tasks), then run `mise trust`, `mise run setup` and `mise run build`.

Provide an existing access configuration through `CONTROL_CONFIG`, then set `CONTROL_API_KEY` and `CONTROL_SESSION_KEY` using the deployment's credential tooling. Run `mise run serve`. The app listens on port 1337; expose it through the deployment's intended private network or authenticated HTTPS proxy. The configured engine socket, shared GPU lease directory and persistent app state must be accessible to the process.

This repository builds and publishes `ghcr.io/maxexcloo/ai-control` for AMD64 and ARM64. Supply configuration and host resources externally:

```sh
docker run --rm --publish 127.0.0.1:1337:1337 \
  --mount type=bind,src=/absolute/path/control.json,dst=/config/control.json,readonly \
  --mount type=bind,src=/absolute/path/engine.sock,dst=/run/engine.sock \
  --mount type=bind,src=/absolute/path/leases,dst=/run/ai-gpu \
  --mount type=bind,src=/absolute/path/state,dst=/state \
  --mount type=bind,src=/proc,dst=/host/proc,readonly \
  --mount type=bind,src=/sys,dst=/host/sys,readonly \
  --env CONTROL_CONFIG=/config/control.json \
  --env CONTROL_API_KEY --env CONTROL_SESSION_KEY \
  ghcr.io/maxexcloo/ai-control:VERSION
```

Replace `VERSION` with the required release. Use the deployment's actual socket and directories. Workload bind mounts in the supplied create specifications use paths on the engine host. The lease mount must expose the same lock files used by the GPU workload wrappers; state must survive app recreation.

## Configuration

`CONTROL_CONFIG` names a JSON file containing `catalogue`, `engine`, `origins` and `users`. `origins` lists the exact browser origins allowed to submit changes. `users` maps existing account names to Argon2 password hashes; the app does not seed accounts. Keep this file private, including resolved workload environment values.

The catalogue provides:

- `media` and `models`: labelled legacy selection choices. Media IDs remain `off`, `image` and `pair`; model IDs include `off` and the configured text workload IDs.
- `rules.media`: allowed GPU placements for each media choice.
- `rules.profiles`: allowed placements for each workload, using `0`, `1` or `0,1`. Include `off: []`.
- `rules.roles`: `image` and `video` map to the corresponding configured workload IDs.
- `rules.runtime_names`: labels for runtime identities reported by the engine.
- `rules.workloads`: each workload has `kind` (`image`, `text` or `video`), `label` and `runtime`. Workload IDs and runtime IDs use lowercase letters, digits, dots, underscores and hyphens.
- `services`: links with `display_label`, `group`, `title` and `url`. Optional `container` and `profiles` fields associate a link with workload status.

`engine` requires `socket`, `state_dir`, `lease_dir`, `host_proc`, `host_sys` and `workloads`. For each catalogue workload, `engine.workloads[profile].placements[placement]` contains a `name` and a Docker-compatible container-create `config`. The deployment resolves images, environment, commands, mounts, devices, namespaces, limits, restart policy and health checks before passing these specifications to the app. AI Control does not read Compose files or interpolate deployment variables.

The synthetic fixtures in [tests/fixtures.py](tests/fixtures.py) show the shape without deployment data. The image/video pair uses GPU 0 for images and GPU 1 for video. Selection remains manual; the app serialises mutations, checks shared leases and retains the saved allocation.

`RADIANCE_API_KEY` and the configuration's `radiance_url` optionally enable the existing authenticated Radiance dashboard proxy. The provider key stays on the server. This integration does not change GPU allocation.

## Operations

The existing status, preview, selection, restart and job APIs remain available. Preview signatures guard against changes between review and Apply. Restart retains the running workload and its GPU placement. The maintenance Check action validates the engine connection and workload configuration; benchmark actions require a deployment-supplied benchmark specification and report an explicit error when unsupported.

Bearer authentication uses `CONTROL_API_KEY`. Browser sessions use the `ai_session` cookie. Mutating browser requests require an allowed origin and `X-AI-Control: 1`.

## Verify

Run `mise run check` for source checks, Python API and host-tool tests, Node UI interaction tests and an isolated UI build. Tests use synthetic configuration and simulated engine responses; they do not establish GPU generation on a live host. Linux also exercises the real GPU lease helper with disposable lock files.

Run `mise run image:build` and `mise run image:check` to build and smoke-test the image on the local Docker host. The image smoke test supplies temporary synthetic configuration and disposable test credentials, checks the actual UI and authenticated catalogue API, and removes its container. It does not contact a live engine.

## Release

Update the version in `pyproject.toml`, verify and commit the change, then push an annotated `vVERSION` tag matching that version. The container workflow runs source checks and native AMD64/ARM64 builds in parallel. Both images must pass their runtime smoke tests before publication.

Main publishes `latest` and `sha-COMMIT`; a matching release tag publishes `VERSION`. Intermediate architecture tags are unique to the workflow run and attempt. Publication uses the repository's `GITHUB_TOKEN`; an existing GHCR package must grant this repository Write access under its package settings' Manage Actions Access section.
