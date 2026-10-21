"""Engine selection must never fall through to another daemon or its Compose files."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deployment import runtime


class RuntimeTests(unittest.TestCase):
    def test_docker_ignores_inherited_podman_compose_files(self):
        with (
            patch.dict(
                os.environ, AI_CONTAINER_ENGINE="docker", COMPOSE_FILE="wrong.yaml"
            ),
            patch.object(runtime, "ROOT", Path("/fixture")),
        ):
            self.assertEqual(
                runtime.compose("config"),
                ["docker", "compose", "--file", "/fixture/compose.yaml", "config"],
            )
            self.assertEqual(
                runtime.command("inspect", "fixture"), ["docker", "inspect", "fixture"]
            )

    def test_podman_adds_its_overlay_after_the_base(self):
        with (
            patch.dict(os.environ, AI_CONTAINER_ENGINE="podman"),
            patch.object(runtime, "ROOT", Path("/fixture")),
        ):
            self.assertEqual(
                runtime.compose("config"),
                [
                    "podman-compose",
                    "--file",
                    "/fixture/compose.yaml",
                    "--file",
                    "/fixture/compose.podman.yaml",
                    "config",
                ],
            )

    def test_invalid_or_missing_engine_never_chooses_a_daemon(self):
        for value in [None, "", "auto", "Docker", "docker;true"]:
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ["AI_CONTAINER_ENGINE"] = value
                for operation in [runtime.command, runtime.compose]:
                    with self.assertRaisesRegex(ValueError, "AI_CONTAINER_ENGINE"):
                        operation("ps")

    def test_explicit_socket_requires_an_absolute_path_without_daemon_discovery(self):
        with (
            patch.dict(
                os.environ,
                AI_CONTAINER_ENGINE="docker",
                CONTROL_ENGINE_SOCKET="/fixture/engine.sock",
            ),
            patch.object(runtime.subprocess, "run") as run,
        ):
            self.assertEqual(runtime.socket_path(), "/fixture/engine.sock")
            run.assert_not_called()
        for value in ["", "relative.sock"]:
            with (
                patch.dict(
                    os.environ,
                    AI_CONTAINER_ENGINE="podman",
                    CONTROL_ENGINE_SOCKET=value,
                ),
                self.assertRaisesRegex(ValueError, "CONTROL_ENGINE_SOCKET"),
            ):
                runtime.socket_path()

    def test_podman_socket_uses_runtime_directory_or_current_uid(self):
        with patch.dict(
            os.environ,
            {"AI_CONTAINER_ENGINE": "podman", "XDG_RUNTIME_DIR": "/fixture/runtime"},
            clear=True,
        ):
            self.assertEqual(
                runtime.socket_path(), "/fixture/runtime/podman/podman.sock"
            )
        with (
            patch.dict(os.environ, {"AI_CONTAINER_ENGINE": "podman"}, clear=True),
            patch.object(runtime.os, "getuid", return_value=1234),
        ):
            self.assertEqual(runtime.socket_path(), "/run/user/1234/podman/podman.sock")

    def test_docker_socket_uses_explicit_unix_host_or_current_context(self):
        with (
            patch.dict(
                os.environ,
                {
                    "AI_CONTAINER_ENGINE": "docker",
                    "DOCKER_HOST": "unix:///fixture/docker.sock",
                },
                clear=True,
            ),
            patch.object(runtime.subprocess, "run") as run,
        ):
            self.assertEqual(runtime.socket_path(), "/fixture/docker.sock")
            run.assert_not_called()
        context = [{"Endpoints": {"docker": {"Host": "unix:///fixture/desktop.sock"}}}]
        with (
            patch.dict(os.environ, {"AI_CONTAINER_ENGINE": "docker"}, clear=True),
            patch.object(
                runtime.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 0, stdout=json.dumps(context)
                ),
            ) as run,
        ):
            self.assertEqual(runtime.socket_path(), "/fixture/desktop.sock")
            self.assertEqual(run.call_args.args[0], ["docker", "context", "inspect"])
            self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_docker_remote_or_invalid_endpoints_fail_without_context_fallback(self):
        for endpoint in [
            "tcp://fixture:2375",
            "ssh://fixture",
            "unix://remote/engine.sock",
            "unix:relative.sock",
            "",
        ]:
            with (
                self.subTest(endpoint=endpoint),
                patch.dict(
                    os.environ,
                    {"AI_CONTAINER_ENGINE": "docker", "DOCKER_HOST": endpoint},
                    clear=True,
                ),
                patch.object(runtime.subprocess, "run") as run,
                self.assertRaisesRegex(ValueError, "local unix socket"),
            ):
                runtime.socket_path()
            run.assert_not_called()
