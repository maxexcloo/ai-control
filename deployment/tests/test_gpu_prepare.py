"""GPU setup selects the engine and preserves active lease files on failure."""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deployment import gpu_prepare


class GpuPrepareTests(unittest.TestCase):
    def test_both_engines_copy_helper_and_preserve_existing_leases(self):
        for engine in ["docker", "podman"]:
            with (
                self.subTest(engine=engine),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                leases = root / "control/leases"
                leases.mkdir(parents=True)
                existing = leases / "gpu0.lock"
                existing.write_bytes(b"active lease")
                existing.chmod(0o600)
                before = existing.stat()
                commands = []

                def run(arguments, commands=commands, **kwargs):
                    commands.append(arguments)
                    if arguments[1] == "cp":
                        Path(arguments[-1]).write_bytes(b"static busybox")
                    return subprocess.CompletedProcess(
                        arguments, 0, stdout="fixture-container\n"
                    )

                with (
                    patch.dict(os.environ, AI_CONTAINER_ENGINE=engine),
                    patch.object(gpu_prepare.subprocess, "run", side_effect=run),
                ):
                    gpu_prepare.prepare(root)
                self.assertEqual(
                    [arguments[:2] for arguments in commands],
                    [[engine, operation] for operation in ["create", "cp", "rm"]],
                )
                self.assertEqual(
                    commands[0], [engine, "create", gpu_prepare.IMAGE, "true"]
                )
                self.assertEqual(commands[1][2], "fixture-container:/bin/busybox")
                self.assertEqual(
                    commands[-1], [engine, "rm", "--force", "fixture-container"]
                )
                binary = root / "control/bin/busybox"
                self.assertEqual(binary.read_bytes(), b"static busybox")
                self.assertEqual(stat.S_IMODE(binary.stat().st_mode), 0o755)
                self.assertEqual(existing.read_bytes(), b"active lease")
                self.assertEqual(existing.stat().st_ino, before.st_ino)
                self.assertEqual(existing.stat().st_mtime_ns, before.st_mtime_ns)
                self.assertEqual(stat.S_IMODE(existing.stat().st_mode), 0o600)
                self.assertEqual(
                    stat.S_IMODE((leases / "gpu1.lock").stat().st_mode), 0o666
                )
                self.assertEqual(
                    sorted(path.name for path in binary.parent.iterdir()),
                    ["busybox", "gpu-lease.sh"],
                )

    def test_failed_copy_removes_container_without_replacing_binary_or_leases(self):
        for engine in ["docker", "podman"]:
            with (
                self.subTest(engine=engine),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                binary = root / "control/bin/busybox"
                binary.parent.mkdir(parents=True)
                binary.write_bytes(b"existing binary")
                leases = root / "control/leases"
                leases.mkdir()
                lock = leases / "gpu0.lock"
                lock.write_bytes(b"active lease")
                commands = []

                def run(arguments, commands=commands, **kwargs):
                    commands.append(arguments)
                    if arguments[1] == "cp":
                        Path(arguments[-1]).write_bytes(b"partial copy")
                        raise subprocess.CalledProcessError(1, arguments)
                    return subprocess.CompletedProcess(
                        arguments, 0, stdout="fixture-container\n"
                    )

                with (
                    patch.dict(os.environ, AI_CONTAINER_ENGINE=engine),
                    patch.object(gpu_prepare.subprocess, "run", side_effect=run),
                    self.assertRaises(subprocess.CalledProcessError),
                ):
                    gpu_prepare.prepare(root)
                self.assertEqual(
                    commands[-1], [engine, "rm", "--force", "fixture-container"]
                )
                self.assertEqual(binary.read_bytes(), b"existing binary")
                self.assertEqual(lock.read_bytes(), b"active lease")
                self.assertFalse((leases / "gpu1.lock").exists())
                self.assertEqual(list(binary.parent.iterdir()), [binary])

    def test_failed_create_preserves_existing_binary_and_cleans_temporary_directory(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "control/bin/busybox"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"existing binary")
            with (
                patch.dict(os.environ, AI_CONTAINER_ENGINE="docker"),
                patch.object(
                    gpu_prepare.subprocess,
                    "run",
                    side_effect=subprocess.CalledProcessError(1, ["docker", "create"]),
                ) as run,
                self.assertRaises(subprocess.CalledProcessError),
            ):
                gpu_prepare.prepare(root)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(binary.read_bytes(), b"existing binary")
            self.assertEqual(list(binary.parent.iterdir()), [binary])
