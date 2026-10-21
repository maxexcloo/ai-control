"""Verify installed worker images and externally supplied Compose configuration."""

import os
import pathlib
import re
import subprocess

import yaml

from deployment import runtime as container_runtime

root = pathlib.Path(os.environ["AI_ROOT"])


def check_comfyui_versions(root, services):
    images = sorted(
        {
            service["image"]
            for name, service in services.items()
            if name.startswith("comfyui-")
        }
    )
    for image in images:
        labels = (
            ".Config.Labels" if container_runtime.engine() == "docker" else ".Labels"
        )
        expected = (
            subprocess.check_output(
                container_runtime.command(
                    "image",
                    "inspect",
                    "--format",
                    "{{index " + labels + ' "io.excloo.comfyui.version"}}',
                    image,
                ),
                text=True,
            )
            .strip()
            .removeprefix("v")
        )
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", expected):
            raise SystemExit("Missing ComfyUI image version metadata.")
        code = "import runpy; print(runpy.run_path('/root/ComfyUI/comfyui_version.py')['__version__'])"
        actual = subprocess.check_output(
            container_runtime.command(
                "run",
                "--rm",
                "--entrypoint",
                "python3",
                image,
                "-c",
                code,
            ),
            text=True,
        ).strip()
        if actual != expected:
            raise SystemExit(
                f"{image}: packaged ComfyUI core {actual} differs from image metadata {expected}."
            )


def check_runtime():
    from deployment.studio_worker import verify

    services = yaml.safe_load((root / "compose.yaml").read_text())["services"]
    check_comfyui_versions(root, services)
    print("Packaged ComfyUI core versions match image metadata.")
    verify(root)

    from deployment.engine_config import placement_environment
    from deployment.workloads import WORKLOADS

    # Render each real placement; inactive text services still require interpolation.
    profiles = sorted(
        {
            profile
            for service in services.values()
            for profile in service.get("profiles", [])
        }
    )
    count = 0
    for profile in profiles:
        placements = (
            WORKLOADS[profile]["placements"] if profile in WORKLOADS else [None]
        )
        for devices in placements:
            environment = (
                placement_environment(profile, devices)
                if devices is not None
                else container_runtime.compose_environment()
            )
            subprocess.run(
                container_runtime.compose("--profile", profile, "config"),
                check=True,
                stdout=subprocess.DEVNULL,
                cwd=root,
                env=environment,
            )
            count += 1
    print(f"Compose configuration passed for {count} profile placements.")
