"""Run host tools against an external configuration directory through the engine socket."""

import argparse
import os
import tempfile
from pathlib import Path

from deployment import gpu
from deployment.config import Deployment, load_environment
from deployment.engine import Engine

# renovate: datasource=docker depName=docker.io/library/busybox
BUSYBOX = "docker.io/library/busybox:1.38.0"


def prepare(engine, deployment):
    """Create writable bind directories and the GPU lease helper, preserving existing lease files."""
    data = Path(os.environ["AI_DATA_DIR"])
    for path in deployment.writable_directories():
        path.mkdir(parents=True, exist_ok=True)
    binaries = data / "control/bin"
    binaries.mkdir(parents=True, exist_ok=True)
    # The stock Radiance image has no flock; the lease helper uses this static copy.
    engine.pull(BUSYBOX)
    with tempfile.NamedTemporaryFile(
        dir=binaries, prefix=".busybox-", delete=False
    ) as stream:
        stream.write(engine.read_file(BUSYBOX, "/bin/busybox"))
    Path(stream.name).chmod(0o755)
    Path(stream.name).replace(binaries / "busybox")
    helper = binaries / "gpu-lease.sh"
    helper.write_bytes(Path(__file__).with_name("gpu-lease.sh").read_bytes())
    helper.chmod(0o755)
    leases = data / "control/leases"
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
        os.fchmod(descriptor, 0o666)
        os.close(descriptor)
    print(
        "Runtime directories and GPU lease helper prepared; existing lease files were preserved."
    )


def check_workers(engine, deployment):
    """Workers must carry the SDK of the exact Studio release they serve."""
    studio = deployment.containers["studio"]
    for image in deployment.worker_images():
        details = engine.inspect("images", image)
        if details is None:
            raise SystemExit(f"Pull {image} before deploying.")
        built = (details["Config"].get("Labels") or {}).get("io.excloo.studio.image")
        if built != studio:
            raise SystemExit(
                f"{image} was built with {built or 'no recorded Studio release'}, not {studio}."
            )
    print(f"Workers carry the Studio {studio} SDK.")


def start_control(engine, deployment):
    spec = deployment.spec("control")
    engine.replace(spec)
    engine.wait_healthy(spec["name"])
    print("AI Control is running.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("command", choices=["prepare", "pull", "deploy", "boot", "gpu"])
    options, remaining = parser.parse_known_args()
    if options.command != "gpu" and remaining:
        parser.error("Unexpected arguments")
    root = options.root.resolve(strict=True)
    load_environment(root)
    engine = Engine(os.environ["CONTROL_ENGINE_SOCKET"])
    if options.command == "gpu":
        gpu.main(remaining)
        return
    deployment = Deployment(root)
    if options.command == "prepare":
        prepare(engine, deployment)
    elif options.command == "pull":
        for image in deployment.images():
            print("Pulling " + image, flush=True)
            engine.pull(image)
    elif options.command == "deploy":
        check_workers(engine, deployment)
        deployment.write_control()
        start_control(engine, deployment)
    else:
        start_control(engine, deployment)
        gpu.restore_allocation()


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, RuntimeError) as error:
        raise SystemExit(
            f"Missing setting {error}." if isinstance(error, KeyError) else str(error)
        ) from None
