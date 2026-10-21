"""Validate Python and browser source syntax without runtime configuration."""
import ast
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
for folder in ['control', 'deployment', 'scripts', 'tests']:
    for source in sorted((root / folder).rglob('*.py')):
        if '.venv' not in source.parts and '__pycache__' not in source.parts:
            ast.parse(source.read_text(), filename=str(source))
for source in sorted((root / 'control/web').iterdir()):
    if source.suffix in {'.js', '.mjs'}:
        subprocess.run(['node', '--check', str(source)], check=True)
print('Python and browser source syntax passed.')
