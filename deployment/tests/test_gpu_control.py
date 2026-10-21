"""Verify the Mise client delegates mutations to authenticated AI Control APIs."""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from deployment import gpu


def completed():
    return {"id": "test-job", "state": "completed", "output": ""}


class GPUClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        directory = patch.dict(
            os.environ,
            AI_DATA_DIR=str(self.root),
            AI_BOOT_ALLOCATION='[{"profile":"radiance-flashnext","devices":["0","1"]}]',
        )
        directory.start()
        self.addCleanup(directory.stop)

    def command(self, *arguments):
        with patch("sys.argv", ["gpu", *arguments]):
            gpu.main()

    def test_start_is_conservative_and_select_is_explicit_replacement(self):
        for command, replace in [("start", False), ("select", True)]:
            for profile, expected in [
                ("gemma-31b", {"kind": "text", "model": "gemma-31b"}),
                ("comfyui-image", {"kind": "media", "mode": "image"}),
                ("comfyui-video", {"kind": "video", "mode": "on"}),
            ]:
                with (
                    self.subTest(command=command, profile=profile),
                    patch.object(gpu, "submit") as submit,
                ):
                    self.command(command, profile, "0")
                    submit.assert_called_once_with(
                        "selections", dict(expected, gpus="0", replace=replace)
                    )

    def test_board_previews_and_applies_only_the_returned_signature(self):
        allocation = [{"profile": "gemma-31b", "devices": ["0"]}]
        with patch.object(
            gpu, "api", side_effect=[{"signature": "reviewed"}, completed()]
        ) as request:
            self.command("board", json.dumps(allocation).encode().hex())
        self.assertEqual(
            request.call_args_list[0].args, ("board/plan", {"allocation": allocation})
        )
        self.assertEqual(
            request.call_args_list[1].args,
            (
                "board",
                {"allocation": allocation, "signature": "reviewed", "replace": True},
            ),
        )

    def test_invalid_board_never_reaches_the_api(self):
        for value in [
            "not-hex",
            json.dumps([{"profile": "comfyui-image", "devices": ["0", "1"]}])
            .encode()
            .hex(),
        ]:
            with self.subTest(value=value), patch.object(gpu, "api") as request:
                with self.assertRaises(ValueError):
                    self.command("board", value)
                request.assert_not_called()

    def test_stop_runtime_uses_scoped_selection_not_a_complete_board(self):
        for runtime, payload in [
            ("text", {"kind": "text", "model": "off"}),
            ("image", {"kind": "media", "mode": "off", "runtime": "image"}),
            ("comfyui-video", {"kind": "video", "mode": "off"}),
        ]:
            with self.subTest(runtime=runtime), patch.object(gpu, "submit") as submit:
                self.command("stop", runtime)
                submit.assert_called_once_with(
                    "selections", dict(payload, replace=True)
                )
        with patch.object(gpu, "apply_board") as board:
            self.command("stop", "all")
            board.assert_called_once_with([])

    def test_unknown_stop_target_never_reaches_the_api(self):
        with (
            patch.object(gpu, "submit") as submit,
            patch.object(gpu, "apply_board") as board,
        ):
            with self.assertRaisesRegex(ValueError, "known runtime"):
                gpu.stop("unknown")
            submit.assert_not_called()
            board.assert_not_called()

    def test_media_and_benchmark_keep_their_cli_interfaces(self):
        with patch.object(gpu, "submit") as submit:
            self.command("media", "pair", "0")
            submit.assert_called_once_with(
                "selections",
                {"kind": "media", "mode": "pair", "gpus": "0", "replace": True},
            )
        with patch.object(gpu, "submit") as submit:
            self.command("bench")
            submit.assert_called_once_with("maintenance", {"action": "benchmark"})

    def test_restart_preserves_the_reviewed_profile_and_devices(self):
        status = {
            "runtimes": {
                "text": {"running": True, "profile": "gemma-31b", "devices": ["1"]}
            }
        }
        with (
            patch.object(gpu, "status", return_value=status),
            patch.object(gpu, "submit") as submit,
        ):
            self.command("restart", "text")
            submit.assert_called_once_with(
                "restart", {"runtime": "text", "profile": "gemma-31b", "devices": ["1"]}
            )
        with (
            patch.object(gpu, "status", return_value=status),
            patch.object(gpu, "submit") as submit,
        ):
            with self.assertRaisesRegex(RuntimeError, "live workload changed"):
                self.command(
                    "restart",
                    "text",
                    "--profile",
                    "radiance-flashnext",
                    "--gpus",
                    "0,1",
                )
            submit.assert_not_called()

    def test_jobs_are_polled_without_repeating_the_mutation(self):
        updates = [
            {
                "id": "test-job",
                "state": "running",
                "stage": "Loading",
                "output": "First\n",
            },
            {
                "id": "test-job",
                "state": "running",
                "stage": "Loading",
                "output": "First\nSecond\n",
            },
            {
                "id": "test-job",
                "state": "completed",
                "stage": "Ready",
                "output": "First\nSecond\n",
            },
        ]
        output = io.StringIO()
        with (
            patch.object(gpu, "api", side_effect=updates) as request,
            patch.object(gpu.time, "sleep"),
            redirect_stdout(output),
        ):
            gpu.submit("maintenance", {"action": "benchmark"})
        self.assertEqual(
            [call.args[0] for call in request.call_args_list],
            ["maintenance", "jobs/test-job", "jobs/test-job"],
        )
        self.assertEqual(output.getvalue().count("First\n"), 1)
        self.assertEqual(output.getvalue().count("Second\n"), 1)

    def test_failed_or_timed_out_jobs_are_never_resubmitted(self):
        with patch.object(gpu, "api") as request:
            with self.assertRaisesRegex(RuntimeError, "operation failed"):
                gpu.wait_for_job({"id": "job", "state": "failed"})
            request.assert_not_called()
        with patch.object(gpu, "api") as request:
            with self.assertRaisesRegex(RuntimeError, "no request was repeated"):
                gpu.wait_for_job({"id": "job", "state": "running"}, timeout=0)
            request.assert_not_called()

    def test_restore_empty_saved_allocation_does_not_start_the_boot_default(self):
        path = self.root / "control/data/allocation.json"
        path.parent.mkdir(parents=True)
        path.write_text("[]")
        with (
            patch.object(gpu, "apply_board") as board,
            patch.object(gpu, "status") as status,
        ):
            self.command("restore")
            board.assert_called_once_with([])
            status.assert_not_called()

    def test_restore_without_saved_state_preserves_running_workloads(self):
        with (
            patch.object(
                gpu, "status", return_value={"runtimes": {"text": {"running": True}}}
            ),
            patch.object(gpu, "apply_board") as board,
            redirect_stdout(io.StringIO()),
        ):
            self.command("restore")
            board.assert_not_called()
        with (
            patch.object(gpu, "status", return_value={"runtimes": {}}),
            patch.object(gpu, "apply_board") as board,
        ):
            self.command("restore")
            board.assert_called_once_with(
                [{"profile": "radiance-flashnext", "devices": ["0", "1"]}]
            )

    def test_remember_never_overwrites_an_existing_saved_allocation(self):
        path = self.root / "control/data/allocation.json"
        path.parent.mkdir(parents=True)
        path.write_text("[]")
        with patch.object(gpu, "status") as status, redirect_stdout(io.StringIO()):
            self.command("remember")
            status.assert_not_called()
        self.assertEqual(path.read_text(), "[]")

    def test_only_manual_logs_use_a_selected_engine_cli(self):
        for engine in ["docker", "podman"]:
            with (
                self.subTest(engine=engine),
                patch.dict(os.environ, AI_CONTAINER_ENGINE=engine),
                patch.object(gpu.subprocess, "run") as run,
            ):
                self.command("logs", "text")
                run.assert_called_once_with(
                    [engine, "logs", "--follow", gpu.CONTAINERS["text"]], check=True
                )

    def test_json_status_is_the_authenticated_api_result(self):
        output = io.StringIO()
        expected = {"runtimes": {}, "gpu0": "Available", "gpu1": "Available"}
        with (
            patch.object(gpu, "api", return_value=expected) as request,
            redirect_stdout(output),
        ):
            self.command("status", "--json")
        request.assert_called_once_with("status")
        self.assertEqual(json.loads(output.getvalue()), expected)


class HTTPClientTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(
            os.environ,
            AI_HOST="compute.example.invalid",
            CONTROL_PORT="1337",
            CONTROL_API_KEY="synthetic-api-key",
        )
        environment.start()
        self.addCleanup(environment.stop)

    def test_request_uses_configured_endpoint_and_unredirected_bearer_header(self):
        response = MagicMock()
        response.__enter__.return_value = io.BytesIO(b'{"ok":true}')
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(gpu, "build_opener", return_value=opener):
            self.assertEqual(gpu.api("board/plan", {"allocation": []}), {"ok": True})
        request = opener.open.call_args.args[0]
        self.assertEqual(
            request.full_url, "http://compute.example.invalid:1337/api/board/plan"
        )
        self.assertEqual(
            request.unredirected_hdrs["Authorization"], "Bearer synthetic-api-key"
        )
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(json.loads(request.data), {"allocation": []})
        self.assertIsNone(
            gpu.NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://other.invalid"
            )
        )

    def test_http_conflict_is_reported_without_retry_or_credential_output(self):
        body = io.BytesIO(b'{"error":"Conflict synthetic-api-key"}')
        failure = HTTPError("http://compute.example.invalid", 409, "Conflict", {}, body)
        opener = MagicMock()
        opener.open.side_effect = failure
        with patch.object(gpu, "build_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "HTTP 409") as error:
                gpu.api("selections", {"replace": False})
        self.assertNotIn("synthetic-api-key", str(error.exception))
        self.assertIn("[redacted]", str(error.exception))
        self.assertEqual(opener.open.call_count, 1)

    def test_invalid_required_settings_fail_before_a_request(self):
        for key, value in [
            ("AI_HOST", ""),
            ("CONTROL_PORT", "not-a-port"),
            ("CONTROL_PORT", "0"),
            ("CONTROL_API_KEY", ""),
        ]:
            with (
                self.subTest(key=key, value=value),
                patch.dict(os.environ, {key: value}),
                patch.object(gpu, "build_opener") as opener,
            ):
                with self.assertRaisesRegex(ValueError, key):
                    gpu.api("status")
                opener.assert_not_called()
