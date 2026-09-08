"""Declarative, checksum-verified SAM3 model registry."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class CheckpointIntegrityError(RuntimeError):
    """Checkpoint bytes do not match the immutable registry entry."""


@dataclass(frozen=True)
class ModelSource:
    type: str
    path: Path | None = None
    repo_id: str | None = None
    filename: str | None = None
    revision: str | None = None


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    source: ModelSource
    sha256: str
    sam3_commit: str


class ModelRegistry:
    def __init__(
        self,
        *,
        models: dict[str, ModelSpec],
        default_model: str,
        object_assignments: dict[str, str],
    ) -> None:
        self.models = models
        self.default_model = default_model
        self.object_assignments = object_assignments

    @classmethod
    def load(cls, path: Path) -> "ModelRegistry":
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if document.get("schema_version") != 1:
            raise ValueError("models.yaml exige schema_version: 1")
        models: dict[str, ModelSpec] = {}
        for raw in document.get("models") or []:
            model_id = str(raw.get("model_id") or "").strip()
            if not model_id or model_id in models:
                raise ValueError(f"model_id ausente ou duplicado: {model_id!r}")
            checksum = str(raw.get("sha256") or "").lower()
            if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
                raise ValueError(f"sha256 invalido para {model_id}")
            source_raw = raw.get("source") or {}
            source_type = source_raw.get("type")
            if source_type == "local":
                source_path = Path(str(source_raw.get("path") or ""))
                if not source_path.is_absolute():
                    source_path = path.parent / source_path
                source = ModelSource(type="local", path=source_path.resolve())
            elif source_type == "huggingface":
                revision = str(source_raw.get("revision") or "").strip()
                if not revision:
                    raise ValueError(f"revision fixa obrigatoria para {model_id}")
                source = ModelSource(
                    type="huggingface",
                    repo_id=str(source_raw.get("repo_id") or ""),
                    filename=str(source_raw.get("filename") or ""),
                    revision=revision,
                )
                if not source.repo_id or not source.filename:
                    raise ValueError(f"repo_id e filename obrigatorios para {model_id}")
            else:
                raise ValueError(f"source.type invalido para {model_id}: {source_type}")
            commit = str(raw.get("sam3_commit") or "").strip()
            if not commit:
                raise ValueError(f"sam3_commit obrigatorio para {model_id}")
            models[model_id] = ModelSpec(model_id, source, checksum, commit)

        default_model = str(document.get("default_model") or "")
        if default_model not in models:
            raise ValueError("default_model nao existe no registro")
        assignments = {
            str(object_id): str(model_id)
            for object_id, model_id in ((document.get("assignments") or {}).get("objects") or {}).items()
        }
        unknown = sorted(set(assignments.values()) - set(models))
        if unknown:
            raise ValueError(f"assignments referenciam modelos inexistentes: {unknown}")
        return cls(
            models=models,
            default_model=default_model,
            object_assignments=assignments,
        )

    def select(self, object_id: str) -> ModelSpec:
        return self.models[self.object_assignments.get(object_id, self.default_model)]

    def resolve_local(self, model_id: str) -> Path:
        model = self.models[model_id]
        if model.source.type != "local" or model.source.path is None:
            raise ValueError(f"modelo {model_id} nao e uma fonte local")
        if not model.source.path.is_file():
            raise FileNotFoundError(model.source.path)
        digest = _sha256_file(model.source.path)
        if digest != model.sha256:
            raise CheckpointIntegrityError(
                f"sha256 incorreto para {model_id}: esperado {model.sha256}, obtido {digest}"
            )
        return model.source.path

    def resolve_checkpoint(self, model_id: str, *, cache_dir: Path) -> Path:
        """Resolve local/Hugging Face sources into a verified local file."""
        model = self.models[model_id]
        if model.source.type == "local":
            return self.resolve_local(model_id)
        from huggingface_hub import hf_hub_download

        downloaded = Path(
            hf_hub_download(
                repo_id=model.source.repo_id,
                filename=model.source.filename,
                revision=model.source.revision,
                cache_dir=cache_dir,
            )
        ).resolve()
        digest = _sha256_file(downloaded)
        if digest != model.sha256:
            raise CheckpointIntegrityError(
                f"sha256 incorreto para {model_id}: esperado {model.sha256}, obtido {digest}"
            )
        return downloaded
