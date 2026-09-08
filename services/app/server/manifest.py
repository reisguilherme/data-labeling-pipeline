"""Manifestos por objeto: o que já foi baixado e o que foi descartado.

Os dois existem para a mesma coisa: NUNCA repetir trabalho. Sem o manifesto de
download, cada sincronização rebaixaria o acervo inteiro; sem o de exclusão, um
arquivo descartado por regra voltaria no próximo `gcloud storage cp`, seria
descartado de novo, e assim para sempre.

Ambos são gravados com a mesma disciplina do annotations.json — tmp no mesmo
diretório, fsync, os.replace — porque um manifesto truncado por queda de energia
no meio de um download é indistinguível de "nada foi baixado".
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .videos import iso

SCHEMA_VERSION = 1


def _write_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data.get("files") or {}


# --------------------------------------------------------------------------
# downloads
# --------------------------------------------------------------------------


class DownloadManifest:
    """Registro do que já veio do bucket, chaveado pelo nome do blob."""

    def __init__(self, path: Path, object_id: str) -> None:
        self.path = path
        self.object_id = object_id
        self._files: dict[str, dict] = _read(path)

    def count(self) -> int:
        return len(self._files)

    def has(self, name: str) -> bool:
        return name in self._files

    def get(self, name: str) -> dict | None:
        return self._files.get(name)

    def all(self) -> dict[str, dict]:
        return dict(self._files)

    def record(
        self,
        name: str,
        *,
        relpath: str,
        gcs_uri: str,
        size_bytes: int | None,
        generation: str | None,
        updated: str | None,
        user: str | None,
        job_id: str | None,
    ) -> None:
        self._files[name] = {
            "relpath": relpath,
            "gcs_uri": gcs_uri,
            "size_bytes": size_bytes,
            # A geração detecta blob substituído no bucket: mesmo nome, conteúdo
            # novo. Sem ela, um arquivo corrigido na origem nunca seria rebaixado.
            "gcs_generation": generation,
            "gcs_updated": updated,
            "downloaded_at": iso(),
            "downloaded_by": user,
            "job_id": job_id,
        }

    def forget(self, name: str) -> None:
        self._files.pop(name, None)

    def save(self, source_uri: str | None = None) -> None:
        _write_atomic(
            self.path,
            {
                "schema_version": SCHEMA_VERSION,
                "object_id": self.object_id,
                "source_uri": source_uri,
                "updated_at": iso(),
                "files": self._files,
            },
        )


# --------------------------------------------------------------------------
# exclusões
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Pattern:
    kind: str  # "substring" | "regex"
    value: str
    case_sensitive: bool = False

    def to_json(self) -> dict:
        return {
            "kind": self.kind,
            "value": self.value,
            "case_sensitive": self.case_sensitive,
        }


class InvalidPattern(ValueError):
    pass


def compile_patterns(raw: list[dict]) -> list[tuple[Pattern, re.Pattern | None]]:
    """Valida e compila. Regex quebrada precisa falhar no PUT das regras, não às
    três da manhã dentro de um job de download."""
    compiled: list[tuple[Pattern, re.Pattern | None]] = []
    for item in raw or []:
        kind = (item.get("kind") or "substring").lower()
        value = item.get("value") or ""
        if not value:
            raise InvalidPattern("padrão vazio")
        if kind not in ("substring", "regex"):
            raise InvalidPattern(f"tipo de padrão desconhecido: {kind}")
        case_sensitive = bool(item.get("case_sensitive"))
        pattern = Pattern(kind, value, case_sensitive)
        if kind == "regex":
            try:
                flags = 0 if case_sensitive else re.IGNORECASE
                compiled.append((pattern, re.compile(value, flags)))
            except re.error as exc:
                raise InvalidPattern(f"regex inválida '{value}': {exc}") from exc
        else:
            compiled.append((pattern, None))
    return compiled


def match_pattern(
    name: str, compiled: list[tuple[Pattern, re.Pattern | None]]
) -> Pattern | None:
    """Primeiro padrão que casa com o NOME DO ARQUIVO (não o caminho)."""
    for pattern, regex in compiled:
        if regex is not None:
            if regex.search(name):
                return pattern
        else:
            haystack = name if pattern.case_sensitive else name.lower()
            needle = pattern.value if pattern.case_sensitive else pattern.value.lower()
            if needle in haystack:
                return pattern
    return None


class ExclusionList:
    """Arquivos descartados, chaveados pelo nome — nunca apagados, só movidos."""

    def __init__(self, path: Path, object_id: str) -> None:
        self.path = path
        self.object_id = object_id
        self._files: dict[str, dict] = _read(path)

    def count(self) -> int:
        return len(self._files)

    def has(self, name: str) -> bool:
        return name in self._files

    def all(self) -> dict[str, dict]:
        return dict(self._files)

    def record(
        self,
        name: str,
        *,
        relpath: str,
        reason: str,
        pattern: Pattern | None,
        user: str | None,
        moved_to: str | None,
        size_bytes: int | None = None,
        source_uri: str | None = None,
        note: str = "",
    ) -> None:
        self._files[name] = {
            "relpath": relpath,
            "reason": reason,
            "pattern": pattern.to_json() if pattern else None,
            "excluded_at": iso(),
            "excluded_by": user,
            "moved_to": moved_to,
            "size_bytes": size_bytes,
            "source_uri": source_uri,
            "note": note,
        }

    def restore(self, name: str) -> dict | None:
        return self._files.pop(name, None)

    def save(self) -> None:
        _write_atomic(
            self.path,
            {
                "schema_version": SCHEMA_VERSION,
                "object_id": self.object_id,
                "updated_at": iso(),
                "files": self._files,
            },
        )
