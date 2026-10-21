# AI Control

- This repository owns the standalone AI Control app and its two-GPU SSH dispatcher client. Deployment catalogues, accounts, hosts, leases and host runtime commands belong to the consuming deployment; never include them here.
- Read the catalogue, roles and access settings from the supplied configuration. Preserve explicit manual GPU selection, preview signatures and host-side allocation validation. Model downloads and profiles belong to Studio.
- Use the default Web Awesome theme and component appearance. Keep CSS limited to layout and operational feedback.
- Run `mise run check`, then build and smoke-test the actual container with `mise run image:build` and `mise run image:check` after image changes.
- This repository owns the Docker image and its publication. Keep AMD64 and ARM64 builds native and parallel with source checks; publish stable tags only after every check and image smoke test passes. A `v*` tag must match the version in `pyproject.toml`.
