# AI Control

AI Control is a web UI and authenticated API for manually allocating two GPUs. It previews changes, checks the current allocation before applying them and runs a restricted SSH dispatcher on the compute host. Workloads and service links come from deployment configuration.

The current host integration uses the existing SSH task protocol and Podman workloads. Docker and Kubernetes backends are not implemented. AI Control does not manage models, downloads or generation profiles.

## Run

Install the [shared Mise tools and tasks](https://github.com/maxexcloo/dotfiles#shared-tools--tasks), then run `mise trust`, `mise run setup` and `mise run build`.

Provide an existing access configuration through `CONTROL_CONFIG`, then set `CONTROL_API_KEY`, `CONTROL_SESSION_KEY` and `CONTROL_SSH_HOST` using the deployment's credential tooling. Run `mise run serve`. The app listens on port 1337; expose it through the deployment's intended private network or authenticated HTTPS proxy. Mount the SSH client configuration and its credential references at `/ssh/config`.

This repository builds and publishes `ghcr.io/maxexcloo/ai-control` for AMD64 and ARM64. The image includes the built UI and runtime dependencies. Supply configuration, credentials and SSH access externally:

```sh
docker run --rm --publish 127.0.0.1:1337:1337 \
  --mount type=bind,src=/absolute/path/control.json,dst=/config/control.json,readonly \
  --mount type=bind,src=/absolute/path/ssh,dst=/ssh,readonly \
  --env CONTROL_CONFIG=/config/control.json \
  --env CONTROL_API_KEY --env CONTROL_SESSION_KEY --env CONTROL_SSH_HOST \
  ghcr.io/maxexcloo/ai-control:VERSION
```

Replace `VERSION` with the required release. The environment variables above must already be set through the deployment's credential tooling. The container runs AI Control; the controlled host still uses the documented SSH dispatcher.

## Configuration

`CONTROL_CONFIG` names a JSON file containing `catalogue`, `origins` and `users`. `origins` lists the exact browser origins allowed to submit changes. `users` maps existing account names to Argon2 password hashes; the app does not seed accounts. Keep this file private.

The catalogue provides:

- `media` and `models`: labelled legacy selection choices. Media IDs remain `off`, `image` and `pair`; model IDs include `off` and the configured text workload IDs.
- `rules.media`: allowed GPU placements for each media choice.
- `rules.profiles`: allowed placements for each workload, using `0`, `1` or `0,1`. Include `off: []`.
- `rules.roles`: `image` and `video` map to the corresponding configured workload IDs.
- `rules.runtime_names`: labels for runtime identities reported by the host.
- `rules.workloads`: each workload has `kind` (`image`, `text` or `video`), `label` and `runtime`. Workload IDs and runtime IDs use lowercase letters, digits, dots, underscores and hyphens.
- `services`: links with `display_label`, `group`, `title` and `url`. Optional `container` and `profiles` fields associate a link with host status.

The synthetic catalogue in [tests/fixtures.py](tests/fixtures.py) shows the complete shape without deployment data. The host owns the real catalogue and revalidates every allocation. The image/video pair uses GPU 0 for images and GPU 1 for video.

`RADIANCE_API_KEY` and the configuration's `radiance_url` optionally enable the existing authenticated Radiance dashboard proxy. The provider key stays on the server. This integration does not change GPU allocation.

## Dispatcher Contract

The host returns live JSON for `gpu:status --json` and accepts `gpu:apply`, `gpu:restart`, the `media:*`, `text:*` and `video:*` commands used by the selection API, plus `check` for maintenance. It must restrict these commands and validate arguments and GPU leases. Progress lines start with `@@ai-stage `.

Bearer authentication uses `CONTROL_API_KEY`. Browser sessions use the `ai_session` cookie; changing from an older cookie name requires signing in again. Mutating browser requests require an allowed origin and `X-AI-Control: 1`.

## Verify

Run `mise run check` for source checks, Python API tests, Node UI interaction tests and an isolated UI build. Test dependencies and build output stay in temporary directories. The tests simulate the SSH dispatcher; they do not verify GPU workloads or a live host.

Run `mise run image:build` and `mise run image:check` to build and smoke-test the image on the local Docker host. The image smoke test mounts a temporary synthetic catalogue and disposable test credentials, checks the actual UI and authenticated catalogue API, and removes its container. It does not contact a compute host.

## Release

Update the version in `pyproject.toml`, verify and commit the change, then push an annotated `vVERSION` tag matching that version. The container workflow runs source checks and native AMD64/ARM64 builds in parallel. Both images must pass their runtime smoke tests before publication.

Main publishes `latest` and `sha-COMMIT`; a matching release tag publishes `VERSION`. Intermediate architecture tags are unique to the workflow run and attempt. Publication uses the repository's `GITHUB_TOKEN`; an existing GHCR package must grant this repository Write access under its package settings' Manage Actions Access section.
