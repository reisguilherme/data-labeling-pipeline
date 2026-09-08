"""MinIO artifact storage with deterministic, confined object keys."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def object_key_for(root: Path, artifact: Path) -> str:
    resolved_root = root.resolve()
    resolved_artifact = artifact.resolve()
    try:
        return resolved_artifact.relative_to(resolved_root).as_posix()
    except ValueError as exc:
        raise ValueError(f"artefato fora da raiz permitida: {resolved_artifact}") from exc


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    key: str
    etag: str
    version_id: str | None


class MinioBlobStore:
    def __init__(self, client) -> None:
        self.client = client

    @classmethod
    def from_env(cls) -> "MinioBlobStore | None":
        endpoint = os.environ.get("MINIO_ENDPOINT")
        access_key = os.environ.get("MINIO_ROOT_USER")
        secret_key = os.environ.get("MINIO_ROOT_PASSWORD")
        if not endpoint or not access_key or not secret_key:
            return None
        from minio import Minio

        return cls(
            Minio(
                endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=os.environ.get("MINIO_SECURE", "false").lower() == "true",
            )
        )

    def put_file(self, bucket: str, key: str, path: Path) -> StoredObject:
        normalized = key.replace("\\", "/").lstrip("/")
        if not normalized or any(part in ("", ".", "..") for part in normalized.split("/")):
            raise ValueError(f"object key invalida: {key!r}")
        result = self.client.fput_object(bucket, normalized, str(path))
        return StoredObject(
            bucket=bucket,
            key=normalized,
            etag=result.etag,
            version_id=getattr(result, "version_id", None),
        )

    def put_file_if_absent(self, bucket: str, key: str, path: Path) -> StoredObject:
        """Sincroniza um blob imutavel sem reenviar arquivos grandes.

        Deve ser usado somente quando a chave ja inclui o digest do conteudo,
        como checkpoints em ``models/<id>/<sha256>/...``. Uma chave existente
        com tamanho diferente indica corrupcao ou violacao da imutabilidade.
        """
        normalized = key.replace("\\", "/").lstrip("/")
        if not normalized or any(part in ("", ".", "..") for part in normalized.split("/")):
            raise ValueError(f"object key invalida: {key!r}")
        try:
            existing = self.client.stat_object(bucket, normalized)
        except Exception as exc:  # o tipo S3Error so existe quando minio esta instalado
            if getattr(exc, "code", None) not in {"NoSuchKey", "NoSuchObject", "NotFound"}:
                raise
        else:
            local_size = path.stat().st_size
            if existing.size != local_size:
                raise RuntimeError(
                    f"objeto imutavel {bucket}/{normalized} tem tamanho inesperado: "
                    f"{existing.size}, esperado {local_size}"
                )
            return StoredObject(
                bucket=bucket,
                key=normalized,
                etag=existing.etag,
                version_id=getattr(existing, "version_id", None),
            )
        return self.put_file(bucket, normalized, path)
