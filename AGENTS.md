# AI Control

- This repository owns the standalone AI Control app and its two-GPU SSH dispatcher client. Deployment catalogues, accounts, hosts, leases and host runtime commands belong to the consuming deployment; never include them here.
- Read the catalogue, roles and access settings from the supplied configuration. Preserve explicit manual GPU selection, preview signatures and host-side allocation validation. Model downloads and profiles belong to Studio.
- Use the default Web Awesome theme and component appearance. Keep CSS limited to layout and operational feedback.
- Run `mise run check`. Images are built and published by `maxexcloo/images`, which consumes an exact source release from this repository; this repository does not publish container images.
