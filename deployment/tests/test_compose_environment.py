"""Inactive GPU services must interpolate without changing manual allocation."""

import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from deployment import runtime
from deployment.__main__ import main
from deployment.check import check_runtime


class ComposeEnvironmentTests(unittest.TestCase):
    def test_non_gpu_environment_uses_owned_text_identity_without_mutating_process(
        self,
    ):
        workloads = SimpleNamespace(CONTAINERS={"text": "owned-text"})
        with (
            patch.dict(
                os.environ,
                {"GPU_CONTAINER": "stale-image", "EXPLICIT_SETTING": "keep"},
                clear=True,
            ),
            patch.dict("sys.modules", {"deployment.workloads": workloads}),
        ):
            environment = runtime.compose_environment()
            self.assertEqual(environment["GPU_CONTAINER"], "owned-text")
            self.assertEqual(environment["EXPLICIT_SETTING"], "keep")
            self.assertEqual(os.environ["GPU_CONTAINER"], "stale-image")

    def test_runtime_check_renders_each_supported_placement_and_non_gpu_profile(self):
        workloads = SimpleNamespace(
            CONTAINERS={"text": "owned-text", "image": "owned-image"},
            PROFILES={
                "text-profile": ("text", "TEXT_DEVICES"),
                "image-profile": ("image", "IMAGE_DEVICES"),
            },
            WORKLOADS={
                "text-profile": {
                    "placements": ["0", "0,1"],
                    "environment": {
                        "0": {"RADIANCE_TP": "1"},
                        "0,1": {"RADIANCE_TP": "2"},
                    },
                },
                "image-profile": {"placements": ["1"]},
            },
        )
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            (root / "compose.yaml").write_text(
                "services:\n  text:\n    profiles: [text-profile]\n  image:\n    profiles: [image-profile]\n  control:\n    profiles: [control]\n"
            )
            stack.enter_context(patch("deployment.check.root", root))
            stack.enter_context(
                patch.dict(
                    os.environ, AI_DATA_DIR="/state", AI_CONTAINER_ENGINE="docker"
                )
            )
            stack.enter_context(
                patch.dict("sys.modules", {"deployment.workloads": workloads})
            )
            stack.enter_context(patch("deployment.check.check_comfyui_versions"))
            stack.enter_context(patch("deployment.studio_worker.verify"))
            run = stack.enter_context(patch("deployment.check.subprocess.run"))
            check_runtime()
            environments = [call.kwargs["env"] for call in run.call_args_list]
            self.assertEqual(len(environments), 4)
            self.assertEqual(environments[0]["GPU_CONTAINER"], "owned-text")
            self.assertEqual(environments[1]["GPU_CONTAINER"], "owned-image")
            self.assertEqual(environments[1]["IMAGE_DEVICES"], "1")
            self.assertEqual(
                [env["TEXT_DEVICES"] for env in environments[2:]], ["0", "0,1"]
            )
            self.assertEqual(
                [env["RADIANCE_TP"] for env in environments[2:]], ["1", "2"]
            )
            self.assertTrue(
                all(env["GPU_CONTAINER"] == "owned-text" for env in environments[2:])
            )
            self.assertTrue(all(call.kwargs["check"] for call in run.call_args_list))

    def test_boot_and_pull_supply_interpolation_without_starting_gpu_workloads(self):
        for command in ["boot", "pull"]:
            with self.subTest(command=command), ExitStack() as stack:
                stack.enter_context(
                    patch("sys.argv", ["deployment", "--root", ".", command])
                )
                stack.enter_context(patch("deployment.__main__.load_environment"))
                stack.enter_context(patch("deployment.gpu.restore_allocation"))
                environment = {"GPU_CONTAINER": "owned-text"}
                stack.enter_context(
                    patch(
                        "deployment.runtime.compose_environment",
                        return_value=environment,
                    )
                )
                run = stack.enter_context(patch("deployment.__main__.subprocess.run"))
                main()
                self.assertIs(run.call_args.kwargs["env"], environment)
                arguments = run.call_args.args[0]
                self.assertNotIn("text-profile", arguments)
                self.assertNotIn("image-profile", arguments)
