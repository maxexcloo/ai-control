"""Minimal client for the local Docker-compatible engine socket."""

import http.client
import io
import json
import socket
import tarfile
import time
from urllib.parse import quote, urlencode


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class Engine:
    def __init__(self, path):
        self.path = path

    def request(self, method, path, body=None, missing=False, timeout=130):
        connection = UnixConnection(self.path, timeout)
        try:
            data = json.dumps(body).encode() if body is not None else None
            connection.request(method, path, data, {"Content-Type": "application/json"})
            response = connection.getresponse()
            content = response.read()
            if missing and response.status == 404:
                return None
            if response.status >= 300:
                # Engine error bodies can include supplied environment values.
                raise RuntimeError(
                    f"Engine {method} {path.split('?')[0]} failed ({response.status})."
                )
            return content
        finally:
            connection.close()

    def inspect(self, kind, name):
        content = self.request(
            "GET", f"/{kind}/{quote(name, safe='')}/json", missing=True
        )
        return json.loads(content) if content is not None else None

    def pull(self, image):
        name, _, digest = image.partition("@")
        repository, tag = (
            name.rsplit(":", 1) if ":" in name.rsplit("/", 1)[-1] else (name, "latest")
        )
        query = urlencode({"fromImage": repository, "tag": digest or tag})
        # A failed pull still answers 200; its progress stream carries the error.
        for line in self.request(
            "POST", "/images/create?" + query, timeout=None
        ).splitlines():
            if line.strip() and "error" in (message := json.loads(line)):
                raise RuntimeError(f"Pulling {image} failed: {message['error']}")

    def replace(self, spec):
        """Remove any existing container with the spec's name, then create and start it."""
        name = quote(spec["name"], safe="")
        self.request("DELETE", f"/containers/{name}?force=true&v=true", missing=True)
        self.request(
            "POST",
            "/containers/create?" + urlencode({"name": spec["name"]}),
            spec["config"],
        )
        self.request("POST", f"/containers/{name}/start")

    def wait_healthy(self, name, timeout=60):
        deadline = time.monotonic() + timeout
        while (state := self.inspect("containers", name)["State"]).get(
            "Health", {}
        ).get("Status") != "healthy":
            if not state.get("Running") or time.monotonic() > deadline:
                raise RuntimeError(
                    f"{name} did not become healthy; inspect its container logs."
                )
            time.sleep(1)

    def read_file(self, image, path):
        container = json.loads(
            self.request(
                "POST", "/containers/create", {"Image": image, "Cmd": ["true"]}
            )
        )["Id"]
        try:
            archive = self.request(
                "GET", f"/containers/{container}/archive?" + urlencode({"path": path})
            )
        finally:
            self.request(
                "DELETE", f"/containers/{container}?force=true&v=true", missing=True
            )
        with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
            return stream.extractfile(stream.getmembers()[0]).read()
