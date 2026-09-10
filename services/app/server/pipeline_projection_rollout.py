"""Read-only inventory and explicit, bounded repair of pipeline projections."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .pipeline_projection import apply_intent, get_many, persisted_keys, reserve_repair_intent


@dataclass(frozen=True)
class ProjectionCandidate:
    object_id: str
    video_id: str
    source_identity: dict[str, Any]
    snapshot: dict[str, Any]
    context: Any = None
    repairable: bool = True


def _health(value: Any) -> tuple[bool, bool]:
    legacy = False
    invalid = False
    if isinstance(value, dict):
        state = value.get("state")
        legacy = state == "legacy"
        invalid = state == "invalid"
        for child in value.values():
            child_legacy, child_invalid = _health(child)
            legacy = legacy or child_legacy
            invalid = invalid or child_invalid
    elif isinstance(value, list):
        for child in value:
            child_legacy, child_invalid = _health(child)
            legacy = legacy or child_legacy
            invalid = invalid or child_invalid
    return legacy, invalid


def _encode_resume(key: tuple[str, str]) -> str:
    raw = json.dumps(key, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_resume(token: str | None) -> tuple[str, str] | None:
    if token is None:
        return None
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        value = json.loads(raw.decode("ascii"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("resume token invalido") from exc
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ValueError("resume token invalido")
    return value[0], value[1]


def run_batch(
    candidates: Iterable[ProjectionCandidate],
    *,
    limit: int = 100,
    resume_token: str | None = None,
    apply: bool = False,
    repair: Callable[[ProjectionCandidate], Any] | None = None,
    database_url: str | None = None,
    connect=None,
) -> dict[str, Any]:
    """Classify every candidate, then optionally repair one bounded page."""

    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit deve estar entre 1 e 1000")
    ordered = sorted(candidates, key=lambda item: (item.object_id, item.video_id))
    grouped: dict[str, list[ProjectionCandidate]] = defaultdict(list)
    for candidate in ordered:
        grouped[candidate.object_id].append(candidate)

    records = {}
    for object_id, items in grouped.items():
        records.update(
            {
                (object_id, video_id): record
                for video_id, record in get_many(
                    object_id,
                    [item.video_id for item in items],
                    database_url=database_url,
                    connect=connect,
                ).items()
            }
        )

    counts = Counter({name: 0 for name in ("current", "stale", "missing", "pending", "legacy", "invalid")})
    repairs: list[ProjectionCandidate] = []
    for candidate in ordered:
        record = records.get((candidate.object_id, candidate.video_id))
        if record is None:
            state = "missing"
        elif record.projection_status == "pending":
            state = "pending"
        elif (
            record.projection_status != "current"
            or record.source_identity != candidate.source_identity
            or record.snapshot != candidate.snapshot
        ):
            state = "stale"
        else:
            state = "current"
        counts[state] += 1
        legacy, invalid = _health(candidate.source_identity)
        counts["legacy"] += int(legacy)
        counts["invalid"] += int(invalid)
        if state != "current" and candidate.repairable:
            repairs.append(candidate)

    after = _decode_resume(resume_token)
    remaining = [item for item in repairs if after is None or (item.object_id, item.video_id) > after]
    batch = remaining[:limit]
    next_token = (
        _encode_resume((batch[-1].object_id, batch[-1].video_id))
        if batch and len(remaining) > len(batch)
        else None
    )
    applied = 0
    if apply:
        if repair is None:
            raise ValueError("repair callback obrigatorio com --apply")
        for candidate in batch:
            repair(candidate)
            applied += 1

    return {
        "mode": "apply" if apply else "dry-run",
        "counts": dict(counts),
        "repair_candidates": len(repairs),
        "batch_size": len(batch),
        "applied": applied,
        "resume_token": next_token,
    }


def _unregistered_source(
    object_id: str,
    video_id: str,
    *,
    state: str,
):
    from .pipeline_state import PipelineSnapshot, PipelineSource

    return PipelineSource(
        identity={
            "schema_version": 1,
            "object": {"object_id": object_id, "state": state},
            "video": {"video_id": video_id, "present": None},
        },
        snapshot=PipelineSnapshot(
            stage="triage",
            status=state,
            artifacts_valid=False,
            validation_status="invalid",
            inconsistencies=(f"objeto {state} no registro canonico",),
        ),
    )


def collect_candidates(
    workspace_root: Path,
    *,
    database_url: str,
    object_ids: list[str] | None = None,
) -> list[ProjectionCandidate]:
    """Read canonical control metadata and build a deterministic inventory."""

    from .pipeline_reconcile import derive_read_only_pipeline_source
    from .sam3_postgres import PostgresSam3Queue
    from .workspace import ObjectConfig, ObjectContext
    from . import pipeline_reconcile

    registry = workspace_root / "objects.json"
    try:
        payload = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"registro de objetos invalido: {registry}") from exc
    raw_objects = payload.get("objects") if isinstance(payload, dict) else None
    if not isinstance(raw_objects, list):
        raise RuntimeError(f"registro de objetos invalido: {registry}")
    selected = set(object_ids or ())
    configs_by_id = {}
    invalid_object_ids: set[str] = set()
    for item in raw_objects:
        raw_id = (
            str(item.get("object_id") or "").strip()
            if isinstance(item, dict)
            else ""
        )
        try:
            config = ObjectConfig.from_json(item, workspace_root)
        except (KeyError, TypeError, ValueError):
            if raw_id:
                invalid_object_ids.add(raw_id)
            continue
        configs_by_id[config.object_id] = config
    if selected:
        known = set(configs_by_id) | invalid_object_ids
        missing = sorted(selected - known)
        if missing:
            raise ValueError("objetos desconhecidos: " + ", ".join(missing))
        configs_by_id = {
            object_id: config
            for object_id, config in configs_by_id.items()
            if object_id in selected
        }
        invalid_object_ids.intersection_update(selected)

    persisted_by_object: dict[str, set[str]] = defaultdict(set)
    for object_id, video_id in persisted_keys(database_url=database_url):
        if not selected or object_id in selected:
            persisted_by_object[object_id].add(video_id)
    queue = PostgresSam3Queue(database_url)
    pipeline_reconcile.sam3_queue = queue
    candidates: list[ProjectionCandidate] = []
    for config in sorted(configs_by_id.values(), key=lambda item: item.object_id):
        ctx = ObjectContext(config)
        # Missing deployment mounts are operational errors, not canonical
        # evidence that every video was deleted.
        ctx._require_registered_roots()
        ctx.index.scan()
        video_ids = {video.video_id for video in ctx.index.all()}
        video_ids.update(persisted_by_object.pop(config.object_id, set()))
        for video_id in sorted(video_ids):
            source = derive_read_only_pipeline_source(ctx, video_id)
            candidates.append(
                ProjectionCandidate(
                    config.object_id,
                    video_id,
                    source.identity,
                    source.snapshot_dict,
                    ctx,
                    True,
                )
            )
    for object_id in sorted(persisted_by_object):
        state = "invalid" if object_id in invalid_object_ids else "missing"
        for video_id in sorted(persisted_by_object[object_id]):
            source = _unregistered_source(object_id, video_id, state=state)
            candidates.append(
                ProjectionCandidate(
                    object_id,
                    video_id,
                    source.identity,
                    source.snapshot_dict,
                )
            )
    return candidates


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reconcile-pipeline-projection",
        description="Inventory pipeline projections; write only with --apply.",
    )
    parser.add_argument("--workspace", type=Path, default=Path("/workspace"))
    parser.add_argument("--object", action="append", dest="object_ids")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--resume-token")
    parser.add_argument("--apply", action="store_true")
    return parser


def verify_projection_schema(database_url: str) -> None:
    """Fail read-only when migrations 005/006 have not been marked applied."""

    import psycopg

    with psycopg.connect(database_url, connect_timeout=5) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT to_regclass('video_pipeline_projection_events')::text,
                       to_regclass('video_pipeline_projection')::text,
                       to_regclass('video_pipeline_projection_barriers')::text,
                       to_regclass('schema_migrations')::text
                """
            )
            tables = cursor.fetchone()
            marked = False
            if tables[3] is not None:
                cursor.execute(
                    """
                    SELECT count(*) = 2 FROM schema_migrations
                     WHERE name IN ('005_video_pipeline_projection.sql',
                                    '006_video_pipeline_projection_barriers.sql')
                    """
                )
                marked = bool(cursor.fetchone()[0])
    if any(name is None for name in tables[:3]) or not marked:
        raise RuntimeError(
            "schema de projecao incompleto: aplique as migrations 005 e 006 "
            "iniciando app/worker antes de executar o reconciliador"
        )


def main(
    argv: list[str] | None = None,
    *,
    database_url_factory: Callable[[], str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.limit <= 1000:
        build_parser().error("--limit deve estar entre 1 e 1000")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url and database_url_factory is not None:
        database_url = database_url_factory()
        os.environ["DATABASE_URL"] = database_url
    if not database_url:
        print("DATABASE_URL ausente", file=sys.stderr)
        return 2
    try:
        verify_projection_schema(database_url)
        candidates = collect_candidates(
            args.workspace.resolve(),
            database_url=database_url,
            object_ids=args.object_ids,
        )
        if args.apply:
            from .pipeline_reconcile import reconcile_video

            def repair(candidate: ProjectionCandidate):
                if candidate.context is not None:
                    return reconcile_video(
                        candidate.context,
                        candidate.video_id,
                        read_only=True,
                    )
                intent, current = reserve_repair_intent(
                    object_id=candidate.object_id,
                    video_id=candidate.video_id,
                    source_identity=candidate.source_identity,
                    snapshot=candidate.snapshot,
                    database_url=database_url,
                )
                if current is not None or intent is None:
                    return current
                return apply_intent(
                    intent,
                    candidate.snapshot,
                    database_url=database_url,
                )
        else:
            repair = None
        report = run_batch(
            candidates,
            limit=args.limit,
            resume_token=args.resume_token,
            apply=args.apply,
            repair=repair,
            database_url=database_url,
        )
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
