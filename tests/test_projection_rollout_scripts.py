from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProjectionRolloutScriptTests(unittest.TestCase):
    def test_shell_wrapper_preserves_default_compose_file_selection(self):
        temp = Path(tempfile.mkdtemp())
        capture = temp / "args.json"
        fake = temp / "docker"
        fake.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$CAPTURE\"\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        env = dict(os.environ, PATH=f"{temp}:{os.environ.get('PATH', '')}", CAPTURE=str(capture))
        completed = subprocess.run(
            ["sh", str(ROOT / "scripts" / "reconcile-pipeline-projection.sh"), "--apply", "--limit", "7"],
            cwd=ROOT,
            env=env,
            check=False,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            capture.read_text(encoding="utf-8").splitlines(),
            ["compose", "run", "--rm", "--no-deps", "app", "reconcile-pipeline-projection", "--apply", "--limit", "7"],
        )

    @unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell ausente")
    def test_powershell_wrapper_preserves_default_compose_file_selection(self):
        executable = shutil.which("pwsh") or shutil.which("powershell")
        script = ROOT / "scripts" / "reconcile-pipeline-projection.ps1"
        command = (
            "function docker { $args | ConvertTo-Json -Compress; $global:LASTEXITCODE = 0 }; "
            f"& '{script}' --apply --limit 7"
        )
        completed = subprocess.run(
            [executable, "-NoProfile", "-Command", command],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            ["compose", "run", "--rm", "--no-deps", "app", "reconcile-pipeline-projection", "--apply", "--limit", "7"],
        )


if __name__ == "__main__":
    unittest.main()
