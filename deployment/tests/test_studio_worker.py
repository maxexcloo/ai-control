"""Verify SDK content, rather than a worker image's provenance label."""
import contextlib
import io
import os
import runpy
import sys
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from deployment import studio_worker


class StudioWorkerTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, AI_CONTAINER_ENGINE='podman')
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.studio = 'ghcr.io/maxexcloo/studio:0.39.0'
        self.worker = 'ghcr.io/maxexcloo/comfyui-radiance:v0.42.1'
        self.write_configuration()

    def write_configuration(self):
        (self.root / 'compose.yaml').write_text(
            f'x-studio-worker:\n  image: {self.studio}\n'
            f'services:\n  comfyui-image:\n    image: {self.worker}\n'
        )

    def test_matching_sdk_files_pass_with_cpu_only_runtimes(self):
        for engine in ['docker', 'podman']:
            with self.subTest(engine=engine), patch.dict(os.environ, AI_CONTAINER_ENGINE=engine):
                with patch.object(studio_worker.subprocess, 'check_output', side_effect=['a' * 64 + '\n', 'a' * 64 + '\n']) as run:
                    with contextlib.redirect_stdout(io.StringIO()):
                        studio_worker.verify(self.root)
                commands = [call.args[0] for call in run.call_args_list]
                self.assertEqual(len(commands), 2)
                self.assertEqual(commands[0][commands[0].index('--entrypoint') + 1], 'python3')
                self.assertIn(self.worker, commands[0])
                self.assertEqual(commands[0][-1], '/root/ComfyUI/custom_nodes/studio_worker')
                self.assertEqual(commands[1][commands[1].index('--entrypoint') + 1], 'node')
                self.assertIn(self.studio, commands[1])
                self.assertEqual(commands[1][-1], '/app/comfyui/studio_worker')
                for command in commands:
                    self.assertEqual(command[:3], [engine, 'run', '--rm'])
                    self.assertEqual(command[command.index('--network') + 1], 'none')
                    for flag in ['--device', '--gpus', '--volume', '-v', '--env', '-e']:
                        # Node's -e is its code argument, not a Podman environment flag.
                        self.assertNotIn(flag, command[:command.index('--entrypoint')])

    def test_changed_sdk_requires_a_matching_worker(self):
        with patch.object(studio_worker.subprocess, 'check_output', side_effect=['a' * 64, 'b' * 64]):
            with self.assertRaisesRegex(SystemExit, r'v0\.42\.1 differs from .*0\.39\.0'):
                studio_worker.verify(self.root)

    def test_empty_or_malformed_hash_fails_for_either_image(self):
        for invalid in ['', 'not-a-digest', 'a' * 63, 'A' * 64, 'a' * 64 + '\nextra']:
            for reference in [False, True]:
                outputs = ['a' * 64, invalid] if reference else [invalid]
                with self.subTest(invalid=invalid, reference=reference):
                    with patch.object(studio_worker.subprocess, 'check_output', side_effect=outputs):
                        with self.assertRaisesRegex(SystemExit, 'Invalid SDK fingerprint') as error:
                            studio_worker.verify(self.root)
                        self.assertIn(self.studio if reference else self.worker, str(error.exception))

    def test_invalid_reference_fails_before_starting_containers(self):
        self.studio = 'ghcr.io/maxexcloo/studio:latest'
        self.write_configuration()
        with patch.object(studio_worker.subprocess, 'check_output') as run:
            with self.assertRaisesRegex(SystemExit, 'exact release'):
                studio_worker.verify(self.root)
            run.assert_not_called()

    def test_full_worker_build_tag_still_requires_matching_sdk_files(self):
        self.worker = 'ghcr.io/maxexcloo/comfyui-radiance:sha-' + 'a' * 40
        self.write_configuration()
        for matching in [True, False]:
            hashes = ['a' * 64, ('a' if matching else 'b') * 64]
            with self.subTest(matching=matching), patch.object(studio_worker, 'fingerprint', side_effect=hashes) as fingerprint:
                with contextlib.redirect_stdout(io.StringIO()):
                    if matching:
                        studio_worker.verify(self.root)
                    else:
                        with self.assertRaisesRegex(SystemExit, 'differs from'):
                            studio_worker.verify(self.root)
                self.assertEqual(fingerprint.call_args_list[0].args[0], self.worker)
                self.assertEqual(fingerprint.call_args_list[1].args[0], self.studio)

    def test_only_full_lowercase_build_tags_are_accepted_for_workers(self):
        for tag in ['latest', 'sha-' + 'a' * 39, 'sha-' + 'a' * 41,
                    'sha-' + 'A' * 40, 'sha-' + 'a' * 40 + '-amd64',
                    'v0.42.0@sha256:' + 'a' * 64]:
            self.worker = 'ghcr.io/maxexcloo/comfyui-radiance:' + tag
            self.write_configuration()
            with self.subTest(tag=tag), patch.object(studio_worker, 'fingerprint') as fingerprint:
                with self.assertRaisesRegex(SystemExit, 'exact release or full sha-40hex'):
                    studio_worker.verify(self.root)
                fingerprint.assert_not_called()

    def test_build_tag_does_not_replace_exact_studio_release(self):
        self.studio = 'ghcr.io/maxexcloo/studio:sha-' + 'a' * 40
        self.write_configuration()
        with patch.object(studio_worker, 'fingerprint') as fingerprint:
            with self.assertRaisesRegex(SystemExit, 'exact release'):
                studio_worker.verify(self.root)
            fingerprint.assert_not_called()

    def test_container_failure_names_the_image_and_source_tree(self):
        failure = subprocess.CalledProcessError(1, 'podman', stderr='SDK symlink is forbidden')
        with patch.object(studio_worker.subprocess, 'check_output', side_effect=failure):
            with self.assertRaisesRegex(SystemExit, 'SDK symlink is forbidden') as error:
                studio_worker.verify(self.root)
            self.assertIn(self.worker, str(error.exception))

    def test_missing_or_unsupported_engine_fails_before_starting_containers(self):
        for engine in [None, '', 'containerd']:
            with self.subTest(engine=engine), patch.dict(os.environ):
                if engine is None:
                    os.environ.pop('AI_CONTAINER_ENGINE', None)
                else:
                    os.environ['AI_CONTAINER_ENGINE'] = engine
                with patch.object(studio_worker.subprocess, 'check_output') as run:
                    with self.assertRaises(ValueError) as error:
                        studio_worker.verify(self.root)
                    self.assertIn('AI_CONTAINER_ENGINE', str(error.exception))
                    run.assert_not_called()
