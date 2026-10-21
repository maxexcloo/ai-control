"""Run host tools against an external configuration directory."""

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path


def load_environment(root):
    """Read literal dotenv assignments without shell execution or secret output."""
    path = root / ".env"
    if path.exists():
        for number, raw in enumerate(path.read_text().splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.removeprefix("export ").partition("=")
            if not separator or not key.isidentifier():
                raise ValueError(f"Invalid .env assignment on line {number}.")
            if value[:1] in {'"', "'"}:
                parts = shlex.split(value, comments=True)
                if len(parts) != 1:
                    raise ValueError(f"Invalid .env value on line {number}.")
                value = parts[0]
            os.environ.setdefault(key, value)
    os.environ["AI_ROOT"] = str(root)
    for key in ["AI_DATA_DIR", "AI_MODELS_DIR", "CONTROL_ENGINE_SOCKET"]:
        if not os.environ.get(key) or not Path(os.environ[key]).is_absolute():
            raise ValueError(f"{key} must be an absolute path.")
    from deployment import runtime

    runtime.engine()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument(
        "command",
        choices=["boot", "check", "configure", "deploy", "gpu", "prepare", "pull"],
    )
    arguments, remaining = parser.parse_known_args()
    root = arguments.root.resolve(strict=True)
    load_environment(root)
    if arguments.command != "gpu" and remaining:
        parser.error("Unexpected arguments")
    from deployment import runtime
    from deployment.check import check_runtime
    from deployment.configure import apply
    from deployment.gpu_prepare import prepare

    def interfaces():
        subprocess.run(
            runtime.compose(
                "--profile",
                "control",
                "up",
                "--detach",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "60",
                "control",
            ),
            cwd=root,
            env=runtime.compose_environment(),
            check=True,
        )

    if arguments.command == "prepare":
        data = Path(os.environ["AI_DATA_DIR"])
        for name in ["betterbench/data", "control/data", "radiance/data"]:
            (data / name).mkdir(parents=True, exist_ok=True)
        for worker in ["comfyui-image", "comfyui-video"]:
            for name in ["cache", "config", "input", "output", "user"]:
                (data / worker / "data" / name).mkdir(parents=True, exist_ok=True)
        for name in ["comfyui", "radiance"]:
            (Path(os.environ["AI_MODELS_DIR"]) / name).mkdir(
                parents=True, exist_ok=True
            )
        prepare(data)
    elif arguments.command == "check":
        check_runtime()
    elif arguments.command == "configure":
        apply(root)
    elif arguments.command == "deploy":
        check_runtime()
        apply(root)
        interfaces()
    elif arguments.command == "boot":
        interfaces()
        from deployment.gpu import restore_allocation

        restore_allocation()
    elif arguments.command == "pull":
        subprocess.run(
            runtime.compose(
                "--profile",
                "benchmark",
                "--profile",
                "comfyui-image",
                "--profile",
                "control",
                "pull",
                "betterbench",
                "comfyui-image",
                "control",
            ),
            cwd=root,
            env=runtime.compose_environment(),
            check=True,
        )
    else:
        from deployment.gpu import main as gpu

        sys.argv = [sys.argv[0], *remaining]
        gpu()


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError) as error:
        raise SystemExit(str(error)) from None
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
    except subprocess.CalledProcessError as error:
        raise SystemExit(
            f"{error.cmd[0]} failed (exit {error.returncode}); private output withheld."
        ) from None
