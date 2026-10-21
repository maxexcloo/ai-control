"""Resolved Docker and Podman Compose models produce equivalent create requests."""

import os
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from deployment.engine_config import (
    container_spec,
    duration,
    memory,
    placement_environment,
    resolve,
)

# Captured from Docker Compose 5.5 and Podman Compose 1.6 using synthetic data.
DOCKER_MODEL = """
name: tmp
services:
  sample:
    command:
      - sh
      - -c
      - echo "$$NEUTRAL"
    container_name: ai-fixture
    devices:
      - source: /dev/kfd
        target: /dev/kfd
        permissions: rwm
    entrypoint:
      - /bin/sh
      - /opt/ai/gpu-lease.sh
    environment:
      NEUTRAL: $$KEEP
    healthcheck:
      test:
        - CMD
        - "true"
      interval: 2s
      start_period: 15m0s
    image: example.invalid/runtime:1.0.0
    init: true
    mem_limit: "85899345920"
    networks:
      default: null
    ports:
      - mode: ingress
        host_ip: 127.0.0.1
        target: 8188
        published: "8190"
        protocol: tcp
    restart: unless-stopped
    volumes:
      - type: bind
        source: /private/tmp/state
        target: /data
        read_only: true
        bind: {}
networks:
  default:
    name: tmp_default
"""

PODMAN_MODEL = """
services:
  sample:
    command:
    - sh
    - -c
    - echo "$NEUTRAL"
    container_name: ai-fixture
    devices:
    - /dev/kfd:/dev/kfd
    entrypoint:
    - /bin/sh
    - /opt/ai/gpu-lease.sh
    environment:
      NEUTRAL: $KEEP
    healthcheck:
      interval: 2s
      start_period: 15m
      test:
      - CMD
      - 'true'
    image: example.invalid/runtime:1.0.0
    init: true
    mem_limit: 80g
    ports:
    - 127.0.0.1:8190:8188
    restart: unless-stopped
    volumes:
    - ./state:/data:ro

"""


class EngineConfigTests(unittest.TestCase):
    def test_actual_render_formats_resolve_to_equivalent_specs(self):
        outputs = []
        for engine, rendered in [("docker", DOCKER_MODEL), ("podman", PODMAN_MODEL)]:
            with self.subTest(engine=engine):
                settings = {"AI_CONTAINER_ENGINE": engine, "NEUTRAL": "fixture"}
                document = yaml.safe_load(rendered)
                document["services"]["betterbench"] = dict(
                    document["services"]["sample"]
                )
                document["services"]["betterbench"].pop("container_name")
                rendered = yaml.safe_dump(document)
                workloads = SimpleNamespace(
                    WORKLOADS={"sample": {"placements": ["0", "1"]}},
                    CONTAINERS={"text": "fixture-text"},
                )
                with (
                    patch.dict(
                        os.environ,
                        AI_HOST="fixture.example",
                        RADIANCE_API_KEY="fixture-key",
                    ),
                    patch.dict("sys.modules", {"deployment.workloads": workloads}),
                    patch(
                        "deployment.engine_config.placement_environment",
                        return_value=settings,
                    ),
                    patch(
                        "deployment.engine_config.runtime.engine", return_value=engine
                    ),
                    patch(
                        "deployment.engine_config.runtime.compose",
                        return_value=["fixture-compose"],
                    ) as compose,
                    patch(
                        "deployment.engine_config.subprocess.run",
                        return_value=subprocess.CompletedProcess(
                            [], 0, stdout=rendered
                        ),
                    ) as run,
                ):
                    result = resolve(Path("/private/tmp"))
                self.assertEqual(run.call_count, 3)
                for call in compose.call_args_list:
                    self.assertEqual(
                        "--project-directory" in call.args, engine == "docker"
                    )
                self.assertTrue(
                    all(
                        call.kwargs["capture_output"] and call.kwargs["env"] == settings
                        for call in run.call_args_list[:2]
                    )
                )
                self.assertEqual(
                    run.call_args_list[-1].kwargs["env"]["BETTERBENCH_API_KEY"],
                    "fixture-key",
                )
                self.assertEqual(
                    run.call_args_list[-1].kwargs["env"]["GPU_CONTAINER"],
                    "fixture-text",
                )
                self.assertEqual(
                    result["benchmark"]["endpoint"], "http://fixture.example:8000/v1"
                )
                self.assertEqual(result["benchmark"]["api_key"], "fixture-key")
                self.assertEqual(result["benchmark"]["spec"]["name"], "ai-benchmark")
                placements = result["workloads"]["sample"]["placements"]
                self.assertEqual(placements["0"], placements["1"])
                spec = placements["0"]
                self.assertEqual(spec["name"], "ai-fixture")
                config = spec["config"]
                self.assertEqual(config["Cmd"][-1], 'echo "$NEUTRAL"')
                self.assertEqual(config["Env"], ["NEUTRAL=$KEEP"])
                self.assertEqual(
                    config["Labels"]["com.docker.compose.service"], "sample"
                )
                self.assertEqual(config["Healthcheck"]["StartPeriod"], 900_000_000_000)
                host = config["HostConfig"]
                self.assertEqual(host["Binds"], ["/private/tmp/state:/data:ro"])
                self.assertEqual(host["Memory"], 80 * 1024**3)
                self.assertEqual(
                    host["PortBindings"],
                    {"8188/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8190"}]},
                )
                self.assertEqual(
                    host["Devices"],
                    [
                        {
                            "PathOnHost": "/dev/kfd",
                            "PathInContainer": "/dev/kfd",
                            "CgroupPermissions": "rwm",
                        }
                    ],
                )
                outputs.append(spec)
        self.assertEqual(outputs[0], outputs[1])

    def test_unsupported_service_fields_and_custom_networks_are_rejected(self):
        service = yaml.safe_load(PODMAN_MODEL)["services"]["sample"]
        for extra in [{"privileged": True}, {"networks": {"private": None}}]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                container_spec(dict(service, **extra), "sample")

    def test_healthcheck_timing_retains_image_inherited_test(self):
        config = container_spec(
            {
                "image": "fixture:1",
                "container_name": "fixture",
                "healthcheck": {"interval": "5s"},
            },
            "sample",
        )["config"]
        self.assertEqual(config["Healthcheck"], {"Interval": 5_000_000_000})

    def test_ipv6_http_origin_preserves_address_and_port(self):
        from deployment.address import http_url

        self.assertEqual(http_url("fd00::1", 1337), "http://[fd00::1]:1337")
        self.assertEqual(http_url("[::1]", 8000), "http://[::1]:8000")
        self.assertEqual(http_url("host.example", 1337), "http://host.example:1337")

    def test_security_readiness_and_shutdown_settings_survive(self):
        service = yaml.safe_load(PODMAN_MODEL)["services"]["sample"]
        service.update(
            volumes=["/srv/library:/models:ro"],
            network_mode="host",
            ipc="host",
            cap_add=["SYS_PTRACE"],
            security_opt=["label=disable", "seccomp=unconfined"],
            group_add=["keep-groups"],
            userns_mode="keep-id",
            stop_grace_period="60s",
        )
        service["healthcheck"].update(timeout="5s", retries=300)
        config = container_spec(service, "sample")["config"]
        host = config["HostConfig"]
        for name, value in [
            ("NetworkMode", "host"),
            ("IpcMode", "host"),
            ("CapAdd", ["SYS_PTRACE"]),
            ("SecurityOpt", ["label=disable", "seccomp=unconfined"]),
            ("GroupAdd", ["keep-groups"]),
            ("UsernsMode", "keep-id"),
        ]:
            self.assertEqual(host[name], value)
        self.assertEqual(config["StopTimeout"], 60)
        self.assertEqual(config["Healthcheck"]["Timeout"], 5_000_000_000)
        self.assertEqual(config["Healthcheck"]["Retries"], 300)

    def test_unsupported_or_unresolved_values_fail_instead_of_exposing_state(self):
        for volumes in [
            ["./relative:/data"],
            [{"type": "volume", "source": "named", "target": "/data"}],
        ]:
            with self.subTest(volumes=volumes), self.assertRaises(ValueError):
                container_spec(
                    {
                        "image": "fixture:1",
                        "container_name": "fixture",
                        "volumes": volumes,
                    },
                    "sample",
                )
        with self.assertRaises(ValueError):
            container_spec(
                {
                    "image": "fixture:1",
                    "container_name": "fixture",
                    "environment": {"MISSING": None},
                },
                "sample",
            )
        for value in ["bad", "2days"]:
            with self.assertRaises(ValueError):
                duration(value)
        self.assertEqual(duration("1m2.5s"), 62_500_000_000)
        self.assertEqual(memory("512m"), 512 * 1024**2)

    def test_placement_environment_preserves_text_context_and_gpu_interpolation(self):
        profiles = {
            "radiance-uncensored": ("text", "RADIANCE_DEVICES"),
            "radiance-other": ("text", "RADIANCE_DEVICES"),
            "comfyui-image": ("image", "COMFYUI_DEVICES"),
        }
        workloads = SimpleNamespace(
            PROFILES=profiles,
            CONTAINERS={"text": "ai-text", "image": "ai-image"},
            WORKLOADS={
                profile: {
                    "placements": ["0", "1", "0,1"],
                    "environment": {
                        devices: {
                            "RADIANCE_CONTEXT": "163840"
                            if profile == "radiance-uncensored" and devices != "0,1"
                            else "262144",
                            "RADIANCE_TP": "2" if devices == "0,1" else "1",
                        }
                        for devices in ["0", "1", "0,1"]
                    },
                }
                for profile in profiles
            },
        )
        with (
            patch.dict(os.environ, {"AI_DATA_DIR": "/fixture"}, clear=True),
            patch.dict("sys.modules", {"deployment.workloads": workloads}),
            patch("deployment.engine_config.runtime.ROOT", Path("/fixture")),
        ):
            single = placement_environment("radiance-uncensored", "0")
            dual = placement_environment("radiance-uncensored", "0,1")
            other = placement_environment("radiance-other", "1")
            image = placement_environment("comfyui-image", "1")
            with self.assertRaises(ValueError):
                placement_environment("comfyui-image", "2")
        self.assertEqual(
            (single["RADIANCE_CONTEXT"], single["RADIANCE_TP"]), ("163840", "1")
        )
        self.assertEqual(
            (dual["RADIANCE_CONTEXT"], dual["RADIANCE_TP"]), ("262144", "2")
        )
        self.assertEqual(other["RADIANCE_CONTEXT"], "262144")
        self.assertEqual(image["GPU_CONTAINER"], "ai-image")
        for key in ["AI_GPU_DEVICES", "HIP_VISIBLE_DEVICES", "COMFYUI_DEVICES"]:
            self.assertEqual(image[key], "1")
        self.assertEqual(image["AI_GPU_LEASE_DIR"], "/fixture/control/leases")
