"""Mapa label -> índice de classe, estável entre execuções.

APPEND-ONLY, e isso é o ponto inteiro do módulo. O índice de classe fica gravado
dentro de milhares de arquivos .txt espalhados pelo dataset; reordenar ou
remover uma entrada reescreveria o significado de todos eles de uma vez, sem
tocar em nenhum. Arquivar um objeto, renomear um rótulo na interface, ou
processar os objetos numa ordem diferente não pode renumerar nada.

Por isso o mapa NÃO é derivado do objects.json: lá o `label` é campo mutável
(PATCH /api/objects/{id}), e a estabilidade do índice tem que ser independente
disso.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

SCHEMA_VERSION = 1
FILENAME = "sam3_classes.json"


class ClassMap:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._names: list[str] = []
        self._lock = threading.Lock()
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._names = []
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._names = []
            return
        self._names = list(data.get("names") or [])

    @property
    def names(self) -> list[str]:
        return list(self._names)

    def get(self, label: str) -> int | None:
        try:
            return self._names.index(label)
        except ValueError:
            return None

    def index_of(self, label: str) -> int:
        """Índice do rótulo, cadastrando no fim se ainda não existir."""
        with self._lock:
            try:
                return self._names.index(label)
            except ValueError:
                self._names.append(label)
                self._save()
                return len(self._names) - 1

    def ensure(self, labels: list[str]) -> dict[str, int]:
        return {label: self.index_of(label) for label in labels}

    def _save(self) -> None:
        from datetime import datetime, timezone

        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": datetime.now(timezone.utc).astimezone().isoformat(
                timespec="milliseconds"
            ),
            "note": "append-only: o índice está gravado em milhares de .txt",
            "names": self._names,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def data_yaml(self) -> str:
        """Bloco `names` no formato do data.yaml do Ultralytics."""
        lines = ["names:"]
        for index, name in enumerate(self._names):
            lines.append(f"  {index}: {name}")
        return "\n".join(lines)
