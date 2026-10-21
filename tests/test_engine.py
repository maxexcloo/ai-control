"""Exercise socket allocation safety and Docker-compatible request handling."""

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import unquote, urlsplit

from fixtures import synthetic_catalogue, synthetic_engine

from control.engine import Engine, Operation, lock


class FixtureEngine:
    def __init__(self):
        self.items = {}
        self.calls = []
        self.missing_images = False
        self.image_health = {"Test": ["CMD", "true"]}
        self.benchmark_exit = 0
        self.created = {}

    def request(self, method, path, body=None, **options):
        self.calls.append((method, path))
        target = urlsplit(path)
        if target.path.startswith("/images/"):
            return (
                None
                if self.missing_images
                else {
                    "Id": "fixture-image",
                    "Config": {"Healthcheck": self.image_health},
                }
            )
        if target.path == "/containers/create":
            from urllib.parse import parse_qs

            name = parse_qs(target.query)["name"][0]
            self.items[name] = {
                "Config": copy.deepcopy(body),
                "State": {"Running": False, "Health": {"Status": "healthy"}},
            }
            self.created[name] = copy.deepcopy(body)
            return {"Id": name}
        parts = unquote(target.path).split("/")[2:]
        name, action = parts[0], parts[1] if len(parts) > 1 else ""
        if method == "GET":
            return copy.deepcopy(self.items.get(name))
        if method == "DELETE":
            self.items.pop(name)
        elif action in {"start", "restart"}:
            self.items[name]["State"]["Running"] = True
            if name == "fixture-benchmark":
                self.items[name]["State"].update(
                    Running=False, ExitCode=self.benchmark_exit
                )
        elif action == "stop":
            self.items[name]["State"]["Running"] = False
        return {}

    def mutations(self):
        return [(method, path) for method, path in self.calls if method != "GET"]


class EngineOperationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.config = {"catalogue": synthetic_catalogue(), "engine": synthetic_engine()}
        self.config["engine"].update(
            state_dir=str(root / "state"),
            lease_dir=str(root / "leases"),
            host_proc=str(root / "proc"),
            host_sys=str(root / "sys"),
        )
        self.leases = root / "leases"
        self.leases.mkdir()
        for device in ["0", "1"]:
            (self.leases / f"gpu{device}.lock").touch()
        for profile, definition in self.config["engine"]["workloads"].items():
            for spec in definition["placements"].values():
                spec["config"]["Labels"] = {"com.docker.compose.service": profile}
        self.operation = Operation(self.config)
        self.engine = FixtureEngine()
        self.operation.engine = self.engine

    def active(self, profile, placement):
        spec = self.operation.spec(profile, placement)
        self.engine.items[spec["name"]] = {
            "Config": copy.deepcopy(spec["config"]),
            "State": {"Running": True, "Health": {"Status": "healthy"}},
        }
        return spec["name"]

    def test_invalid_or_shared_allocation_fails_before_engine_requests(self):
        cases = [
            [{"profile": "unknown", "devices": ["0"]}],
            [
                {"profile": "render-model", "devices": ["0"]},
                {"profile": "single-text", "devices": ["0"]},
            ],
            [{"profile": "render-model", "devices": ["0", "1"]}],
        ]
        for allocation in cases:
            with self.subTest(allocation=allocation):
                with self.assertRaises(ValueError):
                    self.operation.apply(allocation)
                self.assertEqual(self.engine.calls, [])

    def test_unknown_lease_owner_aborts_before_stopping_existing_work(self):
        self.active("render-model", "0")
        previous = copy.deepcopy(self.engine.items)
        with lock(self.leases / "gpu1.lock"):
            with self.assertRaisesRegex(RuntimeError, "unknown reservation"):
                self.operation.apply([{"profile": "single-text", "devices": ["1"]}])
        self.assertEqual(self.engine.items, previous)
        self.assertEqual(self.engine.mutations(), [])

    def test_unknown_managed_placement_preserves_every_container(self):
        name = self.active("single-text", "1")
        self.engine.items[name]["Config"]["Env"] = ["AI_GPU_DEVICES=invalid"]
        previous = copy.deepcopy(self.engine.items)
        with self.assertRaisesRegex(RuntimeError, "unknown GPU placement"):
            self.operation.apply([{"profile": "render-model", "devices": ["0"]}])
        self.assertEqual(self.engine.items, previous)
        self.assertEqual(self.engine.mutations(), [])

    def test_partial_selection_retains_disjoint_gpu_workload(self):
        name = self.active("render-model", "0")
        previous = copy.deepcopy(self.engine.items[name])
        self.operation.apply(
            [{"profile": "single-text", "devices": ["1"]}], complete=False
        )
        self.assertEqual(self.engine.items[name], previous)
        self.assertFalse(
            any(
                path.startswith("/containers/" + name + "/")
                for method, path in self.engine.mutations()
            )
        )
        self.assertTrue(self.engine.items["fixture-text"]["State"]["Running"])

    def test_unchanged_digest_keeps_healthy_container_without_recreation(self):
        name = self.active("render-model", "0")
        self.operation.write_state(
            "config-digests.json", {name: self.operation.digest("render-model", "0")}
        )
        self.operation.apply([{"profile": "render-model", "devices": ["0"]}])
        self.assertEqual(self.engine.mutations(), [])
        self.assertEqual(
            json.loads((self.operation.state / "allocation.json").read_text()),
            [{"profile": "render-model", "devices": ["0"]}],
        )

    def test_status_reports_configuration_freshness(self):
        name = self.active("render-model", "0")
        self.operation.write_state(
            "config-digests.json", {name: self.operation.digest("render-model", "0")}
        )
        self.assertTrue(
            self.operation.status()["runtimes"]["render-pool"]["config_current"]
        )
        self.operation.spec("render-model", "0")["config"]["Image"] = (
            "fixture.invalid/updated:2"
        )
        self.assertFalse(
            self.operation.status()["runtimes"]["render-pool"]["config_current"]
        )

    def test_missing_or_disabled_healthcheck_preserves_running_workloads(self):
        self.active("render-model", "0")
        for health in [{}, {"Test": ["NONE"]}]:
            self.operation.spec("single-text", "1")["config"]["Healthcheck"] = health
            self.engine.image_health = {"Test": ["CMD", "true"]} if health else {}
            with self.assertRaisesRegex(RuntimeError, "health check"):
                self.operation.apply([{"profile": "single-text", "devices": ["1"]}])
            self.assertEqual(self.engine.mutations(), [])

    def test_invalid_identity_is_rejected_at_startup(self):
        for key, value in [
            ("Env", ["AI_GPU_DEVICES=1"]),
            ("Labels", {"com.docker.compose.service": "other"}),
        ]:
            configuration = copy.deepcopy(self.config)
            configuration["engine"]["workloads"]["render-model"]["placements"]["0"][
                "config"
            ][key] = value
            with self.assertRaisesRegex(ValueError, "identity and GPU placement"):
                Operation(configuration)

    def test_restart_keeps_profile_and_placement(self):
        name = self.active("motion-model", "1")
        previous = copy.deepcopy(self.engine.items[name])
        self.operation.restart("motion-pool", "motion-model", "1")
        self.assertEqual(
            self.engine.mutations(), [("POST", "/containers/" + name + "/restart")]
        )
        self.assertEqual(self.engine.items[name], previous)

    def test_changed_restart_placement_is_rejected_without_mutation(self):
        self.active("motion-model", "1")
        with self.assertRaisesRegex(RuntimeError, "live workload changed"):
            self.operation.restart("motion-pool", "motion-model", "0")
        self.assertEqual(self.engine.mutations(), [])

    def test_stopping_video_uses_catalogue_runtime_identity(self):
        image = self.active("render-model", "0")
        video = self.active("motion-model", "1")
        self.operation.run("video:stop")
        self.assertEqual(
            self.engine.mutations(), [("POST", "/containers/" + video + "/stop")]
        )
        self.assertTrue(self.engine.items[image]["State"]["Running"])

    def test_keep_groups_becomes_oci_annotation_without_editing_source_spec(self):
        spec = copy.deepcopy(self.operation.spec("render-model", "0"))
        spec["config"]["HostConfig"] = {
            "GroupAdd": ["keep-groups"],
            "UsernsMode": "keep-id",
            "IpcMode": "host",
            "Annotations": {"fixture": "retained"},
        }
        original = copy.deepcopy(spec)
        host = self.operation.container_config(spec)["HostConfig"]
        self.assertNotIn("GroupAdd", host)
        self.assertEqual(
            host["Annotations"],
            {"fixture": "retained", "run.oci.keep_original_groups": "1"},
        )
        self.assertEqual(host["UsernsMode"], "keep-id")
        self.assertEqual(spec, original)

    def test_invalid_group_combination_fails_before_engine_mutations(self):
        self.operation.spec("render-model", "0")["config"]["HostConfig"] = {
            "GroupAdd": ["keep-groups", "render"]
        }
        with self.assertRaises(ValueError):
            self.operation.apply([{"profile": "render-model", "devices": ["0"]}])
        self.assertEqual(self.engine.calls, [])

    def test_missing_target_image_preserves_running_workloads(self):
        self.active("render-model", "0")
        previous = copy.deepcopy(self.engine.items)
        self.engine.missing_images = True
        with self.assertRaises(RuntimeError):
            self.operation.apply([{"profile": "single-text", "devices": ["1"]}])
        self.assertEqual(self.engine.items, previous)
        self.assertEqual(self.engine.mutations(), [])

    def test_benchmark_without_configuration_fails_without_engine_requests(self):
        with self.assertRaisesRegex(RuntimeError, "no supplied engine configuration"):
            self.operation.benchmark()
        self.assertEqual(self.engine.calls, [])

    def test_benchmark_success_and_failure_always_remove_only_benchmark(self):
        for exit_code in [0, 1]:
            with self.subTest(exit_code=exit_code):
                name = self.active("single-text", "1")
                previous = copy.deepcopy(self.engine.items[name])
                self.engine.benchmark_exit = exit_code
                self.operation.options["benchmark"] = {
                    "endpoint": "http://fixture.invalid/v1",
                    "api_key": "fixture-key",
                    "spec": {
                        "name": "fixture-benchmark",
                        "config": {"Image": "fixture.invalid/benchmark:1.0.0"},
                    },
                }
                with patch(
                    "control.engine.urlopen",
                    return_value=io.BytesIO(b'{"data":[{"id":"fixture-model"}]}'),
                ):
                    if exit_code:
                        with self.assertRaisesRegex(RuntimeError, "benchmark failed"):
                            self.operation.benchmark()
                    else:
                        self.operation.benchmark()
                self.assertNotIn("fixture-benchmark", self.engine.items)
                self.assertEqual(self.engine.items[name], previous)
                body = self.engine.created["fixture-benchmark"]
                self.assertIn("fixture-model", body["Cmd"])
                self.assertEqual(body["HostConfig"]["RestartPolicy"], {"Name": "no"})
                self.assertIn(
                    ("DELETE", "/containers/fixture-benchmark?force=true"),
                    self.engine.mutations(),
                )

    def test_startup_requires_every_catalogue_workload(self):
        for extra in [False, True]:
            configuration = copy.deepcopy(self.config)
            if extra:
                configuration["engine"]["workloads"]["unknown"] = copy.deepcopy(
                    configuration["engine"]["workloads"]["render-model"]
                )
            else:
                del configuration["engine"]["workloads"]["render-model"]
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(ValueError, "every catalogue workload"):
                    Operation(configuration)

    def test_startup_requires_every_supported_placement(self):
        configuration = copy.deepcopy(self.config)
        del configuration["engine"]["workloads"]["render-model"]["placements"]["1"]
        with self.assertRaisesRegex(ValueError, "every supported GPU placement"):
            Operation(configuration)

    def test_text_operations_use_the_configured_runtime(self):
        rules = self.config["catalogue"]["rules"]
        rules["runtime_names"]["language-pool"] = rules["runtime_names"].pop("text")
        for workload in rules["workloads"].values():
            if workload["kind"] == "text":
                workload["runtime"] = "language-pool"
        self.operation = Operation(self.config)
        self.operation.engine = self.engine
        name = self.active("single-text", "0")
        self.assertEqual(self.operation.status()["text_profile"], "single-text")
        self.operation.run("text:stop")
        self.assertEqual(
            self.engine.mutations(), [("POST", "/containers/" + name + "/stop")]
        )

    def test_text_off_stops_only_text_and_remembers_other_work(self):
        text = self.active("single-text", "0")
        image = self.active("render-model", "1")
        self.operation.run("text:start --model off --gpus 0")
        self.assertEqual(
            self.engine.mutations(), [("POST", "/containers/" + text + "/stop")]
        )
        self.assertTrue(self.engine.items[image]["State"]["Running"])
        self.assertEqual(
            json.loads((self.operation.state / "allocation.json").read_text()),
            [{"profile": "render-model", "devices": ["1"]}],
        )


class EngineTransportTests(unittest.TestCase):
    def test_create_failure_does_not_expose_supplied_secret_in_error_body(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        response.status = 500
        response.read.return_value = b'{"message":"fixture-sensitive-value"}'
        with patch("control.engine.UnixConnection", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, r"failed \(500\)") as error:
                Engine("/fixture/socket").request(
                    "POST",
                    "/containers/create?name=fixture",
                    {"Env": ["TOKEN=fixture-sensitive-value"]},
                )
        self.assertNotIn("fixture-sensitive-value", str(error.exception))
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
