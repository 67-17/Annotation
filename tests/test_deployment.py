"""Verify real shell staging without contacting or scheduling a cluster."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class DeploymentTests(unittest.TestCase):
    def test_source_context_and_compatibility_entrypoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            fake = directory / "det"
            fake.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
if "create" in sys.argv:
    root = Path(sys.argv[-1])
    files = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    Path(os.environ["ANNOTATION_TEST_LOG"]).write_text(json.dumps({"files":files,"context":str(root),"config":sys.argv[-2]}))
''')
            fake.chmod(0o700)
            log = directory / "context.json"
            env = {**os.environ, "ANNOTATION_DET_CLI": str(fake), "ANNOTATION_TEST_LOG": str(log),
                   "TMPDIR": str(directory)}
            subprocess.run(["bash", str(ROOT / "submit.sh")], cwd=temporary, env=env, check=True,
                           capture_output=True, text=True, timeout=20)
            result = json.loads(log.read_text())
            files = set(result["files"])
            self.assertTrue({"app.py", "start.sh", "scripts/start.sh", "backend/__init__.py",
                             "backend/app.py", "backend/batch.py", "backend/curve_first.py",
                             "static/index.html", "static/app.js", "requirements.txt"} <= files)
            self.assertFalse(any(p.startswith(("state/", "tests/", "docs/", ".venv", "determined-checkpoints/")) for p in files))
            self.assertEqual(Path(result["config"]), ROOT / "configs/experiment.yaml")
            # Import the staged service itself with an isolated state directory.
            env["ANNOTATION_STATE_DIR"] = str(directory / "isolated-state")
            subprocess.run([sys.executable, "-c", "from app import app; assert app"],
                           cwd=result["context"], env=env, check=True, capture_output=True, timeout=20)


if __name__ == "__main__":
    unittest.main()
