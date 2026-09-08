"""`_sam3/run.json`: o marcador de conclusão de um segmento.

Copia a disciplina de `server/proxy.py` da triagem, e por um motivo que já se
provou lá: os PARÂMETROS da execução vão dentro do marcador. Assim, mudar
`min_box_px` ou o limiar de máscara invalida o cache sozinho — senão um segmento
processado com parâmetros antigos continuaria "pronto" para sempre, e a única
pista seria um dataset silenciosamente inconsistente.

O marcador também guarda o digest do prompt.json: reanotar o vídeo na triagem
gera um prompt novo, e o resultado antigo deixa de valer.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1


def iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


@dataclass
class ObjectReport:
    obj_id: int
    label: str
    class_index: int
    frames: int = 0
    dropped_small: int = 0
    dropped_huge: int = 0


@dataclass
class RunReport:
    status: str = "running"  # running | done | partial | error
    runner_version: str = "0.1.0"
    prompt_digest: str = ""
    started_at: str = field(default_factory=iso)
    finished_at: str | None = None
    duration_sec: float | None = None
    frame_count: int = 0
    frames_written: int = 0
    frames_with_objects: int = 0
    objects: list[ObjectReport] = field(default_factory=list)
    params: dict = field(default_factory=dict)
    model: dict = field(default_factory=dict)
    artifacts: dict = field(default_factory=dict)
    error: str | None = None

    def to_json(self) -> dict:
        data = asdict(self)
        data["schema_version"] = SCHEMA_VERSION
        return data


def write(path: Path, report: RunReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def read(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def is_complete(path: Path, *, prompt_digest: str, frame_count: int, params: dict) -> bool:
    """Este segmento já foi processado com ESTES parâmetros e ESTE prompt?

    Quatro condições, e cada uma cobre uma forma real de resultado obsoleto:
      status done        — não retomar um erro nem um parcial
      prompt_digest      — o vídeo foi reanotado desde então
      params             — o runner mudou de configuração
      frames_written     — o processo morreu no meio da escrita
    """
    data = read(path)
    if not data:
        return False
    if data.get("status") != "done":
        return False
    if data.get("prompt_digest") != prompt_digest:
        return False
    if data.get("frames_written") != frame_count:
        return False
    if params.get("mask_format") == "png-1bit-v1":
        objects = data.get("objects") or []
        artifacts = data.get("artifacts") or {}
        expected = frame_count * len(objects)
        if artifacts.get("files") != expected:
            return False
        if len(artifacts.get("checksums") or {}) != expected:
            return False
    return data.get("params") == params
