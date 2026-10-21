"""Verify the host tools run without the AI Control app beside them."""
import ast
from pathlib import Path
import unittest

from control.allocation import normalise_allocation as app_normalise
from deployment.allocation import normalise_allocation
from deployment.workloads import GPU_RULES

ROOT = Path(__file__).resolve().parents[1]


class StandaloneTests(unittest.TestCase):
    def test_no_module_imports_the_app(self):
        # Bento carries only deployment/; an app import breaks boot and gpu there.
        for path in ROOT.glob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ''] if isinstance(node, ast.ImportFrom) else []
                for name in names:
                    self.assertFalse(name == 'control' or name.startswith('control.'), f'{path.name} imports {name}')

    def test_allocation_rule_matches_the_app(self):
        profiles = list(GPU_RULES['profiles'])
        cases = [[], [{'profile': profiles[0], 'devices': ['0']}], [{'profile': 'off', 'devices': ['0']}], [{'profile': profiles[0], 'devices': ['2']}], 'bad']
        for case in cases:
            outcomes = []
            for rule in (normalise_allocation, app_normalise):
                try:
                    outcomes.append(('ok', rule(case, GPU_RULES)))
                except ValueError as error:
                    outcomes.append(('error', str(error)))
            self.assertEqual(outcomes[0], outcomes[1], case)


if __name__ == '__main__':
    unittest.main()
