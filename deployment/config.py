"""Read the external configuration directory and write AI Control's configuration."""

import json
import os
import re
import secrets
import shlex
from pathlib import Path
from urllib.parse import urlparse

import yaml

# ${NAME} must be set; ${NAME:-default} falls back when unset or empty. $NAME is left for the container's shell.
VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def load_environment(root):
    """Read literal dotenv assignments without shell execution; explicit process values win."""
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
    for key in ["AI_DATA_DIR", "AI_MODELS_DIR", "CONTROL_ENGINE_SOCKET"]:
        if not os.environ.get(key) or not Path(os.environ[key]).is_absolute():
            raise ValueError(f"{key} must be an absolute path.")


def substitute(value, variables):
    if isinstance(value, dict):
        return {key: substitute(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item, variables) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match):
        name, default = match.groups()
        if default is not None:
            return variables.get(name) or default
        if name not in variables:
            raise ValueError(f"containers.yaml needs {name}.")
        return variables[name]

    return VARIABLE.sub(replace, value)


class Deployment:
    """workloads.json says what may run where; containers.yaml says how each container runs."""

    def __init__(self, root):
        self.root = root
        self.workloads = json.loads((root / "workloads.json").read_text())
        self.containers = yaml.safe_load((root / "containers.yaml").read_text())
        if set(self.containers["workloads"]) != set(self.workloads):
            raise ValueError(
                "containers.yaml and workloads.json must list the same workloads."
            )

    def spec(self, name):
        return substitute(self.containers[name], os.environ)

    def workload_spec(self, profile, devices):
        workload = self.workloads[profile]
        variables = {
            **os.environ,
            "GPU_DEVICES": devices,
            **workload.get("environment", {}).get(devices, {}),
        }
        spec = substitute(self.containers["workloads"][profile], variables)
        config = spec["config"]
        # Control identifies a workload and its placement from this label and these variables.
        config.setdefault("Labels", {})["io.excloo.ai-control.workload"] = profile
        config["Env"] = [
            *config.get("Env", []),
            "AI_GPU_DEVICES=" + devices,
            "HIP_VISIBLE_DEVICES=" + devices,
        ]
        return spec

    def all_specs(self):
        return [
            self.containers["control"],
            self.containers["benchmark"],
            *self.containers["workloads"].values(),
        ]

    def images(self):
        return sorted(
            {
                substitute(spec["config"]["Image"], os.environ)
                for spec in self.all_specs()
            }
        )

    def writable_directories(self):
        """Writable bind sources beneath the data and model directories."""
        roots = [Path(os.environ["AI_DATA_DIR"]), Path(os.environ["AI_MODELS_DIR"])]
        result = set()
        for spec in self.all_specs():
            for bind in spec["config"].get("HostConfig", {}).get("Binds", []):
                source, _, target = substitute(bind, os.environ).partition(":")
                path = Path(source)
                if "ro" not in target.partition(":")[2].split(",") and any(
                    path.is_relative_to(root) for root in roots
                ):
                    result.add(path)
        return sorted(result)

    def worker_images(self):
        return sorted(
            {
                substitute(
                    self.containers["workloads"][profile]["config"]["Image"], os.environ
                )
                for profile, workload in self.workloads.items()
                if workload["kind"] != "text"
            }
        )

    def rules(self):
        public = ["kind", "label", "model_label", "runtime"]
        return {
            "profiles": {
                "off": [],
                **{
                    profile: item["placements"]
                    for profile, item in self.workloads.items()
                },
            },
            # The first workload listed for a runtime names it.
            "runtime_names": {
                item["runtime"]: item["label"]
                for item in reversed(list(self.workloads.values()))
            },
            "workloads": {
                profile: {key: item[key] for key in public if key in item}
                for profile, item in self.workloads.items()
            },
        }

    def service_links(self):
        links = []
        for profile, workload in self.workloads.items():
            if "service" not in workload:
                continue
            service = workload["service"]
            link = {
                key: service[key] for key in ["display_label", "group", "title", "url"]
            }
            link.update(
                container=self.containers["workloads"][profile]["name"],
                target="_blank",
            )
            if service.get("radiance"):
                link["profiles"] = sorted(
                    name
                    for name, item in self.workloads.items()
                    if item["kind"] == "text"
                )
            links.append(link)
        return sorted(links, key=lambda link: link["title"].casefold())

    def engine(self):
        key = os.environ["RADIANCE_API_KEY"]
        workloads = {
            profile: {
                "placements": {
                    devices: self.workload_spec(profile, devices)
                    for devices in workload["placements"]
                }
            }
            for profile, workload in self.workloads.items()
        }
        return {
            "socket": "/run/engine.sock",
            "state_dir": "/state",
            "lease_dir": "/run/ai-gpu",
            "host_proc": "/host/proc",
            "host_sys": "/host/sys",
            "workloads": workloads,
            "benchmark": {
                "spec": self.spec("benchmark"),
                "api_key": key,
                "endpoint": http_url(os.environ["AI_HOST"], 8000) + "/v1",
            },
        }

    def write_control(self):
        """Retain existing owner accounts and write the resolved configuration Control reads."""
        output = Path(os.environ["AI_DATA_DIR"]) / "control/data/control.json"
        if not output.is_file():
            raise SystemExit(
                "Restore existing Control access settings before configuring the deployment."
            )
        users = json.loads(output.read_text())["users"]
        if not users:
            raise SystemExit(
                "No existing authorised local users; no public access was created."
            )
        self.ensure_keys()
        control = urlparse(os.environ["CONTROL_URL"])
        links = self.service_links()
        radiance = next(link for link in links if "profiles" in link)
        radiance_url, radiance["url"] = radiance["url"], "/radiance/"
        origins = {
            f"{control.scheme}://{control.netloc}",
            http_url(os.environ["AI_HOST"], int(os.environ["CONTROL_PORT"])),
        }
        origins.update(
            origin.strip()
            for origin in os.environ.get("CONTROL_ALLOWED_ORIGINS", "").split(",")
            if origin.strip()
        )
        catalogue = {"rules": self.rules(), "services": links}
        output.write_text(
            json.dumps(
                {
                    "engine": self.engine(),
                    "catalogue": catalogue,
                    "host": control.hostname,
                    "origins": sorted(origins),
                    "radiance_url": radiance_url,
                    "users": users,
                },
                indent=2,
            )
            + "\n"
        )
        output.chmod(0o600)
        print("AI Control configured; existing owner accounts retained.")
        for link in links:
            print(link["title"] + ": " + link["url"])

    def ensure_keys(self):
        """Add missing Control credentials to the private .env without replacing existing values."""
        path = self.root / ".env"
        text = path.read_text() if path.exists() else ""
        for key in ["CONTROL_API_KEY", "CONTROL_SESSION_KEY"]:
            if not os.environ.get(key):
                os.environ[key] = secrets.token_urlsafe(32)
                text += f"\n{key}={json.dumps(os.environ[key])}\n"
        path.write_text(text)
        path.chmod(0o600)


def bracket(host):
    host = host.strip("[]")
    return f"[{host}]" if ":" in host else host


def http_url(host, port):
    return f"http://{bracket(host)}:{port}"
