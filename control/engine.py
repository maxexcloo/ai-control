"""Manual GPU allocation through a local Docker-compatible engine socket."""

import asyncio
import copy
import fcntl
import hashlib
import http.client
import json
import socket
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from . import metrics
from .allocation import normalise_allocation


class OperationError(RuntimeError):
    """An operational failure whose message contains no private request data."""


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=130)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class Engine:
    def __init__(self, path):
        self.path = path

    def request(self, method, path, body=None, missing=False, raw=False):
        connection = UnixConnection(self.path)
        try:
            data = json.dumps(body).encode() if body is not None else None
            connection.request(method, path, data, {"Content-Type": "application/json"})
            response = connection.getresponse()
            content = response.read()
            if missing and response.status == 404:
                return None
            if response.status not in {200, 201, 204, 304}:
                # Engine error bodies can include supplied environment values.
                raise RuntimeError(
                    f"Engine {method} {path.split('?')[0]} failed ({response.status})."
                )
            if raw:
                return content
            if not content:
                return {}
            try:
                return json.loads(content)
            except ValueError:
                return content.decode(errors="replace")
        finally:
            connection.close()


@contextmanager
def lock(path):
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OperationError(
                "Another control change or GPU reservation is active."
            ) from None
        yield


class Operation:
    def __init__(self, config, output=None):
        self.config = config
        self.options = config["engine"]
        self.rules = config["catalogue"]["rules"]
        self.definitions = self.options["workloads"]
        self.engine = Engine(self.options["socket"])
        self.state = Path(self.options["state_dir"])
        self.leases = Path(self.options["lease_dir"])
        self.output = output or (lambda value: None)
        self.names = {}
        text_runtimes = {
            item["runtime"]
            for item in self.rules["workloads"].values()
            if item["kind"] == "text"
        }
        if len(text_runtimes) != 1:
            raise ValueError("Text workloads must share one runtime.")
        self.text_runtime = next(iter(text_runtimes))
        if set(self.definitions) != set(self.rules["workloads"]):
            raise ValueError("Supply engine definitions for every catalogue workload.")
        for profile, definition in self.definitions.items():
            if profile not in self.rules["workloads"]:
                raise ValueError(
                    "Engine workload is absent from the supplied catalogue."
                )
            runtime = self.rules["workloads"][profile]["runtime"]
            if set(definition["placements"]) != set(self.rules["profiles"][profile]):
                raise ValueError(
                    "Supply engine definitions for every supported GPU placement."
                )
            for placement, spec in definition["placements"].items():
                self.validate(profile, placement)
                body = self.container_config(spec)
                environment = dict(
                    value.split("=", 1) for value in body.get("Env", []) if "=" in value
                )
                if (
                    body.get("Labels", {}).get("com.docker.compose.service") != profile
                    or environment.get("AI_GPU_DEVICES") != placement
                ):
                    raise ValueError(
                        "Engine workload identity and GPU placement must match the catalogue."
                    )
                previous = self.names.setdefault(runtime, spec["name"])
                if previous != spec["name"] or not spec["config"]["Image"]:
                    raise ValueError(
                        "Each runtime needs one configured container name and an image."
                    )
        if len(set(self.names.values())) != len(self.names):
            raise ValueError("Configured runtimes must have distinct container names.")

    def stage(self, value):
        self.output("@@ai-stage " + value + "\n")

    def validate(self, profile, placement):
        if (
            profile not in self.rules["profiles"]
            or profile == "off"
            or placement not in self.rules["profiles"][profile]
        ):
            raise ValueError("Choose a known workload and a supported GPU placement.")

    def spec(self, profile, placement):
        self.validate(profile, placement)
        try:
            return self.definitions[profile]["placements"][placement]
        except KeyError:
            raise ValueError(
                "The requested workload placement has no supplied engine definition."
            ) from None

    def inspect(self):
        result = {}
        for name in self.names.values():
            item = self.engine.request(
                "GET", "/containers/" + quote(name, safe="") + "/json", missing=True
            )
            if item is None:
                continue
            environment = dict(
                value.split("=", 1)
                for value in item["Config"].get("Env", [])
                if "=" in value
            )
            placement = environment.get(
                "AI_GPU_DEVICES", environment.get("HIP_VISIBLE_DEVICES", "")
            )
            labels = item["Config"].get("Labels") or {}
            state = item["State"]
            health = state.get("Health") or state.get("Healthcheck") or {}
            result[name] = {
                "devices": set(placement.split(","))
                if placement in {"0", "1", "0,1"}
                else set(),
                "health": health.get("Status", "unknown"),
                "running": state.get("Running", False),
                # The deployment sets this label on every workload spec; the name predates dropping Compose.
                "service": labels.get("com.docker.compose.service", ""),
            }
        return result

    def status(self):
        current = self.inspect()
        result = {
            "services": {
                name: {"running": item["running"]} for name, item in current.items()
            },
            "gpu_metrics": metrics.gpu_metrics(Path(self.options["host_sys"])),
            "host_metrics": metrics.host_metrics(
                self.state,
                Path(self.options["host_proc"]),
                Path(self.options["host_sys"]),
            ),
            "name": self.config.get("host", "AI"),
            "updated": datetime.now(UTC).isoformat(timespec="seconds"),
            "runtimes": {},
        }
        names = {}
        digests = self.digests()
        for runtime, name in self.names.items():
            item = current.get(name, {})
            active = item.get("running", False)
            workload = self.rules["workloads"].get(item.get("service"), {})
            title = workload.get(
                "model_label",
                workload.get("label", self.rules["runtime_names"][runtime]),
            )
            names[name] = workload.get("label", self.rules["runtime_names"][runtime])
            devices = sorted(item.get("devices", set()))
            result[runtime] = title + " · GPU " + ",".join(devices) if active else "Off"
            result[runtime + "_state"] = (
                item.get("health", "unknown") if active else "stopped"
            )
            result["runtimes"][runtime] = {
                "running": active,
                "profile": item.get("service", ""),
                "devices": devices,
                "health": result[runtime + "_state"],
                "config_current": bool(
                    active
                    and item.get("service") in self.definitions
                    and ",".join(devices)
                    in self.definitions[item["service"]]["placements"]
                    and digests.get(name)
                    == self.digest(item["service"], ",".join(devices))
                ),
            }
            if runtime == self.text_runtime:
                result.update(
                    text_profile=item.get("service", "") if active else "off",
                    text_devices=",".join(devices) if active else "",
                    text_model=title.split(" · ")[0] if active else "Off",
                    text_placement="GPU " + ",".join(devices) if active else "No GPUs",
                )
        for device in ["0", "1"]:
            owners = [
                names[name]
                for name, item in current.items()
                if item["running"] and device in item["devices"]
            ]
            availability = "Not Prepared"
            path = self.leases / f"gpu{device}.lock"
            if path.is_file():
                try:
                    with lock(path):
                        availability = "Available"
                except RuntimeError:
                    availability = "Reserved"
            result["gpu" + device] = ", ".join(owners) if owners else availability
        return result

    def digest(self, profile, placement):
        return hashlib.sha256(
            json.dumps(
                self.spec(profile, placement), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()

    def digests(self):
        try:
            return json.loads((self.state / "config-digests.json").read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def write_state(self, name, value):
        import os
        import tempfile

        self.state.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=self.state, prefix="." + name, delete=False
        ) as stream:
            temporary = Path(stream.name)
            try:
                json.dump(value, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
                os.replace(temporary, self.state / name)
            finally:
                temporary.unlink(missing_ok=True)

    def stop(self, names):
        for name in sorted(names):
            self.stage("Stopping " + name)
            self.engine.request(
                "POST", "/containers/" + quote(name, safe="") + "/stop", missing=True
            )

    def wait_ready(self, name, profile, placement):
        self.stage("Waiting For " + profile + " Readiness")
        deadline = time.monotonic() + 900
        while True:
            item = self.inspect().get(name, {})
            if (
                not item.get("running")
                or item.get("service") != profile
                or item.get("devices") != set(placement.split(","))
            ):
                raise OperationError(
                    "The requested workload failed startup; inspect its container logs."
                )
            if item.get("health") == "healthy":
                break
            if item.get("health") == "unhealthy" or time.monotonic() > deadline:
                raise OperationError(
                    "The requested workload did not become ready; inspect its container logs."
                )
            time.sleep(1)
        self.stage(profile + " Ready")

    def container_config(self, spec):
        body = copy.deepcopy(spec["config"])
        host = body.setdefault("HostConfig", {})
        if "keep-groups" in host.get("GroupAdd", []):
            if host["GroupAdd"] != ["keep-groups"]:
                raise ValueError(
                    "Original supplementary groups cannot be combined with additional groups."
                )
            # Podman compatibility create treats this CLI token as a literal group.
            # crun's OCI annotation preserves the actual rootless host groups instead.
            host.pop("GroupAdd")
            host.setdefault("Annotations", {})["run.oci.keep_original_groups"] = "1"
        return body

    def start(self, profile, placement):
        spec = self.spec(profile, placement)
        name = spec["name"]
        body = self.container_config(spec)
        self.stage("Starting " + profile + " On GPU " + placement)
        self.engine.request(
            "POST", "/containers/create?" + urlencode({"name": name}), body
        )
        self.engine.request("POST", "/containers/" + quote(name, safe="") + "/start")
        self.wait_ready(name, profile, placement)
        digests = self.digests()
        digests[name] = self.digest(profile, placement)
        self.write_state("config-digests.json", digests)

    def apply(self, allocation):
        allocation = normalise_allocation(allocation, self.rules)
        plan = [(item["profile"], ",".join(item["devices"])) for item in allocation]
        for profile, placement in plan:
            self.container_config(self.spec(profile, placement))
        self.stage("Validating Allocation")
        current = self.inspect()
        digests = self.digests()
        targets = {
            self.names[self.rules["workloads"][profile]["runtime"]]
            for profile, _ in plan
        }
        wanted = {device for _, placement in plan for device in placement.split(",")}
        unchanged = {
            self.names[self.rules["workloads"][profile]["runtime"]]
            for profile, placement in plan
            if (
                item := current.get(
                    self.names[self.rules["workloads"][profile]["runtime"]], {}
                )
            ).get("running")
            and item["health"] == "healthy"
            and item["service"] == profile
            and item["devices"] == set(placement.split(","))
            and digests.get(self.names[self.rules["workloads"][profile]["runtime"]])
            == self.digest(profile, placement)
        }
        conflicts = set()
        for name, item in current.items():
            if not item["running"]:
                continue
            if not item["devices"]:
                raise OperationError(
                    "A managed workload has unknown GPU placement; no runtimes were stopped."
                )
            if name not in unchanged:
                conflicts.add(name)
        for device in sorted(wanted):
            path = self.leases / f"gpu{device}.lock"
            if not path.is_file():
                raise OperationError("GPU leases are not prepared.")
            try:
                with lock(path):
                    pass
            except RuntimeError:
                if not any(
                    item["running"] and device in item["devices"]
                    for item in current.values()
                ):
                    raise OperationError(
                        "A GPU has an unknown reservation; no runtimes were stopped."
                    ) from None
        for profile, placement in plan:
            if self.names[self.rules["workloads"][profile]["runtime"]] not in unchanged:
                image = self.spec(profile, placement)["config"]["Image"]
                image_config = self.engine.request(
                    "GET", "/images/" + quote(image, safe="") + "/json", missing=True
                )
                if image_config is None:
                    raise OperationError(
                        "Pull the configured workload image before changing GPU allocation."
                    )
                health = dict(image_config.get("Config", {}).get("Healthcheck") or {})
                health.update(
                    self.spec(profile, placement)["config"].get("Healthcheck") or {}
                )
                if not health.get("Test") or health["Test"][0] == "NONE":
                    raise OperationError(
                        "Configure a workload health check before changing GPU allocation."
                    )
        self.stage("GPU Reservations Checked")
        self.stop(conflicts)
        for name in sorted(targets - unchanged):
            if name in current:
                self.engine.request(
                    "DELETE", "/containers/" + quote(name, safe="") + "?v=true"
                )
        for profile, placement in plan:
            if self.names[self.rules["workloads"][profile]["runtime"]] not in unchanged:
                self.start(profile, placement)
        self.remember()
        self.stage("Allocation Ready")

    def remember(self):
        allocation = []
        for item in self.inspect().values():
            if not item["running"]:
                continue
            if (
                item["service"] not in self.rules["workloads"]
                or item["health"] != "healthy"
            ):
                raise OperationError(
                    "The running allocation is not ready; its saved state was retained."
                )
            allocation.append(
                {"profile": item["service"], "devices": sorted(item["devices"])}
            )
        self.write_state(
            "allocation.json", normalise_allocation(allocation, self.rules)
        )

    def restart(self, runtime, profile, placement):
        name = self.names[runtime]
        item = self.inspect().get(name, {})
        if (
            not item.get("running")
            or item.get("service") != profile
            or item.get("devices") != set(placement.split(","))
        ):
            raise OperationError(
                "The live workload changed. Review refreshed status before restarting."
            )
        self.validate(profile, placement)
        self.stage("Restarting " + profile + " On GPU " + placement)
        self.engine.request("POST", "/containers/" + quote(name, safe="") + "/restart")
        self.wait_ready(name, profile, placement)

    def benchmark(self):
        options = self.options.get("benchmark")
        if not options:
            raise OperationError("Benchmarking has no supplied engine configuration.")
        current = self.inspect().get(self.names[self.text_runtime], {})
        if not current.get("running") or current.get("health") != "healthy":
            raise OperationError("Start a healthy text model before benchmarking.")
        request = Request(options["endpoint"].rstrip("/") + "/models")
        request.add_unredirected_header("Authorization", "Bearer " + options["api_key"])
        with urlopen(request, timeout=30) as response:
            model = json.load(response)["data"][0]["id"]
        if not isinstance(model, str) or not model:
            raise OperationError("The text endpoint returned no model.")
        spec = options["spec"]
        name = quote(spec["name"], safe="")
        body = self.container_config(spec)
        body["Cmd"] = [
            "run",
            "--categories",
            "prose",
            "--decode",
            "--endpoint",
            options["endpoint"],
            "--model",
            model,
            "--name",
            current["service"],
            "--no-update-check",
            "--quick",
        ]
        body.setdefault("HostConfig", {})["RestartPolicy"] = {"Name": "no"}
        existing = self.engine.request(
            "GET", "/containers/" + name + "/json", missing=True
        )
        if existing and existing["State"].get("Running"):
            raise OperationError("A benchmark is already running.")
        if existing:
            self.engine.request("DELETE", "/containers/" + name + "?v=true")
        self.stage("Running Text Benchmark")
        self.engine.request(
            "POST", "/containers/create?" + urlencode({"name": spec["name"]}), body
        )
        try:
            self.engine.request("POST", "/containers/" + name + "/start")
            deadline = time.monotonic() + 3600
            while True:
                item = self.engine.request("GET", "/containers/" + name + "/json")
                if not item["State"].get("Running"):
                    if item["State"].get("ExitCode", 1):
                        raise OperationError(
                            "The benchmark failed; inspect its saved results."
                        )
                    break
                if time.monotonic() > deadline:
                    raise OperationError("The benchmark exceeded its time limit.")
                time.sleep(1)
            self.stage("Text Benchmark Complete")
        finally:
            self.engine.request(
                "DELETE", "/containers/" + name + "?force=true&v=true", missing=True
            )

    def run(self, command):
        parameters = command if isinstance(command, dict) else {"action": command}
        action = parameters["action"]
        if action == "gpu:status":
            return json.dumps(self.status())
        if action == "check":
            self.engine.request("GET", "/_ping")
            self.status()
            return (
                "Engine connection, workload configuration and GPU status verified.\n"
            )
        self.state.mkdir(parents=True, exist_ok=True)
        with lock(self.state / ".allocation.lock"):
            if action == "gpu:apply":
                self.apply(parameters["allocation"])
            elif action == "text:bench":
                self.benchmark()
            elif action == "gpu:restart":
                self.restart(
                    parameters["runtime"],
                    parameters["profile"],
                    ",".join(parameters["devices"]),
                )
            else:
                raise ValueError("Unsupported control operation.")
        return ""


class SocketDispatcher:
    def __init__(self, config):
        self.config = config
        # Validate supplied definitions before starting the web interface.
        Operation(config)

    async def __call__(self, command, on_output=None):
        loop = asyncio.get_running_loop()
        output = []

        def progress(value):
            output.append(value)
            if on_output:
                loop.call_soon_threadsafe(on_output, value)

        def execute():
            try:
                result = Operation(self.config, progress).run(command)
                return 0, "".join(output) + result, ""
            except OperationError as error:
                return 1, "".join(output), str(error)
            except Exception as error:  # noqa: BLE001 - report any engine failure without leaking its details
                # Do not return engine response bodies or private configuration values.
                return (
                    1,
                    "".join(output),
                    "The engine operation failed ("
                    + type(error).__name__
                    + "). Review live status before retrying.",
                )

        return await asyncio.to_thread(execute)

    async def close(self):
        pass
