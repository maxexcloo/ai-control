"""Thin authenticated client for an externally supplied AI deployment."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

from deployment.allocation import normalise_allocation
from deployment import runtime as container_runtime
from deployment.workloads import CONTAINERS, GPU_RULES, PROFILES, WORKLOADS

ROOT = Path(os.environ["AI_ROOT"])


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, destination):
        return None


def api(path, payload=None):
    host = os.environ["AI_HOST"].strip()
    if (
        not host
        or "/" in host
        or "@" in host
        or any(character.isspace() for character in host)
    ):
        raise ValueError("AI_HOST must be a configured hostname or IP address.")
    try:
        port = int(os.environ["CONTROL_PORT"])
    except ValueError:
        raise ValueError("CONTROL_PORT must be a valid port number.") from None
    if not 1 <= port <= 65535:
        raise ValueError("CONTROL_PORT must be a valid port number.")
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    key = os.environ["CONTROL_API_KEY"]
    if not key:
        raise ValueError("CONTROL_API_KEY is required.")
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        f"http://{host}:{port}/api/{path}",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    request.add_unredirected_header("Authorization", "Bearer " + key)
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            result = json.loads(error.read(65536))
            message = result.get("error", "") if isinstance(result, dict) else ""
        except (ValueError, UnicodeDecodeError):
            message = ""
        finally:
            error.close()
        detail = (
            ": " + message[:500].replace(key, "[redacted]")
            if isinstance(message, str) and message
            else ""
        )
        raise RuntimeError(
            f"AI Control rejected the request (HTTP {error.code}){detail}"
        ) from None
    except OSError:
        raise RuntimeError(
            "AI Control is unavailable at its configured address."
        ) from None


def status():
    return api("status")


def wait_for_job(job, timeout=3900):
    deadline = time.monotonic() + timeout
    stage, shown = None, ""
    while True:
        if job.get("stage") and job["stage"] != stage:
            stage = job["stage"]
            print("@@ai-stage " + stage, flush=True)
        output = job.get("output", "")
        if output != shown:
            print(
                output.removeprefix(shown),
                end="",
                flush=True,
            )
            shown = output
        state = job.get("state")
        if state == "completed":
            return job
        if state == "failed":
            raise RuntimeError(
                "AI Control reported that the operation failed. Review live status before retrying."
            )
        if state != "running" or not isinstance(job.get("id"), str) or not job["id"]:
            raise RuntimeError("AI Control returned an invalid operation response.")
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Timed out waiting for AI Control. Check live status before retrying; no request was repeated."
            )
        time.sleep(0.5)
        job = api("jobs/" + quote(job["id"], safe=""))


def submit(path, payload):
    return wait_for_job(api(path, payload))


def apply_board(allocation):
    allocation = normalise_allocation(allocation, GPU_RULES)
    preview = api("board/plan", {"allocation": allocation})
    return submit(
        "board",
        {"allocation": allocation, "replace": True, "signature": preview["signature"]},
    )


def select(profile, devices, replace=False):
    if profile == "off":
        return submit("selections", {"kind": "text", "model": "off", "replace": True})
    if profile not in WORKLOADS or devices not in GPU_RULES["profiles"][profile]:
        raise ValueError("Choose a known workload and a supported GPU placement.")
    kind = WORKLOADS[profile]["kind"]
    payload = (
        {"kind": "text", "model": profile}
        if kind == "text"
        else {"kind": "media", "mode": "image"}
        if kind == "image"
        else {"kind": "video", "mode": "on"}
    )
    return submit("selections", dict(payload, gpus=devices, replace=replace))


def stop(runtime):
    if runtime not in CONTAINERS and runtime != "all":
        raise ValueError("Choose a known runtime or all.")
    if runtime == "all":
        return apply_board([])
    if runtime == "text":
        payload = {"kind": "text", "model": "off"}
    elif runtime == WORKLOADS[GPU_RULES["roles"]["image"]]["runtime"]:
        payload = {"kind": "media", "mode": "off", "runtime": runtime}
    else:
        payload = {"kind": "video", "mode": "off"}
    return submit("selections", dict(payload, replace=True))


def restart(runtime, profile=None, devices=None):
    item = status()["runtimes"].get(runtime, {})
    if not item.get("running") or not item.get("profile") or not item.get("devices"):
        raise RuntimeError(
            "Only a running workload with a known GPU placement can be restarted."
        )
    if (
        profile is not None
        and profile != item["profile"]
        or devices is not None
        and devices != ",".join(item["devices"])
    ):
        raise RuntimeError(
            "The live workload changed. Review refreshed status before restarting."
        )
    return submit(
        "restart",
        {"runtime": runtime, "profile": item["profile"], "devices": item["devices"]},
    )


def remember_allocation():
    path = Path(os.environ["AI_DATA_DIR"]) / "control/data/allocation.json"
    if path.exists():
        print("Existing saved allocation retained.", flush=True)
        return
    current = status()["runtimes"]
    if any(
        item["running"] and item.get("health") != "healthy" for item in current.values()
    ):
        raise RuntimeError(
            "The running allocation is not ready; its saved state was retained."
        )
    allocation = normalise_allocation(
        [
            {"profile": item["profile"], "devices": item["devices"]}
            for item in current.values()
            if item["running"]
        ],
        GPU_RULES,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, prefix=".allocation-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(allocation, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                print("Existing saved allocation retained.", flush=True)
        finally:
            temporary.unlink(missing_ok=True)


def restore_allocation():
    path = Path(os.environ["AI_DATA_DIR"]) / "control/data/allocation.json"
    if path.exists():
        return apply_board(
            normalise_allocation(json.loads(path.read_text()), GPU_RULES)
        )
    if any(item["running"] for item in status()["runtimes"].values()):
        print("No saved allocation; the running allocation was retained.", flush=True)
        return
    # Initial boot allocation is supplied by the deployment, never chosen here.
    return apply_board(
        normalise_allocation(json.loads(os.environ["AI_BOOT_ALLOCATION"]), GPU_RULES)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ["start", "select"]:
        child = commands.add_parser(command)
        child.add_argument("profile", choices=[*PROFILES, "off"])
        child.add_argument("devices", choices=["0", "1", "0,1"])
    media = commands.add_parser("media")
    media.add_argument("mode", choices=["image", "pair", "off"])
    media.add_argument("devices", choices=["0", "1", "0,1"])
    for command in ["logs", "stop"]:
        child = commands.add_parser(command)
        child.add_argument(
            "runtime", choices=[*CONTAINERS, "all"] if command == "stop" else CONTAINERS
        )
    child = commands.add_parser("restart")
    child.add_argument("runtime", choices=CONTAINERS)
    child.add_argument("--profile", choices=PROFILES)
    child.add_argument("--gpus", choices=["0", "1", "0,1"])
    child = commands.add_parser("status")
    child.add_argument("--json", action="store_true")
    for command in ["bench", "restore", "remember"]:
        commands.add_parser(command)
    child = commands.add_parser("board")
    child.add_argument("allocation")
    arguments = parser.parse_args()
    if arguments.command == "status":
        data = status()
        if arguments.json:
            print(json.dumps(data))
        else:
            for key in ["gpu0", "gpu1", *CONTAINERS]:
                print(key + ": " + data[key])
    elif arguments.command == "logs":
        subprocess.run(
            container_runtime.command(
                "logs", "--follow", CONTAINERS[arguments.runtime]
            ),
            check=True,
        )
    elif arguments.command == "restart":
        restart(arguments.runtime, arguments.profile, arguments.gpus)
    elif arguments.command == "restore":
        restore_allocation()
    elif arguments.command == "remember":
        remember_allocation()
    elif arguments.command == "board":
        try:
            allocation = json.loads(bytes.fromhex(arguments.allocation))
        except (ValueError, TypeError, UnicodeDecodeError):
            raise ValueError("Supply a valid encoded GPU allocation.") from None
        apply_board(allocation)
    elif arguments.command in ["start", "select"]:
        select(
            arguments.profile, arguments.devices, replace=arguments.command == "select"
        )
    elif arguments.command == "media":
        submit(
            "selections",
            {
                "kind": "media",
                "mode": arguments.mode,
                "gpus": arguments.devices,
                "replace": True,
            },
        )
    elif arguments.command == "stop":
        stop(arguments.runtime)
    elif arguments.command == "bench":
        submit("maintenance", {"action": "benchmark"})


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(error, file=sys.stderr)
        sys.exit(64)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        message = (
            f"{error.cmd[0]} failed (exit {error.returncode})."
            if isinstance(error, subprocess.CalledProcessError)
            else str(error)
        )
        print(message, file=sys.stderr)
        sys.exit(
            error.returncode if isinstance(error, subprocess.CalledProcessError) else 75
        )
