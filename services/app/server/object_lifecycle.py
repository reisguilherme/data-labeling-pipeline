"""Regras e operações seguras do ciclo de vida de objetos/classes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def normalize_label(value: str) -> str:
    return value.strip().casefold()


def validate_unique_label(
    items: Iterable[tuple[str, str, bool]],
    *,
    object_id: str,
    label: str,
) -> None:
    wanted = normalize_label(label)
    if not wanted:
        raise ValueError("nome da classe é obrigatório")
    for current_id, current_label, archived in items:
        if current_id == object_id or archived:
            continue
        if normalize_label(current_label) == wanted:
            raise ValueError(f"classe já usada pelo objeto {current_id}")


def validate_purge_confirmation(object_id: str, confirmation: str) -> None:
    if confirmation != f"EXCLUIR {object_id}":
        raise ValueError("confirmação de exclusão inválida")


def partition_managed_paths(
    workspace_root: Path,
    paths: Iterable[Path],
) -> tuple[list[Path], list[Path]]:
    """Separa alvos confinados ao workspace; nunca aceita a raiz em si."""
    root = workspace_root.resolve()
    managed: list[Path] = []
    skipped: list[Path] = []
    for original in paths:
        resolved = original.resolve()
        if resolved != root and resolved.is_relative_to(root):
            managed.append(original)
        else:
            skipped.append(original)
    return managed, skipped


def _lexical(path: Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.fspath(_lexical(left))) == os.path.normcase(
        os.fspath(_lexical(right))
    )


def _overlaps(left: Path, right: Path) -> bool:
    left = left.resolve()
    right = right.resolve()
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _contains_symlink(workspace_root: Path, candidate: Path) -> bool:
    root = _lexical(workspace_root)
    path = _lexical(candidate)
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _exclusive_default_root(
    workspace_root: Path,
    object_id: str,
    candidate: Path,
    kind: str,
    registered_roots: Iterable[tuple[str, Path]],
) -> bool:
    root = workspace_root.resolve()
    expected = root / object_id / kind
    if not _same_path(candidate, expected):
        return False
    if _contains_symlink(root, candidate):
        return False
    resolved = candidate.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        return False
    for registered_object, registered in registered_roots:
        if registered_object != object_id and _overlaps(resolved, registered):
            return False
    return True


def validate_new_object_roots(
    workspace_root: Path,
    object_id: str,
    videos_root: Path,
    output_root: Path,
    *,
    registered_roots: Iterable[tuple[str, Path]] = (),
) -> tuple[Path, Path]:
    """Novos objetos sempre possuem roots exclusivos no layout portatil."""
    root = workspace_root.resolve()
    registered = tuple(registered_roots)
    if not _exclusive_default_root(
        root, object_id, videos_root, "raw", registered
    ) or not _exclusive_default_root(
        root, object_id, output_root, "dataset", registered
    ):
        raise ValueError(
            "raizes do objeto devem usar o layout exclusivo "
            f"{object_id}/raw e {object_id}/dataset dentro do workspace"
        )
    return root / object_id / "raw", root / object_id / "dataset"


def partition_owned_object_paths(
    workspace_root: Path,
    object_id: str,
    paths: Iterable[Path],
    *,
    registered_roots: Iterable[tuple[str, Path]] = (),
) -> tuple[list[Path], list[Path]]:
    """Seleciona apenas os dois roots exclusivos que um purge pode remover."""
    root = workspace_root.resolve()
    registered = tuple(registered_roots)
    managed: list[Path] = []
    skipped: list[Path] = []
    for path in paths:
        kind = "raw" if _same_path(path, root / object_id / "raw") else (
            "dataset" if _same_path(path, root / object_id / "dataset") else None
        )
        if kind is not None and _exclusive_default_root(
            root, object_id, path, kind, registered
        ):
            managed.append(path)
        else:
            skipped.append(path)
    return managed, skipped


def active_durable_jobs(database_url: str | None, object_id: str) -> int:
    if not database_url:
        return 0
    import psycopg

    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT count(*) FROM jobs
             WHERE payload->>'object_id'=%s
               AND state IN ('queued','leased','running')
            """,
            (object_id,),
        )
        return int(cursor.fetchone()[0])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_purge_inventory(
    *,
    workspace_root: Path,
    object_id: str,
    roots: Iterable[Path],
    skipped_paths: Iterable[Path],
) -> tuple[Path, dict]:
    files: list[dict] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                files.append(
                    {
                        "path": path.relative_to(workspace_root).as_posix(),
                        "bytes": path.stat().st_size,
                        "sha256": _sha256(path),
                    }
                )
    now = datetime.now(timezone.utc)
    payload = {
        "schema_version": 1,
        "object_id": object_id,
        "created_at": now.isoformat(),
        "files": files,
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files),
        "skipped_external_paths": [str(path) for path in skipped_paths],
    }
    backup_dir = workspace_root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"purge-{object_id}-{now.strftime('%Y%m%dT%H%M%SZ')}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)
    return target, payload


def preserve_exported_datasets(workspace_root: Path, object_id: str, roots: Iterable[Path]) -> list[str]:
    preserved: list[str] = []
    destination = workspace_root / "_datasets" / "legacy" / object_id
    for root in roots:
        source = root / "_datasets"
        if not source.is_dir():
            continue
        destination.mkdir(parents=True, exist_ok=True)
        for dataset in source.iterdir():
            target = destination / dataset.name
            if target.exists():
                raise RuntimeError(f"dataset preservado já existe: {target}")
            shutil.move(str(dataset), str(target))
            preserved.append(target.as_posix())
    return preserved


def remove_minio_object_prefix(store, object_id: str) -> int:
    removed = 0
    prefix = f"{object_id}/"
    for bucket in ("videos", "frames", "masks"):
        try:
            versions = store.client.list_objects(
                bucket, prefix=prefix, recursive=True, include_version=True
            )
            for item in versions:
                store.client.remove_object(
                    bucket,
                    item.object_name,
                    version_id=getattr(item, "version_id", None),
                )
                removed += 1
        except TypeError:
            # Compatibilidade com clientes MinIO anteriores sem include_version.
            for item in store.client.list_objects(bucket, prefix=prefix, recursive=True):
                store.client.remove_object(bucket, item.object_name)
                removed += 1
    return removed


def delete_project_records(database_url: str, object_id: str, *, actor: str | None) -> dict:
    import psycopg

    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id FROM projects WHERE slug=%s FOR UPDATE", (object_id,))
        row = cursor.fetchone()
        if row is None:
            return {"project_deleted": False}
        project_id = row[0]
        cursor.execute("SET LOCAL pipeline.allow_purge = 'on'")
        cursor.execute("SELECT id FROM videos WHERE project_id=%s", (project_id,))
        video_ids = [row[0] for row in cursor.fetchall()]
        cursor.execute("SELECT id FROM intervals WHERE video_id=ANY(%s)", (video_ids,))
        interval_ids = [row[0] for row in cursor.fetchall()]
        cursor.execute("SELECT id FROM annotation_runs WHERE interval_id=ANY(%s)", (interval_ids,))
        run_ids = [row[0] for row in cursor.fetchall()]
        cursor.execute("SELECT id FROM revisions WHERE run_id=ANY(%s)", (run_ids,))
        revision_ids = [row[0] for row in cursor.fetchall()]

        cursor.execute("DELETE FROM revision_instances WHERE revision_id=ANY(%s)", (revision_ids,))
        cursor.execute("DELETE FROM revisions WHERE run_id=ANY(%s)", (run_ids,))
        cursor.execute("DELETE FROM frame_instances WHERE run_id=ANY(%s)", (run_ids,))
        cursor.execute("DELETE FROM artifacts WHERE run_id=ANY(%s)", (run_ids,))
        cursor.execute("DELETE FROM annotation_runs WHERE interval_id=ANY(%s)", (interval_ids,))
        cursor.execute("DELETE FROM prompts WHERE interval_id=ANY(%s)", (interval_ids,))
        cursor.execute("DELETE FROM intervals WHERE video_id=ANY(%s)", (video_ids,))
        cursor.execute("DELETE FROM videos WHERE project_id=%s", (project_id,))
        cursor.execute("DELETE FROM classes WHERE project_id=%s", (project_id,))
        cursor.execute("UPDATE exports SET project_id=NULL WHERE project_id=%s", (project_id,))
        cursor.execute("UPDATE jobs SET project_id=NULL WHERE project_id=%s", (project_id,))
        cursor.execute("UPDATE audit_events SET project_id=NULL WHERE project_id=%s", (project_id,))
        cursor.execute("DELETE FROM projects WHERE id=%s", (project_id,))
        cursor.execute(
            """
            INSERT INTO audit_events(actor,event,entity_type,entity_id,details)
            VALUES (%s,'object_purged','object',%s,%s::jsonb)
            """,
            (actor, object_id, json.dumps({"object_id": object_id})),
        )
    return {"project_deleted": True}
