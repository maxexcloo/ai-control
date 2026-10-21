"""CPU-only checks of runtime GPU reservations and shutdown."""

import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "gpu-lease.sh"
TIMEOUT = 10


@unittest.skipUnless(shutil.which("flock"), "GPU lease checks require Linux flock")
class Reservations(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.children = []
        self.ready = {}

    def tearDown(self):
        # Release test-controlled shutdowns even when an assertion failed.
        for name in ("release", "release-shutdown"):
            (Path(self.directory.name) / name).touch()
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=TIMEOUT)
            finally:
                child.stderr.close()
        self.directory.cleanup()

    def start(self, devices, *command):
        env = dict(
            os.environ,
            AI_GPU_DEVICES=devices,
            HIP_VISIBLE_DEVICES=devices,
            AI_GPU_LEASE_DIR=self.directory.name,
        )
        ready = Path(self.directory.name) / f"ready-{len(self.children)}"
        # This runs only after the launcher has acquired its locks. Probing with
        # flock during acquisition can itself make the launcher lose the race.
        runtime = [
            "/bin/sh",
            "-c",
            'touch "$1"; shift; exec "$@"',
            "lease-ready",
            str(ready),
            *(command or ("/bin/sleep", "30")),
        ]
        child = subprocess.Popen(
            [str(SCRIPT), *runtime],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.children.append(child)
        self.ready[child] = ready
        return child

    def busy(self, gpu):
        return (
            subprocess.run(
                [
                    "flock",
                    "--nonblock",
                    str(Path(self.directory.name) / f"gpu{gpu}.lock"),
                    "/bin/true",
                ],
                check=False,
            ).returncode
            != 0
        )

    def wait_marker(self, marker, child):
        deadline = time.monotonic() + TIMEOUT
        while not marker.exists():
            if child.poll() is not None:
                self.fail(
                    f"Runtime exited {child.returncode} before readiness: {child.stderr.read()}"
                )
            self.assertLess(
                time.monotonic(), deadline, "Runtime did not reach readiness"
            )
            time.sleep(0.01)

    def wait_ready(self, child):
        self.wait_marker(self.ready[child], child)

    def test_disjoint_singles_and_dual_refusal(self):
        self.wait_ready(self.start("0"))
        self.assertEqual(self.start("0,1").wait(timeout=TIMEOUT), 75)
        self.assertFalse(
            self.busy(1), "Failed dual acquisition must release its partial lease"
        )
        self.wait_ready(self.start("1"))
        self.assertEqual(self.start("0").wait(timeout=TIMEOUT), 75)
        self.assertEqual(self.start("1").wait(timeout=TIMEOUT), 75)

    def test_dual_excludes_both_singles_and_releases_on_stop(self):
        dual = self.start("0,1")
        self.wait_ready(dual)
        for gpu in ("0", "1"):
            self.assertEqual(self.start(gpu).wait(timeout=TIMEOUT), 75)
        dual.terminate()
        dual.wait(timeout=TIMEOUT)
        self.assertFalse(self.busy(0))
        self.assertFalse(self.busy(1))

    def test_concurrent_dual_start_has_one_winner(self):
        release = Path(self.directory.name) / "release"
        command = (
            "import pathlib,time\np=pathlib.Path("
            + repr(str(release))
            + ")\nwhile not p.exists(): time.sleep(0.01)\n"
        )
        first = self.start("0,1", "python3", "-c", command)
        second = self.start("0,1", "python3", "-c", command)
        deadline = time.monotonic() + TIMEOUT
        while first.poll() is None and second.poll() is None:
            self.assertLess(
                time.monotonic(),
                deadline,
                "One competing launcher must refuse the lease",
            )
            time.sleep(0.01)
        loser, winner = (first, second) if first.poll() is not None else (second, first)
        self.assertEqual(loser.returncode, 75)
        self.wait_ready(winner)
        release.touch()
        self.assertEqual(winner.wait(timeout=TIMEOUT), 0)

    def test_invalid_device_does_not_acquire(self):
        self.assertEqual(self.start("2").wait(timeout=TIMEOUT), 64)
        self.assertFalse(self.busy(0))
        self.assertFalse(self.busy(1))

    def test_lease_persists_until_graceful_shutdown_finishes(self):
        ready = Path(self.directory.name) / "handler-ready"
        stopping = Path(self.directory.name) / "stopping"
        release = Path(self.directory.name) / "release-shutdown"
        child_code = (
            "import signal,time,pathlib\n"
            "def stop(*_):\n"
            f" pathlib.Path({str(stopping)!r}).touch()\n"
            f" while not pathlib.Path({str(release)!r}).exists(): time.sleep(0.01)\n"
            " raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM,stop)\n"
            f"pathlib.Path({str(ready)!r}).touch()\n"
            "time.sleep(30)\n"
        )
        runtime = self.start("0", "python3", "-c", child_code)
        self.wait_marker(ready, runtime)
        runtime.send_signal(signal.SIGTERM)
        self.wait_marker(stopping, runtime)
        try:
            self.assertTrue(self.busy(0))
        finally:
            release.touch()
        runtime.wait(timeout=TIMEOUT)
        self.assertFalse(self.busy(0))


if __name__ == "__main__":
    unittest.main()
