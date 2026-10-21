"""Configuration generation preserves private accounts without private task files."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deployment import configure


class ConfigureTests(unittest.TestCase):
    def test_external_state_accounts_and_keys_survive_reconfiguration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "configuration"
            state = Path(directory) / "state"
            root.mkdir()
            output = state / "control/data/control.json"
            output.parent.mkdir(parents=True)
            output.write_text(
                json.dumps(
                    {
                        "users": {"owner": "fixture-hash"},
                        "origins": ["https://alias.invalid"],
                    }
                )
            )
            (root / ".env").write_text(
                "CONTROL_API_KEY=fixture-api\nCONTROL_SESSION_KEY=fixture-session\n"
            )
            original = (root / ".env").read_bytes()
            links = [{"title": "Fixture Text", "url": "http://private.invalid:8000/"}]
            metadata = {"sample": {"radiance": True, "title": "Fixture Text"}}
            with (
                patch.dict(os.environ, AI_DATA_DIR=str(state), CONTROL_PORT="1337"),
                patch.object(configure, "SERVICES", metadata),
                patch.object(configure, "service_links", return_value=links),
                patch.object(
                    configure,
                    "endpoints",
                    return_value=(
                        "private.invalid",
                        {1337: "https://control.invalid/"},
                    ),
                ),
                patch.object(configure, "resolve", return_value={"workloads": {}}),
                patch("builtins.print"),
            ):
                configure.apply(root)
            result = json.loads(output.read_text())
            self.assertEqual(result["users"], {"owner": "fixture-hash"})
            self.assertIn("https://alias.invalid", result["origins"])
            self.assertIn("https://control.invalid", result["origins"])
            self.assertEqual(result["radiance_url"], "http://private.invalid:8000/")
            self.assertEqual(result["engine"]["state_dir"], "/state")
            self.assertEqual((root / ".env").read_bytes(), original)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertFalse((root / "mise.toml").exists())

    def test_missing_accounts_fail_before_generating_configuration(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, AI_DATA_DIR=directory),
            self.assertRaisesRegex(
                SystemExit, "Restore existing Control access settings"
            ),
        ):
            configure.apply(Path(directory))
