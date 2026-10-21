"""Reject either engine's worker image when its packaged core differs from metadata."""

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from deployment.check import check_comfyui_versions


class CoreVersionTests(unittest.TestCase):
    def setUp(self):
        self.image = "ghcr.io/maxexcloo/comfyui-radiance:1.0.1"
        self.services = {
            name: {"image": self.image} for name in ["comfyui-image", "comfyui-video"]
        }

    def test_shared_image_is_checked_once_without_gpu_access(self):
        for engine in ["docker", "podman"]:
            with (
                self.subTest(engine=engine),
                patch.dict(os.environ, AI_CONTAINER_ENGINE=engine),
            ):
                with patch(
                    "deployment.check.subprocess.check_output",
                    side_effect=["v0.42.1\n", "0.42.1\n"],
                ) as run:
                    check_comfyui_versions(Path("."), self.services)
                self.assertEqual(run.call_count, 2)
                inspection = run.call_args_list[0].args[0]
                self.assertEqual(
                    inspection[:4], [engine, "image", "inspect", "--format"]
                )
                self.assertIn(
                    ".Config.Labels" if engine == "docker" else ".Labels", inspection[4]
                )
                command = run.call_args.args[0]
                self.assertEqual(
                    command[:6],
                    [engine, "run", "--rm", "--entrypoint", "python3", self.image],
                )
                self.assertNotIn("--device", command)
                self.assertIn("/root/ComfyUI/comfyui_version.py", command[-1])

    def test_mismatched_packaged_core_blocks_deployment(self):
        for engine in ["docker", "podman"]:
            with (
                self.subTest(engine=engine),
                patch.dict(os.environ, AI_CONTAINER_ENGINE=engine),
                patch(
                    "deployment.check.subprocess.check_output",
                    side_effect=["0.42.1", "0.41.0"],
                ),
                self.assertRaisesRegex(
                    SystemExit, "packaged ComfyUI core 0.41.0 differs"
                ),
            ):
                check_comfyui_versions(Path("."), self.services)

    def test_missing_metadata_blocks_deployment(self):
        for engine in ["docker", "podman"]:
            with (
                self.subTest(engine=engine),
                patch.dict(os.environ, AI_CONTAINER_ENGINE=engine),
                patch("deployment.check.subprocess.check_output", return_value=""),
                self.assertRaisesRegex(
                    SystemExit, "Missing ComfyUI image version metadata"
                ),
            ):
                check_comfyui_versions(Path("."), self.services)

    def test_missing_or_unsupported_engine_fails_before_starting_commands(self):
        for engine in [None, "", "containerd"]:
            with self.subTest(engine=engine), patch.dict(os.environ):
                if engine is None:
                    os.environ.pop("AI_CONTAINER_ENGINE", None)
                else:
                    os.environ["AI_CONTAINER_ENGINE"] = engine
                with patch("deployment.check.subprocess.check_output") as run:
                    with self.assertRaises(ValueError) as error:
                        check_comfyui_versions(Path("."), self.services)
                    self.assertIn("AI_CONTAINER_ENGINE", str(error.exception))
                    run.assert_not_called()
