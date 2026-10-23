"""Resolve synthetic deployment configuration without an engine or host."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deployment.config import Deployment, load_environment, substitute

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ["workloads.json", "containers.yaml"]:
            shutil.copy(FIXTURES / name, self.root / name)
        environment = patch.dict(
            os.environ,
            {
                "AI_DATA_DIR": str(self.root),
                "AI_MODELS_DIR": str(self.root / "models"),
                "AI_HOST": "192.0.2.10",
                "CONTROL_ENGINE_SOCKET": "/run/engine.sock",
                "CONTROL_PORT": "1337",
                "CONTROL_URL": "https://control.example.invalid/",
                "RADIANCE_API_KEY": "fixture-key",
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.deployment = Deployment(self.root)

    def test_substitution_uses_defaults_and_leaves_shell_variables(self):
        self.assertEqual(
            substitute(
                ["${A}", "${B:-two}", "$C", {"k": "${A}"}], {"A": "one", "B": ""}
            ),
            ["one", "two", "$C", {"k": "one"}],
        )
        with self.assertRaisesRegex(ValueError, "MISSING"):
            substitute("${MISSING}", {})

    def test_workload_spec_carries_placement_identity_and_environment(self):
        spec = self.deployment.workload_spec("single-text", "0,1")
        self.assertEqual(spec["name"], "fixture-text")
        self.assertEqual(
            spec["config"]["Cmd"], ['exec serve --key "$FIXTURE_KEY" --tp 2']
        )
        self.assertEqual(
            spec["config"]["Labels"], {"io.excloo.ai-control.workload": "single-text"}
        )
        self.assertEqual(
            spec["config"]["Env"],
            [
                "FIXTURE_KEY=fixture-key",
                "AI_GPU_DEVICES=0,1",
                "HIP_VISIBLE_DEVICES=0,1",
            ],
        )
        self.assertEqual(
            self.deployment.workload_spec("single-text", "1")["config"]["Cmd"][0][-6:],
            "--tp 1",
        )
        # Shared YAML anchors are never mutated between placements.
        self.assertNotIn(
            "Labels", self.deployment.containers["workloads"]["dual-text"]["config"]
        )

    def test_mismatched_workload_lists_fail(self):
        (self.root / "containers.yaml").write_text(
            (FIXTURES / "containers.yaml")
            .read_text()
            .replace("  dual-text: *text\n", "")
        )
        with self.assertRaisesRegex(ValueError, "same workloads"):
            Deployment(self.root)

    def test_catalogue_exposes_only_public_workload_fields(self):
        rules = self.deployment.rules()
        self.assertEqual(rules["profiles"]["off"], [])
        self.assertEqual(
            rules["runtime_names"], {"text": "Fixture Text", "image": "Fixture Image"}
        )
        self.assertEqual(
            set(rules["workloads"]["single-text"]),
            {"kind", "label", "model_label", "runtime"},
        )

    def test_images(self):
        self.assertEqual(
            self.deployment.images(),
            [
                "fixture.invalid/benchmark:1.0.0",
                "fixture.invalid/control:1.0.0",
                "fixture.invalid/text:1.0.0",
                "fixture.invalid/worker:1.0.0",
            ],
        )

    def test_prepare_creates_only_writable_binds_beneath_the_roots(self):
        self.assertEqual(
            self.deployment.writable_directories(),
            [self.root / "control", self.root / "image"],
        )

    def test_write_control_retains_users_and_adds_missing_keys(self):
        output = self.root / "control/data/control.json"
        output.parent.mkdir(parents=True)
        output.write_text(
            json.dumps(
                {"users": {"owner": "hash"}, "origins": ["https://stale.invalid"]}
            )
        )
        (self.root / ".env").write_text("CONTROL_SESSION_KEY=kept\n")
        os.environ["CONTROL_SESSION_KEY"] = "kept"
        self.deployment.write_control()
        written = json.loads(output.read_text())
        self.assertEqual(written["users"], {"owner": "hash"})
        self.assertEqual(
            written["origins"],
            [
                "http://192.0.2.10:1337",
                "https://control.example.invalid",
            ],
        )
        self.assertEqual(written["radiance_url"], "http://text.example.invalid:8000/")
        links = {link["title"]: link for link in written["catalogue"]["services"]}
        self.assertEqual(links["Fixture Text"]["url"], "/radiance/")
        self.assertEqual(
            links["Fixture Text"]["profiles"], ["dual-text", "single-text"]
        )
        self.assertEqual(links["Fixture Image"]["container"], "fixture-image")
        self.assertEqual(
            links["Fixture Image"]["url"], "https://image.example.invalid/"
        )
        self.assertEqual(written["host"], "control.example.invalid")
        self.assertEqual(
            set(written["engine"]["workloads"]["single-text"]["placements"]),
            {"0", "1", "0,1"},
        )
        env = (self.root / ".env").read_text()
        self.assertIn("CONTROL_SESSION_KEY=kept", env)
        self.assertEqual(env.count("CONTROL_API_KEY="), 1)
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_missing_accounts_never_create_access(self):
        with self.assertRaisesRegex(SystemExit, "Restore existing Control access"):
            self.deployment.write_control()

    def test_environment_reads_literal_values_and_explicit_values_win(self):
        (self.root / ".env").write_text(
            '# comment\nexport LITERAL="$HOME value"\nCONTROL_PORT=9999\n'
        )
        load_environment(self.root)
        self.assertEqual(os.environ["LITERAL"], "$HOME value")
        self.assertEqual(os.environ["CONTROL_PORT"], "1337")
        (self.root / ".env").write_text("not an assignment\n")
        with self.assertRaisesRegex(ValueError, "line 1"):
            load_environment(self.root)


if __name__ == "__main__":
    unittest.main()
