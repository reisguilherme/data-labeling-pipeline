"""Configuração do runner, toda por variável de ambiente."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .boxes import DEFAULT_MAX_BOX_FRAC, DEFAULT_MIN_BOX_PX

VERSION = "0.1.0"


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class RunnerConfig:
    workspace: Path = Path("/workspace")

    # Descarte de caixas degeneradas — ver boxes.py.
    min_box_px: int = DEFAULT_MIN_BOX_PX
    max_box_frac: float = DEFAULT_MAX_BOX_FRAC
    mask_threshold: float = 0.0

    # Pedidos de offload. São SONDADOS contra a assinatura real de init_state
    # (ver model.py): a API do SAM3 é gated e não dá para assumir que existem.
    offload_video: bool = True
    offload_state: bool = True

    # 0 = resolução original. Reescalar é o degrau mais barato contra OOM e o
    # mais caro em qualidade: o acervo é de REFLEXOS de boom, e a própria
    # triagem documenta que abaixo de ~1280px o reflexo simplesmente some.
    max_side: int = 0

    # Compatibilidade de API: máscaras agora são canônicas e sempre ligadas.
    save_masks: bool = True

    models_config: Path = Path("/config/models.yaml")
    model_cache: Path = Path("/model-cache")
    model_id: str | None = None
    model_sha256: str | None = None
    sam3_commit: str | None = None

    # Fila (subcomando serve).
    api: str = "http://screening:8000"
    worker_id: str = "sam3-runner-1"
    worker_token: str = ""
    poll_wait: int = 25
    lease_seconds: int = 180

    @classmethod
    def from_env(cls, workspace: Path | None = None) -> "RunnerConfig":
        return cls(
            workspace=workspace or Path(os.environ.get("MST_WORKSPACE", "/workspace")),
            min_box_px=_int("SAM3_MIN_BOX_PX", DEFAULT_MIN_BOX_PX),
            max_box_frac=_float("SAM3_MAX_BOX_FRAC", DEFAULT_MAX_BOX_FRAC),
            mask_threshold=_float("SAM3_MASK_THRESHOLD", 0.0),
            offload_video=_bool("SAM3_OFFLOAD_VIDEO", True),
            offload_state=_bool("SAM3_OFFLOAD_STATE", True),
            max_side=_int("SAM3_MAX_SIDE", 0),
            save_masks=True,
            models_config=Path(os.environ.get("SAM3_MODELS_CONFIG", "/config/models.yaml")),
            model_cache=Path(os.environ.get("SAM3_MODEL_CACHE", "/model-cache")),
            api=os.environ.get("MST_API", "http://screening:8000").rstrip("/"),
            worker_id=os.environ.get("SAM3_WORKER_ID", "sam3-runner-1"),
            worker_token=os.environ.get("MST_WORKER_TOKEN", ""),
            poll_wait=_int("SAM3_POLL_WAIT", 25),
            lease_seconds=_int("SAM3_LEASE_SECONDS", 180),
        )

    def params(self) -> dict:
        """O que entra no marcador. Mudar qualquer um destes invalida o cache —
        é por isso que o dicionário é fechado e ordenado."""
        return {
            "runner_version": VERSION,
            "mask_threshold": self.mask_threshold,
            "min_box_px": self.min_box_px,
            "max_box_frac": self.max_box_frac,
            "max_side": self.max_side,
            "mask_format": "png-1bit-v1",
            "model_id": self.model_id,
            "model_sha256": self.model_sha256,
            "sam3_commit": self.sam3_commit,
        }

    @property
    def classes_path(self) -> Path:
        from .classes import FILENAME

        return self.workspace / FILENAME
