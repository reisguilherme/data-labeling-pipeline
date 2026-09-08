from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def load_secret(name: str, required: bool) -> None:
    path = os.environ.get(f"{name}_FILE")
    value = Path(path).read_text(encoding="utf-8").strip() if path else os.environ.get(name, "")
    if required and not value:
        raise RuntimeError(f"secret ausente: {name}_FILE")
    if value:
        os.environ[name] = value


load_secret("MST_WORKER_TOKEN", True)
load_secret("HF_TOKEN", False)
load_secret("MINIO_ROOT_USER", True)
load_secret("MINIO_ROOT_PASSWORD", True)
raise SystemExit(subprocess.call([sys.executable, "-m", "sam3_runner", *sys.argv[1:]]))
