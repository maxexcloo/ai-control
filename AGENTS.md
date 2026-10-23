# AI Control

- This repository owns AI Control's manual two-GPU allocation, container lifecycle, shared lease checks and saved allocation. Use the mounted Docker-compatible engine socket directly; do not add SSH or a container CLI dependency.
- Deployment catalogues, accounts, host addresses and resolved container-create specifications belong to the consuming deployment. Read them from supplied configuration and never bake them into the image or expose resolved specifications through the public catalogue API.
- Preserve manual GPU selection, preview signatures, mutation serialisation, shared lock-file identities and runtime state across app recreation. Model downloads and generation profiles belong to Studio.
- Use the default Web Awesome theme and component appearance. Keep CSS limited to layout and operational feedback.
- Run `mise run check`, then build and smoke-test the actual container with `mise run image:build` and `mise run image:check` after image changes.
- Host deployment tools live in `deployment/` and consume an external configuration directory through the engine socket only. Keep private catalogues, container specifications, credentials and runtime state outside this repository and its image.
- A `v*` tag must match the version in `pyproject.toml`.
