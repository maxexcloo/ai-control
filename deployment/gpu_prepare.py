"""Prepare the static GPU lease helper without replacing existing lease files."""

import os
import subprocess
import tempfile
from pathlib import Path

from deployment.runtime import command

ROOT = Path(os.environ["AI_DATA_DIR"])
# renovate: datasource=docker depName=docker.io/library/busybox
IMAGE = "docker.io/library/busybox:1.38.0"


def prepare(root=ROOT):
    binary_directory = root / "control/bin"
    binary_directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".busybox-", dir=binary_directory
    ) as temporary:
        staged = Path(temporary) / "busybox"
        container = subprocess.run(
            command("create", IMAGE, "true"), check=True, capture_output=True, text=True
        ).stdout.strip()
        if not container:
            raise RuntimeError("Container engine returned no BusyBox container ID.")
        try:
            subprocess.run(
                command("cp", f"{container}:/bin/busybox", str(staged)), check=True
            )
            staged.chmod(0o755)
        finally:
            subprocess.run(
                command("rm", "--force", container),
                check=True,
                stdout=subprocess.DEVNULL,
            )
        staged.replace(binary_directory / "busybox")

    helper = binary_directory / "gpu-lease.sh"
    helper.write_bytes(Path(__file__).with_name("gpu-lease.sh").read_bytes())
    helper.chmod(0o755)

    leases = root / "control/leases"
    leases.mkdir(parents=True, exist_ok=True)
    for number in range(2):
        try:
            descriptor = os.open(
                leases / f"gpu{number}.lock",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o666,
            )
        except FileExistsError:
            continue
        try:
            os.fchmod(descriptor, 0o666)
        finally:
            os.close(descriptor)


def main():
    prepare()
    print("GPU lease helper prepared. Existing lease files were preserved.")


if __name__ == "__main__":
    main()
