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

from .pipeline_projection import get_many


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


def _load_annotation_store_read_only(ctx) -> bool:
    """Load annotations without AnnotationStore's corrupt-file backup write."""

    path = ctx.annotations_path
    invalid = False
    if path.exists():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError("annotations root must be an object")
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            document = {"schema_version": 2, "videos": {}, "counts": {}}
            invalid = True
    else:
        document = {"schema_version": 2, "videos": {}, "counts": {}}
    document.setdefault("videos", {})
    document.setdefault("counts", {})
    with ctx.store._state_lock:
        ctx.store._doc = document
        ctx.store.loaded = True
    return invalid


def _pending_by_object(*, database_url: str) -> dict[str, set[str]]:
    from .pipeline_projection import pending_intents

    found: dict[str, set[str]] = defaultdict(set)
    seen: set[int] = set()
    after = 0
    while True:
        batch = pending_intents(
            limit=1000, after_event_seq=after, database_url=database_url
        )
        fresh = [intent for intent in batch if intent.event_seq not in seen]
        if not fresh:
            break
        for intent in fresh:
            seen.add(intent.event_seq)
            found[intent.object_id].add(intent.video_id)
        after = max(intent.event_seq for intent in fresh)
    return found


def collect_candidates(
    workspace_root: Path,
    *,
    database_url: str,
    object_ids: list[str] | None = None,
) -> list[ProjectionCandidate]:
    """Read canonical control metadata and build a deterministic inventory."""

    from .pipeline_reconcile import _terminal_source
    from .pipeline_state import PipelineSnapshot, PipelineSource, derive_pipeline_source
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
    configs = [ObjectConfig.from_json(item, workspace_root) for item in raw_objects]
    if selected:
        known = {config.object_id for config in configs}
        missing = sorted(selected - known)
        if missing:
            raise ValueError("objetos desconhecidos: " + ", ".join(missing))
        configs = [config for config in configs if config.object_id in selected]

    pending = _pending_by_object(database_url=database_url)
    queue = PostgresSam3Queue(database_url)
    pipeline_reconcile.sam3_queue = queue
    candidates: list[ProjectionCandidate] = []
    for config in sorted(configs, key=lambda item: item.object_id):
        ctx = ObjectContext(config)
        ctx.index.scan()
        invalid_annotations = _load_annotation_store_read_only(ctx)
        # Prevent reconcile_video from calling ensure_dirs or binding any file
        # queue.  Everything it needs for metadata repair is already loaded.
        ctx._loaded = True
        video_ids = {video.video_id for video in ctx.index.all()}
        video_ids.update(pending.get(config.object_id, set()))
        for video_id in sorted(video_ids):
            video = ctx.index.get(video_id)
            if config.archived or video is None:
                source = _terminal_source(
                    object_id=config.object_id,
                    video_id=video_id,
                    archived=config.archived,
                    present=None if config.archived else False,
                )
            elif invalid_annotations:
                source = PipelineSource(
                    identity={
                        "schema_version": 1,
                        "annotation": {"state": "invalid"},
                        "video": {"video_id": video_id, "present": True},
                    },
                    snapshot=PipelineSnapshot(
                        stage="triage",
                        status="invalid",
                        artifacts_valid=False,
                        validation_status="invalid",
                        inconsistencies=("annotations.json invalido",),
                    ),
                )
            else:
                source = derive_pipeline_source(
                    ctx.store.entry(video.relpath),
                    queue.public(config.object_id, video.relpath),
                    ctx.output_root,
                )
            candidates.append(
                ProjectionCandidate(
                    config.object_id,
                    video_id,
                    source.identity,
                    source.snapshot_dict,
                    ctx,
                    not invalid_annotations,
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.limit <= 1000:
        build_parser().error("--limit deve estar entre 1 e 1000")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("DATABASE_URL ausente", file=sys.stderr)
        return 2
    try:
        candidates = collect_candidates(
            args.workspace.resolve(),
            database_url=database_url,
            object_ids=args.object_ids,
        )
        if args.apply:
            from .pipeline_reconcile import reconcile_video

            def repair(candidate: ProjectionCandidate):
                return reconcile_video(candidate.context, candidate.video_id)
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
