"""Leitor mínimo de .env.

Escrito à mão em vez de adicionar python-dotenv: são quinze linhas e a lista de
dependências do projeto é curta de propósito.

ORDEM IMPORTA: precisa rodar ANTES de `from server.config import ...`, porque o
config lê os.environ no import (MST_PROXY_WIDTH e companhia). Carregar depois
produz um ".env silenciosamente ignorado", que é um bug chato de perceber.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILENAME = ".env"


def load(path: Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Carrega o .env para os.environ. Devolve o que foi lido."""
    target = path or Path(__file__).resolve().parent.parent / ENV_FILENAME
    if not target.exists():
        return {}

    loaded: dict[str, str] = {}
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded
