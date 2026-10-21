"""External configuration is literal, explicit and independent of Mise."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deployment.__main__ import load_environment


class EnvironmentTests(unittest.TestCase):
    def test_literal_values_and_process_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env').write_text(
                'AI_CONTAINER_ENGINE=podman\nAI_DATA_DIR=/srv/state\n'
                'AI_MODELS_DIR=/srv/models\nCONTROL_ENGINE_SOCKET=/run/engine.sock\n'
                'LITERAL="$(touch unwanted) $HOME"\nEMPTY=\n'
            )
            with patch.dict(os.environ, {'AI_CONTAINER_ENGINE': 'docker'}, clear=True):
                load_environment(root)
                self.assertEqual(os.environ['AI_CONTAINER_ENGINE'], 'docker')
                self.assertEqual(os.environ['AI_ROOT'], str(root))
                self.assertEqual(os.environ['LITERAL'], '$(touch unwanted) $HOME')
                self.assertEqual(os.environ['EMPTY'], '')
            self.assertFalse((root / 'unwanted').exists())

    def test_missing_or_relative_required_paths_fail_with_the_setting_name(self):
        settings = {'AI_CONTAINER_ENGINE': 'docker', 'AI_DATA_DIR': '/state',
                    'AI_MODELS_DIR': '/models', 'CONTROL_ENGINE_SOCKET': '/engine.sock'}
        with tempfile.TemporaryDirectory() as directory:
            for name in ['AI_DATA_DIR', 'AI_MODELS_DIR', 'CONTROL_ENGINE_SOCKET']:
                for value in ['', 'relative']:
                    with self.subTest(name=name, value=value), patch.dict(os.environ, settings | {name: value}, clear=True):
                        with self.assertRaisesRegex(ValueError, name):
                            load_environment(Path(directory))

    def test_invalid_assignment_does_not_include_the_value_in_the_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env').write_text('bad-key=private-value\n')
            with self.assertRaisesRegex(ValueError, '^Invalid .env assignment on line 1.$'):
                load_environment(root)
