"""Thin authenticated client for AI Control's allocation API."""

import argparse
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, destination):
        return None


def api(path, payload=None):
    key = os.environ["CONTROL_API_KEY"]
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        f"http://127.0.0.1:{int(os.environ['CONTROL_PORT'])}/api/{path}",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    request.add_unredirected_header("Authorization", "Bearer " + key)
    try:
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            message = json.loads(error.read(65536)).get("error", "")
        except (ValueError, AttributeError, UnicodeDecodeError):
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
            "AI Control is unavailable at its configured port."
        ) from None


def wait_for_job(job, timeout=3900):
    deadline = time.monotonic() + timeout
    stage, shown = None, ""
    while True:
        if job.get("stage") and job["stage"] != stage:
            stage = job["stage"]
            print("== " + stage, flush=True)
        output = job.get("output", "")
        if output != shown:
            print(
                output.removeprefix(shown),
                end="",
                flush=True,
            )
            shown = output
        if job.get("state") == "completed":
            return job
        if job.get("state") != "running":
            raise RuntimeError(
                "AI Control reported that the operation failed. Review live status before retrying."
            )
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Timed out waiting for AI Control. Check live status before retrying; no request was repeated."
            )
        time.sleep(0.5)
        job = api("jobs/" + quote(job["id"], safe=""))


def apply(allocation):
    """Apply a complete allocation through the same preview and signature check as the UI."""
    preview = api("board/plan", {"allocation": allocation})
    return wait_for_job(
        api(
            "board",
            {
                "allocation": allocation,
                "replace": True,
                "signature": preview["signature"],
            },
        )
    )


def running():
    return {
        runtime: item
        for runtime, item in api("status")["runtimes"].items()
        if item["running"]
    }


def stop(runtime):
    current = running()
    if runtime != "all" and runtime not in current:
        raise ValueError(f"{runtime} is not running.")
    return apply(
        [
            {"profile": item["profile"], "devices": item["devices"]}
            for name, item in current.items()
            if runtime not in {"all", name}
        ]
    )


def restart(runtime):
    item = running().get(runtime)
    if not item:
        raise ValueError(f"{runtime} is not running.")
    return wait_for_job(
        api(
            "restart",
            {
                "runtime": runtime,
                "profile": item["profile"],
                "devices": item["devices"],
            },
        )
    )


def restore_allocation():
    path = Path(os.environ["AI_DATA_DIR"]) / "control/data/allocation.json"
    if path.exists():
        return apply(json.loads(path.read_text()))
    if running():
        print("No saved allocation; the running allocation was retained.", flush=True)
        return None
    # The initial allocation is supplied by the deployment, never chosen here.
    return apply(json.loads(os.environ.get("AI_BOOT_ALLOCATION", "[]")))


def placement(value):
    profile, separator, devices = value.partition("=")
    if not separator or not profile:
        raise argparse.ArgumentTypeError(
            "Use PROFILE=GPUS, such as comfyui-image=0 or gemma-31b=0,1."
        )
    return {"profile": profile, "devices": devices.split(",")}


def main(arguments):
    parser = argparse.ArgumentParser(prog="gpu", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status").add_argument("--json", action="store_true")
    commands.add_parser(
        "apply", help="Replace the allocation; no placements stops everything."
    ).add_argument("placements", nargs="*", type=placement)
    commands.add_parser("stop").add_argument(
        "runtime", help="A running runtime, or all."
    )
    commands.add_parser("restart").add_argument("runtime")
    commands.add_parser("bench")
    options = parser.parse_args(arguments)
    if options.command == "status":
        data = api("status")
        if options.json:
            print(json.dumps(data))
        else:
            for key in ["gpu0", "gpu1", *data["runtimes"]]:
                print(key + ": " + data[key])
    elif options.command == "apply":
        apply(options.placements)
    elif options.command == "stop":
        stop(options.runtime)
    elif options.command == "restart":
        restart(options.runtime)
    else:
        wait_for_job(api("maintenance", {"action": "benchmark"}))
