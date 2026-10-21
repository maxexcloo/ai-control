"""Build the actual UI in a temporary directory, keeping dependencies outside the checkout."""

import shutil
import subprocess
import tempfile
from pathlib import Path

source = Path(__file__).resolve().parents[1] / "control/web"
with tempfile.TemporaryDirectory(prefix="ai-control-web-") as temporary:
    target = Path(temporary)
    for path in source.iterdir():
        if path.is_file():
            shutil.copy2(path, target / path.name)
    subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], check=True, cwd=target)
    subprocess.run(["npm", "run", "build"], check=True, cwd=target)
    if (
        not (target / "dist/index.html").is_file()
        or not (target / "dist/app.js").is_file()
    ):
        raise SystemExit("The UI build did not produce its entry files.")
print("The isolated UI build passed.")
