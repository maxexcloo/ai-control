"""Exercise the engine client against simulated socket responses."""

import io
import json
import tarfile
import unittest
from unittest.mock import patch

from deployment.engine import Engine


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine("/fixture.sock")
        self.calls = []
        self.responses = {}

        def request(method, path, body=None, missing=False, timeout=130):
            self.calls.append((method, path, body))
            return self.responses.get((method, path.split("?")[0]), b"{}")

        patcher = patch.object(self.engine, "request", side_effect=request)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pull_splits_tags_and_digests(self):
        for image, query in [
            (
                "registry.invalid:5000/team/app:1.2",
                "fromImage=registry.invalid%3A5000%2Fteam%2Fapp&tag=1.2",
            ),
            ("registry.invalid/app", "fromImage=registry.invalid%2Fapp&tag=latest"),
            (
                "registry.invalid/app:latest@sha256:abc",
                "fromImage=registry.invalid%2Fapp&tag=sha256%3Aabc",
            ),
        ]:
            self.engine.pull(image)
            self.assertEqual(self.calls[-1][1], "/images/create?" + query)

    def test_pull_reports_errors_from_the_progress_stream(self):
        self.responses[("POST", "/images/create")] = (
            b'{"status":"Pulling"}\n{"error":"manifest unknown"}\n'
        )
        with self.assertRaisesRegex(RuntimeError, "manifest unknown"):
            self.engine.pull("registry.invalid/app:1")

    def test_replace_removes_creates_and_starts(self):
        self.engine.replace({"name": "ai-control", "config": {"Image": "fixture"}})
        self.assertEqual(
            [(method, path) for method, path, _ in self.calls],
            [
                ("DELETE", "/containers/ai-control?force=true&v=true"),
                ("POST", "/containers/create?name=ai-control"),
                ("POST", "/containers/ai-control/start"),
            ],
        )

    def test_read_file_extracts_and_removes_the_container(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as stream:
            info = tarfile.TarInfo("busybox")
            info.size = 4
            stream.addfile(info, io.BytesIO(b"\x7fELF"))
        self.responses[("POST", "/containers/create")] = json.dumps(
            {"Id": "abc"}
        ).encode()
        self.responses[("GET", "/containers/abc/archive")] = archive.getvalue()
        self.assertEqual(self.engine.read_file("busybox", "/bin/busybox"), b"\x7fELF")
        self.assertEqual(
            self.calls[-1][:2], ("DELETE", "/containers/abc?force=true&v=true")
        )


if __name__ == "__main__":
    unittest.main()
