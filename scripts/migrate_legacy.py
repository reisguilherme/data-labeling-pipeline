"""Copy-only inventory and migration journal for the legacy workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _names(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def filter_decisions(workspace: Path) -> dict[str, str]:
    """Carrega a decisão da triagem do bucket sem modificar os arquivos."""
    meta = workspace / "movies-meta"
    keep = _names(meta / "movies-keep.txt")
    trash = _names(meta / "movies-trash.txt")
    duplicate = _names(meta / "movies-trash-duplicados.txt")
    overlap = (keep & trash) | (keep & duplicate) | (trash & duplicate)
    if overlap:
        raise ValueError(f"nomes presentes em mais de uma lista de filtro: {sorted(overlap)[:3]}")
    return {
        **{name: "keep" for name in sorted(keep)},
        **{name: "trash" for name in sorted(trash)},
        **{name: "trash_duplicate" for name in sorted(duplicate)},
    }


def index_existing_objects(client, buckets: tuple[str, ...]) -> dict[str, dict[str, int]]:
    """Build one in-memory size index per bucket instead of one HEAD per file."""
    return {
        bucket: {
            item.object_name: int(item.size or 0)
            for item in client.list_objects(bucket, recursive=True)
        }
        for bucket in buckets
    }


def _segment_root(workspace: Path, object_id: str, export_root: str) -> Path:
    root = Path(export_root)
    if root.is_dir():
        return root
    return workspace / object_id / "dataset" / root.name


def inventory(workspace: Path, object_id: str, *, checksums: bool = False) -> dict:
    annotations_path = workspace / object_id / "dataset" / "annotations.json"
    document = _load(annotations_path)
    annotated_videos = len(document.get("videos") or {})
    raw_root = workspace / object_id / "raw"
    raw_videos = sum(
        1
        for path in raw_root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".mp4", ".mov", ".mxf", ".avi", ".mkv"}
    ) if raw_root.is_dir() else 0
    decisions = filter_decisions(workspace)
    result = {
        "schema_version": 1,
        "object_id": object_id,
        "videos": max(annotated_videos, raw_videos),
        "annotated_videos": annotated_videos,
        "raw_videos": raw_videos,
        "done_videos": 0,
        "segments": 0,
        "frames": 0,
        "prompts": 0,
        "legacy_reviews": 0,
        "review_files": 0,
        "mask_files": 0,
        "filter_counts": {
            "keep": sum(value == "keep" for value in decisions.values()),
            "trash": sum(value == "trash" for value in decisions.values()),
            "trash_duplicate": sum(value == "trash_duplicate" for value in decisions.values()),
        },
        "invalid": [],
        "checksums": {},
    }
    for relpath, entry in (document.get("videos") or {}).items():
        if entry.get("status") == "done":
            result["done_videos"] += 1
        exported = entry.get("export") or {}
        if not exported.get("root"):
            continue
        root = _segment_root(workspace, object_id, exported["root"])
        for segment_name in exported.get("segments") or []:
            result["segments"] += 1
            segment = root / segment_name
            frames = sorted(segment.glob("*.jpg"))
            result["frames"] += len(frames)
            expected = next(
                (
                    int(item.get("frame_count") or 0)
                    for item in entry.get("intervals") or []
                    if item.get("segment") == segment_name
                ),
                0,
            )
            if expected and expected != len(frames):
                result["invalid"].append(
                    f"{relpath}/{segment_name}: {len(frames)} frames, esperado {expected}"
                )
            prompt = segment / "prompt.json"
            if prompt.is_file():
                result["prompts"] += 1
                if checksums:
                    result["checksums"][prompt.relative_to(workspace).as_posix()] = hashlib.sha256(
                        prompt.read_bytes()
                    ).hexdigest()
            else:
                result["invalid"].append(f"{relpath}/{segment_name}: prompt.json ausente")
            review = segment / "_sam3" / "review.json"
            if review.is_file():
                result["review_files"] += 1
                try:
                    result["legacy_reviews"] += len((_load(review).get("frames") or {}))
                except (OSError, json.JSONDecodeError):
                    result["invalid"].append(f"{relpath}/{segment_name}: review.json invalido")
            masks = list((segment / "_sam3" / "masks").glob("*/*.png"))
            result["mask_files"] += len(masks)
    return result


def _yolo_boxes(path: Path) -> list[tuple[int, list[float]]]:
    boxes = []
    if not path.is_file():
        return boxes
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) != 5:
            continue
        class_index = int(parts[0])
        cx, cy, width, height = (float(value) for value in parts[1:])
        boxes.append(
            (
                class_index,
                [cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2],
            )
        )
    return boxes


def import_database(workspace: Path, object_id: str, database_url: str) -> dict:
    """Idempotent metadata import. It never alters or deletes legacy files."""
    import psycopg

    document = _load(workspace / object_id / "dataset" / "annotations.json")
    annotations = document.get("videos") or {}
    decisions = filter_decisions(workspace)
    raw_root = workspace / object_id / "raw"
    raw_sources = {
        path.relative_to(raw_root).as_posix(): path
        for path in raw_root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".mp4", ".mov", ".mxf", ".avi", ".mkv"}
    } if raw_root.is_dir() else {}
    counters = {
        "videos": 0,
        "intervals": 0,
        "runs": 0,
        "instances": 0,
        "revisions": 0,
        "filter_decisions": 0,
    }
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO projects(slug, name) VALUES (%s, %s)
            ON CONFLICT(slug) DO UPDATE SET name=EXCLUDED.name RETURNING id
            """,
            (object_id, object_id),
        )
        project_id = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO classes(project_id, external_id, name) VALUES (%s, 0, %s)
            ON CONFLICT(project_id, external_id) DO UPDATE SET name=EXCLUDED.name RETURNING id
            """,
            (project_id, object_id),
        )
        class_id = cursor.fetchone()[0]

        decision_sources = {
            "keep": "movies-keep.txt",
            "trash": "movies-trash.txt",
            "trash_duplicate": "movies-trash-duplicados.txt",
        }
        for name, decision in sorted(decisions.items()):
            cursor.execute(
                """
                INSERT INTO video_filter_decisions(project_id,name,decision,source_file)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT(project_id,name) DO UPDATE SET
                    decision=EXCLUDED.decision, source_file=EXCLUDED.source_file
                """,
                (project_id, name, decision, decision_sources[decision]),
            )
            counters["filter_decisions"] += 1

        known_relpaths: set[str] = set()
        for relpath in sorted(raw_sources):
            decision = decisions.get(Path(relpath).name)
            metadata = {
                "legacy": True,
                "filter_status": decision,
                "filter_source": "movies-meta" if decision else None,
                "triage_status": (annotations.get(relpath) or {}).get("status"),
            }
            cursor.execute(
                """
                INSERT INTO videos(project_id, source_uri, blob_key, metadata)
                VALUES (%s,%s,%s,%s::jsonb)
                ON CONFLICT(project_id, source_uri) DO UPDATE SET
                    metadata=videos.metadata || EXCLUDED.metadata
                RETURNING id
                """,
                (project_id, relpath, f"{object_id}/{relpath}", json.dumps(metadata)),
            )
            cursor.fetchone()
            known_relpaths.add(relpath)
            counters["videos"] += 1

        for relpath, entry in annotations.items():
            media = entry.get("media") or {}
            decision = decisions.get(Path(relpath).name)
            cursor.execute(
                """
                INSERT INTO videos(project_id, source_uri, blob_key, width, height,
                                   frame_count, metadata)
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
                ON CONFLICT(project_id, source_uri) DO UPDATE SET
                    width=COALESCE(EXCLUDED.width, videos.width),
                    height=COALESCE(EXCLUDED.height, videos.height),
                    frame_count=COALESCE(EXCLUDED.frame_count, videos.frame_count),
                    metadata=videos.metadata || EXCLUDED.metadata
                RETURNING id
                """,
                (
                    project_id,
                    relpath,
                    f"{object_id}/{relpath}",
                    media.get("width"),
                    media.get("height"),
                    media.get("frame_count"),
                    json.dumps(
                        {
                            "legacy": True,
                            "status": entry.get("status"),
                            "triage_status": entry.get("status"),
                            "filter_status": decision,
                            "filter_source": "movies-meta" if decision else None,
                        }
                    ),
                ),
            )
            video_id = cursor.fetchone()[0]
            if relpath not in known_relpaths:
                counters["videos"] += 1
                known_relpaths.add(relpath)
            exported = entry.get("export") or {}
            if not exported.get("root"):
                continue
            root = _segment_root(workspace, object_id, exported["root"])
            intervals = {item.get("segment"): item for item in entry.get("intervals") or []}
            for segment_name in exported.get("segments") or []:
                interval = intervals.get(segment_name) or {}
                cursor.execute(
                    """
                    INSERT INTO intervals(video_id,start_frame,end_frame,flags)
                    VALUES (%s,%s,%s,%s::jsonb)
                    ON CONFLICT(video_id,start_frame,end_frame) DO UPDATE SET flags=EXCLUDED.flags
                    RETURNING id
                    """,
                    (
                        video_id,
                        int(interval.get("start_frame") or 0),
                        int(interval.get("end_frame") or 0),
                        json.dumps(interval.get("flags") or {}),
                    ),
                )
                interval_id = cursor.fetchone()[0]
                counters["intervals"] += 1
                segment = root / segment_name
                prompt_path = segment / "prompt.json"
                prompt_raw = _load(prompt_path)
                prompt_digest = hashlib.sha256(prompt_path.read_bytes()).hexdigest()
                cursor.execute(
                    """
                    INSERT INTO prompts(interval_id,frame_idx,objects,digest,created_by)
                    VALUES (%s,%s,%s::jsonb,%s,'legacy-import')
                    ON CONFLICT(interval_id,digest) DO UPDATE SET objects=EXCLUDED.objects
                    RETURNING id
                    """,
                    (
                        interval_id,
                        int(prompt_raw.get("prompt_frame_idx") or 0),
                        json.dumps(prompt_raw.get("objects") or []),
                        prompt_digest,
                    ),
                )
                prompt_id = cursor.fetchone()[0]
                frame_count = int(interval.get("frame_count") or len(list(segment.glob("*.jpg"))))
                labels_dir = segment / "_sam3" / "labels"
                if not labels_dir.is_dir():
                    continue
                cursor.execute(
                    """
                    INSERT INTO annotation_runs(interval_id,prompt_id,kind,prompt_digest,
                                                parameters,state,expected_frames,completed_frames,
                                                finished_at)
                    VALUES (%s,%s,'legacy_detection',%s,'{"legacy":true}'::jsonb,
                            'complete',%s,%s,now())
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    (interval_id, prompt_id, prompt_digest, frame_count, frame_count),
                )
                inserted = cursor.fetchone()
                if inserted:
                    run_id = inserted[0]
                    counters["runs"] += 1
                    for frame_idx in range(frame_count):
                        boxes = _yolo_boxes(labels_dir / f"{frame_idx:06d}.txt")
                        if not boxes:
                            cursor.execute(
                                """
                                INSERT INTO frame_instances(run_id,frame_idx,object_id,class_id,is_empty)
                                VALUES (%s,%s,1,%s,TRUE) ON CONFLICT DO NOTHING
                                """,
                                (run_id, frame_idx, class_id),
                            )
                            counters["instances"] += cursor.rowcount
                        for position, (_, bbox) in enumerate(boxes, start=1):
                            cursor.execute(
                                """
                                INSERT INTO frame_instances(run_id,frame_idx,object_id,class_id,bbox)
                                VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                                """,
                                (run_id, frame_idx, position, class_id, bbox),
                            )
                            counters["instances"] += cursor.rowcount
                    review_path = segment / "_sam3" / "review.json"
                    if review_path.is_file():
                        for frame_text, review in (_load(review_path).get("frames") or {}).items():
                            cursor.execute(
                                """
                                INSERT INTO revisions(run_id,frame_idx,revision,status,created_by)
                                VALUES (%s,%s,1,%s,'legacy-import') ON CONFLICT DO NOTHING
                                """,
                                (run_id, int(frame_text), review.get("status", "ok")),
                            )
                            counters["revisions"] += cursor.rowcount
    return counters


def copy_blobs(workspace: Path, object_id: str) -> dict:
    """Idempotent copy to MinIO; source files are opened read-only."""
    from minio import Minio
    client = Minio(
        os.environ.get("MINIO_ENDPOINT", "minio:9000"),
        access_key=os.environ["MINIO_ROOT_USER"],
        secret_key=os.environ["MINIO_ROOT_PASSWORD"],
        secure=os.environ.get("MINIO_SECURE", "false").lower() == "true",
    )
    counts = {"videos": 0, "frames": 0, "masks": 0, "skipped": 0}
    existing = index_existing_objects(client, ("videos", "frames", "masks"))
    processed = 0

    def put(bucket: str, key: str, source: Path) -> None:
        nonlocal processed
        size = source.stat().st_size
        if existing[bucket].get(key) == size:
            counts["skipped"] += 1
            processed += 1
            if processed % 250 == 0:
                print(f"blobs: {processed} verificados", file=sys.stderr, flush=True)
            return
        client.fput_object(bucket, key, str(source))
        counts[bucket] += 1
        existing[bucket][key] = size
        processed += 1
        if processed % 250 == 0:
            print(f"blobs: {processed} processados", file=sys.stderr, flush=True)

    raw_root = workspace / object_id / "raw"
    extensions = {".mp4", ".mov", ".mxf", ".avi", ".mkv"}
    for source in raw_root.rglob("*") if raw_root.is_dir() else []:
        if source.is_file() and source.suffix.lower() in extensions:
            put("videos", f"{object_id}/{source.relative_to(raw_root).as_posix()}", source)

    document = _load(workspace / object_id / "dataset" / "annotations.json")
    for entry in (document.get("videos") or {}).values():
        exported = entry.get("export") or {}
        if not exported.get("root"):
            continue
        root = _segment_root(workspace, object_id, exported["root"])
        for segment_name in exported.get("segments") or []:
            segment = root / segment_name
            prefix = f"{object_id}/{root.name}/{segment_name}"
            for frame in segment.glob("*.jpg"):
                put("frames", f"{prefix}/{frame.name}", frame)
            for mask in (segment / "_sam3" / "masks").glob("*/*.png"):
                put("masks", f"{prefix}/{mask.relative_to(segment / '_sam3').as_posix()}", mask)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--object", default="boom")
    parser.add_argument("--checksums", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--copy-blobs", action="store_true")
    args = parser.parse_args()
    result = inventory(args.workspace.resolve(), args.object, checksums=args.checksums)
    if args.apply:
        if result["invalid"]:
            raise SystemExit("inventario invalido; importacao abortada")
        if not args.database_url:
            raise SystemExit("--database-url ou DATABASE_URL obrigatorio com --apply")
        result["database_import"] = import_database(
            args.workspace.resolve(), args.object, args.database_url
        )
        if args.copy_blobs:
            result["blob_import"] = copy_blobs(args.workspace.resolve(), args.object)
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(serialized, encoding="utf-8")
    print(serialized)
    return 1 if result["invalid"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
