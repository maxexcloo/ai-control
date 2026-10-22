"""Verify the CLI delegates every change to AI Control's board API."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deployment import gpu

COMPLETED = {"id": "job", "state": "completed", "output": ""}


class GPUClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(
            os.environ,
            AI_DATA_DIR=str(self.root),
            CONTROL_API_KEY="key",
            CONTROL_PORT="1337",
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.status = {
            "runtimes": {
                "text": {"running": True, "profile": "single-text", "devices": ["0"]},
                "image": {
                    "running": True,
                    "profile": "comfyui-image",
                    "devices": ["1"],
                },
                "video": {"running": False, "profile": "", "devices": []},
            }
        }
        self.requests = []

        def api(path, payload=None):
            self.requests.append((path, payload))
            if path == "status":
                return self.status
            if path == "board/plan":
                return {"signature": "fixture"}
            return COMPLETED

        patcher = patch.object(gpu, "api", side_effect=api)
        patcher.start()
        self.addCleanup(patcher.stop)

    def boards(self):
        return [payload for path, payload in self.requests if path == "board"]

    def test_apply_previews_then_submits_the_signed_board(self):
        gpu.main(["apply", "comfyui-image=0", "single-text=1"])
        self.assertEqual(
            self.requests[0],
            (
                "board/plan",
                {
                    "allocation": [
                        {"profile": "comfyui-image", "devices": ["0"]},
                        {"profile": "single-text", "devices": ["1"]},
                    ]
                },
            ),
        )
        self.assertEqual(self.boards()[0]["signature"], "fixture")
        self.assertTrue(self.boards()[0]["replace"])

    def test_stop_keeps_the_other_running_workloads(self):
        gpu.main(["stop", "text"])
        self.assertEqual(
            self.boards()[0]["allocation"],
            [{"profile": "comfyui-image", "devices": ["1"]}],
        )
        gpu.main(["stop", "all"])
        self.assertEqual(self.boards()[1]["allocation"], [])
        with self.assertRaisesRegex(ValueError, "not running"):
            gpu.main(["stop", "video"])

    def test_restart_sends_the_live_placement(self):
        gpu.main(["restart", "image"])
        self.assertIn(
            (
                "restart",
                {"runtime": "image", "profile": "comfyui-image", "devices": ["1"]},
            ),
            self.requests,
        )

    def test_restore_prefers_saved_then_running_then_boot_allocation(self):
        saved = self.root / "control/data/allocation.json"
        saved.parent.mkdir(parents=True)
        saved.write_text(
            json.dumps([{"profile": "single-text", "devices": ["0", "1"]}])
        )
        gpu.restore_allocation()
        self.assertEqual(
            self.boards()[-1]["allocation"],
            [{"profile": "single-text", "devices": ["0", "1"]}],
        )
        saved.unlink()
        gpu.restore_allocation()
        self.assertEqual(len(self.boards()), 1)
        self.status = {"runtimes": {}}
        with patch.dict(
            os.environ,
            AI_BOOT_ALLOCATION='[{"profile":"comfyui-image","devices":["1"]}]',
        ):
            gpu.restore_allocation()
        self.assertEqual(
            self.boards()[-1]["allocation"],
            [{"profile": "comfyui-image", "devices": ["1"]}],
        )

    def test_failed_job_is_reported(self):
        with self.assertRaisesRegex(RuntimeError, "failed"):
            gpu.wait_for_job({"id": "job", "state": "failed", "output": ""})


if __name__ == "__main__":
    unittest.main()
