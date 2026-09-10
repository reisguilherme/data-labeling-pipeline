"""Normaliza runs SAM3 concluidos no PostgreSQL a partir dos PNGs validados."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pipeline_core.masks import inspect_binary_png
from pipeline_core.sam3_runs import (
    active_segment_output,
    effective_prompt_override_path,
    generation_root,
    load_current_manifest,
    mask_object_key,
)
from pipeline_core.storage import object_key_for


def _effective_prompt(segment_dir: Path) -> tuple[dict, str]:
    prompt_path = segment_dir / "prompt.json"
    raw = prompt_path.read_bytes()
    prompt = json.loads(raw)
    override_path = effective_prompt_override_path(
        segment_dir, migrate_legacy=True
    )
    if override_path is not None:
        override_raw = override_path.read_bytes()
        override = json.loads(override_raw)
        if override.get("objects"):
            prompt["objects"] = override["objects"]
            raw += override_raw
    return prompt, hashlib.sha256(raw).hexdigest()


def _validate_existing_instances(
    expected: dict[tuple[int, int], dict], indexed: list[tuple]
) -> None:
    """Prove an idempotent replay points at the exact same immutable bytes."""
    actual = {(int(row[0]), int(row[1])): row for row in indexed}
    if set(actual) != set(expected):
        raise ValueError("instancias PostgreSQL divergem da geracao SAM3")
    for identity, wanted in expected.items():
        row = actual[identity]
        is_empty = bool(row[2])
        checksum = str(row[3]).strip() if row[3] is not None else None
        object_key = str(row[4]) if row[4] is not None else None
        area_pixels = int(row[5] or 0)
        if is_empty != bool(wanted["is_empty"]):
            raise ValueError(f"estado vazio diverge para frame/objeto {identity}")
        if checksum != wanted.get("sha256"):
            raise ValueError(f"checksum diverge para frame/objeto {identity}")
        if object_key != wanted.get("object_key"):
            raise ValueError(f"object_key diverge para frame/objeto {identity}")
        if area_pixels != int(wanted.get("area_pixels") or 0):
            raise ValueError(f"area diverge para frame/objeto {identity}")


def index_completed_runs(
    connection,
    *,
    object_id: str,
    relpath: str,
    result: dict,
    export_root: str | Path | None = None,
) -> int:
    workspace = Path(os.environ.get("MST_WORKSPACE", "/workspace"))
    segment_runs = result.get("segment_runs") or []
    publication = result.get("publication") or {}
    generation_id = str(result.get("run_id") or "")
    if publication:
        if publication.get("generation_id") != generation_id:
            raise ValueError("identidade da publicacao SAM3 diverge do resultado")
        if int(publication.get("annotation_revision", -1)) != int(
            result.get("annotation_revision", -1)
        ):
            raise ValueError("revisao da publicacao SAM3 diverge do resultado")
    indexed = 0
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO projects(slug,name) VALUES (%s,%s)
            ON CONFLICT(slug) DO UPDATE SET name=EXCLUDED.name RETURNING id
            """,
            (object_id, object_id),
        )
        project_id = cursor.fetchone()[0]

        for summary in segment_runs:
            if summary.get("status") != "done":
                continue
            segment_name = str(summary.get("segment") or "")
            if publication:
                if export_root is None:
                    # Compatibility with results published before generation
                    # manifests became relocatable.
                    legacy_source = summary.get("dir")
                    if not legacy_source:
                        raise ValueError("export_root ausente para publicacao SAM3")
                    root = Path(str(legacy_source)).parent
                else:
                    root = Path(export_root)
                segment_dir = root / segment_name
                output_dir = generation_root(root, generation_id) / segment_name
            else:
                segment_dir = Path(summary["dir"])
                output_dir = segment_dir / "_sam3"
            prompt, digest = _effective_prompt(segment_dir)
            reported_digest = summary.get("prompt_digest")
            if reported_digest != digest:
                raise ValueError(
                    f"digest do prompt mudou em {summary.get('segment')}: "
                    f"runner={reported_digest} disco={digest}"
                )
            width, height = int(prompt["image_width"]), int(prompt["image_height"])
            frame_count = int(prompt["frame_count"])
            start = int(prompt["source_start_frame"])
            end = int(prompt.get("source_end_frame", start + frame_count - 1))
            cursor.execute(
                """
                INSERT INTO videos(project_id,source_uri,blob_key,width,height,frame_count,metadata)
                VALUES (%s,%s,%s,%s,%s,NULL,%s::jsonb)
                ON CONFLICT(project_id,source_uri) DO UPDATE SET
                    width=EXCLUDED.width,height=EXCLUDED.height,
                    metadata=videos.metadata || EXCLUDED.metadata
                RETURNING id
                """,
                (
                    project_id,
                    relpath,
                    f"{object_id}/{relpath}",
                    width,
                    height,
                    json.dumps({"last_segment": prompt.get("segment")}),
                ),
            )
            video_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO intervals(video_id,start_frame,end_frame,flags)
                VALUES (%s,%s,%s,%s::jsonb)
                ON CONFLICT(video_id,start_frame,end_frame) DO UPDATE SET flags=EXCLUDED.flags
                RETURNING id
                """,
                (video_id, start, end, json.dumps(prompt.get("flags") or {})),
            )
            interval_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO prompts(interval_id,frame_idx,objects,digest,created_by)
                VALUES (%s,%s,%s::jsonb,%s,'sam3-worker')
                ON CONFLICT(interval_id,digest) DO UPDATE SET objects=EXCLUDED.objects
                RETURNING id
                """,
                (
                    interval_id,
                    int(prompt["prompt_frame_idx"]),
                    json.dumps(prompt.get("objects") or []),
                    digest,
                ),
            )
            prompt_id = cursor.fetchone()[0]

            model = summary.get("model") or {}
            checkpoint_sha = str(model.get("checkpoint_sha256") or "")
            commit = str(model.get("sam3_commit") or "")
            model_name = str(model.get("model_id") or "")
            run_checkpoint_sha = checkpoint_sha if len(checkpoint_sha) == 64 else None
            run_commit = commit if len(commit) == 40 else None
            model_db_id = None
            if model_name and len(checkpoint_sha) == 64 and len(commit) == 40:
                cursor.execute(
                    """
                    INSERT INTO models(model_id,source,checkpoint_key,checkpoint_sha256,sam3_commit)
                    VALUES (%s,%s::jsonb,%s,%s,%s) ON CONFLICT(model_id) DO NOTHING
                    RETURNING id
                    """,
                    (
                        model_name,
                        json.dumps({"recorded_by": "sam3-worker"}),
                        f"{model_name}/{checkpoint_sha}",
                        checkpoint_sha,
                        commit,
                    ),
                )
                row = cursor.fetchone()
                if row:
                    model_db_id = row[0]
                else:
                    cursor.execute(
                        "SELECT id,checkpoint_sha256,sam3_commit FROM models WHERE model_id=%s",
                        (model_name,),
                    )
                    model_db_id, stored_sha, stored_commit = cursor.fetchone()
                    if stored_sha.strip() != checkpoint_sha or stored_commit.strip() != commit:
                        raise ValueError(f"model_id {model_name} ja existe com outro checkpoint")

            parameters = dict(summary.get("params") or {})
            if publication:
                parameters["_publication"] = {
                    "generation_id": generation_id,
                    "annotation_revision": int(result.get("annotation_revision") or 0),
                    "manifest_sha256": publication.get("manifest_sha256"),
                }

            expected_mask_keys = {
                f"masks/{int(obj['obj_id'])}/{frame_idx:06d}.png"
                for obj in prompt.get("objects") or []
                for frame_idx in range(frame_count)
            }
            reported_checksums = (summary.get("artifacts") or {}).get("checksums")
            if publication and (
                not isinstance(reported_checksums, dict)
                or set(reported_checksums) != expected_mask_keys
            ):
                raise ValueError(
                    f"manifesto de checksums diverge em {summary.get('segment')}"
                )
            expected_instances: dict[tuple[int, int], dict] = {}
            mask_records: dict[tuple[int, int], tuple[Path, object]] = {}
            for prompt_object in prompt.get("objects") or []:
                prompt_obj_id = int(prompt_object["obj_id"])
                for frame_idx in range(frame_count):
                    relative = f"masks/{prompt_obj_id}/{frame_idx:06d}.png"
                    mask = output_dir / Path(relative)
                    info = inspect_binary_png(
                        mask.read_bytes(), expected_size=(width, height)
                    )
                    if publication and info.sha256 != reported_checksums[relative]:
                        raise ValueError(
                            f"checksum publicado diverge para "
                            f"{summary.get('segment')}/{relative}"
                        )
                    key = (
                        None
                        if info.area_pixels == 0
                        else (
                            mask_object_key(info.sha256)
                            if publication
                            else object_key_for(workspace, mask)
                        )
                    )
                    expected_instances[(frame_idx, prompt_obj_id)] = {
                        "is_empty": info.area_pixels == 0,
                        "sha256": None if info.area_pixels == 0 else info.sha256,
                        "object_key": key,
                        "area_pixels": info.area_pixels,
                    }
                    mask_records[(frame_idx, prompt_obj_id)] = (mask, info)
            cursor.execute(
                """
                INSERT INTO annotation_runs(
                    interval_id,prompt_id,model_id,kind,prompt_digest,
                    checkpoint_sha256,sam3_commit,parameters,state,
                    expected_frames,completed_frames,finished_at
                ) VALUES (%s,%s,%s,'sam3',%s,%s,%s,%s::jsonb,'complete',%s,%s,now())
                ON CONFLICT DO NOTHING RETURNING id
                """,
                (
                    interval_id,
                    prompt_id,
                    model_db_id,
                    digest,
                    run_checkpoint_sha,
                    run_commit,
                    json.dumps(parameters),
                    frame_count,
                    int(summary.get("frames_written") or frame_count),
                ),
            )
            row = cursor.fetchone()
            if row:
                run_id = row[0]
            else:
                cursor.execute(
                    """
                    SELECT id FROM annotation_runs
                     WHERE interval_id=%s AND prompt_id=%s AND kind='sam3'
                       AND prompt_digest=%s
                       AND COALESCE(model_id,'00000000-0000-0000-0000-000000000000'::uuid)
                           = COALESCE(%s,'00000000-0000-0000-0000-000000000000'::uuid)
                       AND parameters=%s::jsonb
                    """,
                    (interval_id, prompt_id, digest, model_db_id, json.dumps(parameters)),
                )
                run_id = cursor.fetchone()[0]
                cursor.execute(
                    """
                    SELECT instance.frame_idx, instance.object_id, instance.is_empty,
                           artifact.sha256, artifact.object_key, instance.area_pixels
                      FROM frame_instances instance
                      LEFT JOIN artifacts artifact ON artifact.id=instance.mask_artifact_id
                     WHERE instance.run_id=%s
                    """,
                    (run_id,),
                )
                _validate_existing_instances(expected_instances, cursor.fetchall())
                continue

            for obj in prompt.get("objects") or []:
                obj_id = int(obj["obj_id"])
                label = str(obj["label"])
                class_external = next(
                    (
                        int(item.get("class_index"))
                        for item in summary.get("objects") or []
                        if int(item.get("obj_id")) == obj_id
                    ),
                    obj_id,
                )
                cursor.execute(
                    """
                    INSERT INTO classes(project_id,external_id,name) VALUES (%s,%s,%s)
                    ON CONFLICT(project_id,external_id) DO UPDATE SET name=EXCLUDED.name
                    RETURNING id
                    """,
                    (project_id, class_external, label),
                )
                class_id = cursor.fetchone()[0]
                for frame_idx in range(frame_count):
                    mask, info = mask_records[(frame_idx, obj_id)]
                    if info.area_pixels == 0:
                        cursor.execute(
                            """
                            INSERT INTO frame_instances(run_id,frame_idx,object_id,class_id,is_empty)
                            VALUES (%s,%s,%s,%s,TRUE) ON CONFLICT DO NOTHING
                            """,
                            (run_id, frame_idx, obj_id, class_id),
                        )
                        continue
                    cursor.execute(
                        """
                        INSERT INTO artifacts(run_id,kind,object_key,sha256,byte_size,width,height,state)
                        VALUES (%s,'raw_mask',%s,%s,%s,%s,%s,'valid') RETURNING id
                        """,
                        (
                            run_id,
                            expected_instances[(frame_idx, obj_id)]["object_key"],
                            info.sha256,
                            mask.stat().st_size,
                            width,
                            height,
                        ),
                    )
                    artifact_id = cursor.fetchone()[0]
                    cursor.execute(
                        """
                        INSERT INTO frame_instances(
                            run_id,frame_idx,object_id,class_id,mask_artifact_id,bbox,area_pixels
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING
                        """,
                        (
                            run_id,
                            frame_idx,
                            obj_id,
                            class_id,
                            artifact_id,
                            list(info.bbox_normalized),
                            info.area_pixels,
                        ),
                    )
            indexed += 1
    return indexed


def _index_revision_with_cursor(
    cursor,
    *,
    run_id,
    project_id,
    object_id: str,
    workspace: Path,
    prompt: dict,
    labels: dict[int, str],
    class_ids: dict[str, object],
    annotation_dir: Path,
    frame_idx: int,
    entry: dict,
    user: str | None,
) -> None:
    revision_number = int(entry["revision"])
    cursor.execute(
        "SELECT id,status::text FROM revisions WHERE run_id=%s AND frame_idx=%s AND revision=%s",
        (run_id, frame_idx, revision_number),
    )
    existing = cursor.fetchone()
    if existing:
        if existing[1] != entry["status"]:
            raise RuntimeError("revisao PostgreSQL existente tem status diferente")
        return

    cursor.execute(
        """
        INSERT INTO revisions(run_id,frame_idx,revision,status,created_by)
        VALUES (%s,%s,%s,%s,%s) RETURNING id
        """,
        (run_id, frame_idx, revision_number, entry["status"], user),
    )
    revision_id = cursor.fetchone()[0]
    if entry["status"] == "edited":
        for item in entry.get("instances") or []:
            obj_id = int(item["obj_id"])
            label = labels.get(obj_id) or str(item.get("label") or object_id)
            class_id = class_ids.get(label)
            if class_id is None:
                cursor.execute(
                    "SELECT id FROM classes WHERE project_id=%s AND name=%s",
                    (project_id, label),
                )
                class_row = cursor.fetchone()
                if class_row is None:
                    cursor.execute(
                        "SELECT COALESCE(max(external_id),-1)+1 FROM classes WHERE project_id=%s",
                        (project_id,),
                    )
                    external_id = int(cursor.fetchone()[0])
                    cursor.execute(
                        "INSERT INTO classes(project_id,external_id,name) VALUES (%s,%s,%s) RETURNING id",
                        (project_id, external_id, label),
                    )
                    class_id = cursor.fetchone()[0]
                else:
                    class_id = class_row[0]
                class_ids[label] = class_id

            mask = annotation_dir / item["path"]
            info = inspect_binary_png(
                mask.read_bytes(),
                expected_size=(int(prompt["image_width"]), int(prompt["image_height"])),
            )
            artifact_id = None
            if info.area_pixels:
                key = object_key_for(workspace, mask)
                cursor.execute(
                    "SELECT id FROM artifacts WHERE run_id=%s AND object_key=%s LIMIT 1",
                    (run_id, key),
                )
                artifact_row = cursor.fetchone()
                if artifact_row:
                    artifact_id = artifact_row[0]
                else:
                    cursor.execute(
                        """
                        INSERT INTO artifacts(run_id,kind,object_key,sha256,byte_size,width,height,state)
                        VALUES (%s,'review_mask',%s,%s,%s,%s,%s,'valid') RETURNING id
                        """,
                        (
                            run_id,
                            key,
                            info.sha256,
                            mask.stat().st_size,
                            info.width,
                            info.height,
                        ),
                    )
                    artifact_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO revision_instances(
                    revision_id,object_id,class_id,mask_artifact_id,deleted,bbox,area_pixels
                ) VALUES (%s,%s,%s,%s,FALSE,%s,%s)
                """,
                (
                    revision_id,
                    obj_id,
                    class_id,
                    artifact_id,
                    list(info.bbox_normalized) if info.bbox_normalized else None,
                    info.area_pixels,
                ),
            )
        for raw_obj_id in entry.get("deleted_obj_ids") or []:
            obj_id = int(raw_obj_id)
            label = labels.get(obj_id) or object_id
            class_id = class_ids.get(label)
            if class_id is None:
                cursor.execute(
                    "SELECT id FROM classes WHERE project_id=%s AND name=%s",
                    (project_id, label),
                )
                class_row = cursor.fetchone()
                if class_row is None:
                    raise RuntimeError(f"classe ausente para tombstone obj_id={obj_id}")
                class_id = class_row[0]
                class_ids[label] = class_id
            cursor.execute(
                """
                INSERT INTO revision_instances(
                    revision_id,object_id,class_id,mask_artifact_id,deleted,bbox,area_pixels
                ) VALUES (%s,%s,%s,NULL,TRUE,NULL,0)
                """,
                (revision_id, obj_id, class_id),
            )

    cursor.execute(
        """
        INSERT INTO audit_events(project_id,actor,event,entity_type,entity_id,details)
        VALUES (%s,%s,'mask_revision_saved','revision',%s,%s::jsonb)
        """,
        (
            project_id,
            user,
            str(revision_id),
            json.dumps(
                {"run_id": str(run_id), "frame_idx": frame_idx, "revision": revision_number}
            ),
        ),
    )


def index_revisions(
    *,
    object_id: str,
    relpath: str,
    segment_dir: Path,
    revisions: list[tuple[int, dict]],
    user: str | None,
) -> None:
    """Indexa um trecho inteiro usando uma conexão e uma transação."""
    database_url = os.environ.get("DATABASE_URL")
    if not database_url or not revisions:
        return
    import psycopg

    workspace = Path(os.environ.get("MST_WORKSPACE", "/workspace"))
    prompt, digest = _effective_prompt(segment_dir)
    annotation_dir = active_segment_output(segment_dir)
    publication = load_current_manifest(segment_dir.parent)
    start = int(prompt["source_start_frame"])
    end = int(prompt.get("source_end_frame", start + int(prompt["frame_count"]) - 1))
    labels = {int(item["obj_id"]): str(item["label"]) for item in prompt.get("objects") or []}

    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        lookup = """
            SELECT run.id, project.id
              FROM annotation_runs run
              JOIN prompts prompt ON prompt.id=run.prompt_id
              JOIN intervals interval ON interval.id=run.interval_id
              JOIN videos video ON video.id=interval.video_id
              JOIN projects project ON project.id=video.project_id
             WHERE project.slug=%s AND video.source_uri=%s
               AND interval.start_frame=%s AND interval.end_frame=%s
               AND prompt.digest=%s AND run.kind='sam3' AND run.state='complete'
        """
        lookup_params: list[object] = [object_id, relpath, start, end, digest]
        if publication is not None:
            lookup += """
               AND run.parameters #>> '{_publication,generation_id}' = %s
               AND run.parameters #>> '{_publication,manifest_sha256}' = %s
               AND run.parameters #>> '{_publication,annotation_revision}' = %s
            """
            lookup_params.extend(
                [
                    str(publication.get("generation_id") or ""),
                    str(publication.get("manifest_sha256") or ""),
                    str(publication.get("annotation_revision")),
                ]
            )
        lookup += " ORDER BY run.finished_at DESC NULLS LAST LIMIT 1"
        cursor.execute(lookup, tuple(lookup_params))
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("run SAM3 normalizado nao encontrado para registrar revisao")
        run_id, project_id = row
        class_ids: dict[str, object] = {}
        for frame_idx, entry in revisions:
            _index_revision_with_cursor(
                cursor,
                run_id=run_id,
                project_id=project_id,
                object_id=object_id,
                workspace=workspace,
                prompt=prompt,
                labels=labels,
                class_ids=class_ids,
                annotation_dir=annotation_dir,
                frame_idx=frame_idx,
                entry=entry,
                user=user,
            )


def index_revision(
    *,
    object_id: str,
    relpath: str,
    segment_dir: Path,
    frame_idx: int,
    entry: dict,
    user: str | None,
) -> None:
    """Compatibilidade para a gravação individual de um frame."""
    index_revisions(
        object_id=object_id,
        relpath=relpath,
        segment_dir=segment_dir,
        revisions=[(frame_idx, entry)],
        user=user,
    )
