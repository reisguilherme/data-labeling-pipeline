"""Backend da aplicação e bootstrap do pacote compartilhado em desenvolvimento."""

from __future__ import annotations

import sys
from pathlib import Path

_core_source = Path(__file__).resolve().parents[2] / "packages" / "pipeline-core" / "src"
if _core_source.is_dir() and str(_core_source) not in sys.path:
    sys.path.insert(0, str(_core_source))
