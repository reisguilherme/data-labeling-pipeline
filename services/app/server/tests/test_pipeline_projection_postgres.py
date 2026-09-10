from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from server.pipeline_projection import (
    apply_intent,
    fail_intent,
    get_many,
    pending_intents,
    reserve_intent,
    reserve_repair_intent,
)


class PipelineProjectionRolloutCommandTests(unittest.TestCase):
    def test_rollout_command_exposes_explicit_apply_and_resume_controls(self):
        completed = subprocess.run(
            [sys.executable, "-m", "server.pipeline_projection_rollout", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("--apply", completed.stdout)
        self.assertIn("--limit", completed.stdout)
        self.assertIn("--resume-token", completed.stdout)

    def test_packaged_entrypoint_dispatches_help_without_migration_or_database_setup(self):
        import entrypoint

        with patch.object(sys, "argv", ["entrypoint.py", "reconcile-pipeline-projection", "--help"]), \
             patch.object(entrypoint, "secret", return_value="synthetic"), \
             patch.object(entrypoint, "migrate") as migrate, \
             patch.object(entrypoint, "database_url", return_value="synthetic") as database:
            with self.assertRaises(SystemExit) as stopped:
                entrypoint.main()
        self.assertEqual(stopped.exception.code, 0)
        migrate.assert_not_called()
        database.assert_not_called()


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL ausente")
class PipelineProjectionPostgresTests(unittest.TestCase):
    def test_foreign_registered_roots_abort_instead_of_becoming_invalid_candidates(self):
        from server import pipeline_projection_rollout as rollout

        root = self._workspace_root()
        registry = json.loads((root / "objects.json").read_text(encoding="utf-8"))
        registry["objects"].append({
            "object_id": "foreign",
            "display_name": "Foreign",
            "label": "foreign",
            "videos_root": r"C:\\archive\\foreign\\raw",
            "output_root": r"C:\\archive\\foreign\\dataset",
        })
        (root / "objects.json").write_text(json.dumps(registry), encoding="utf-8")

        with patch.dict(os.environ, {"PGOPTIONS": f"-c search_path={self.schema}"}):
            with self.assertRaisesRegex(RuntimeError, "raiz.*outro sistema"):
                rollout.collect_candidates(root, database_url=self.url)

    def test_apply_revalidates_restored_corrected_and_existing_objects(self):
        from server import pipeline_projection_rollout as rollout
        from server.videos import make_video_id

        root = self._workspace_root(with_video=True)
        with self._connect() as connection:
            migration = Path(__file__).resolve().parents[4] / "migrations" / "001_initial.sql"
            connection.execute(migration.read_text(encoding="utf-8"))
        registry_path = root / "objects.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["objects"].append({"object_id": "corrected"})
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        video_id = make_video_id("clip.mp4")
        for object_id in ("restored", "corrected"):
            intent = reserve_intent(
                object_id=object_id,
                video_id=video_id,
                event_kind="fixture",
                source_identity={"state": "old"},
                connect=self._connect,
            )
            apply_intent(intent, {"complete": True}, connect=self._connect)

        with patch.dict(os.environ, {
            "DATABASE_URL": self.url,
            "PGOPTIONS": f"-c search_path={self.schema}",
        }):
            candidates = rollout.collect_candidates(root, database_url=self.url)
            stale = {
                item.object_id: item
                for item in candidates
                if item.object_id in {"restored", "corrected"}
            }
            existing = next(item for item in candidates if item.object_id == "boom")
            self.assertEqual(stale["restored"].source_identity["object"]["state"], "missing")
            self.assertEqual(stale["corrected"].source_identity["object"]["state"], "invalid")

            for object_id in ("restored", "corrected"):
                raw = root / object_id / "raw"
                dataset = root / object_id / "dataset"
                raw.mkdir(parents=True)
                dataset.mkdir(parents=True)
                (raw / "clip.mp4").write_bytes(b"")
                (dataset / "annotations.json").write_text(
                    json.dumps({"schema_version": 2, "videos": {}, "counts": {}}),
                    encoding="utf-8",
                )
            registry["objects"] = [
                item for item in registry["objects"]
                if item.get("object_id") != "corrected"
            ]
            for object_id in ("restored", "corrected"):
                registry["objects"].append({
                    "object_id": object_id,
                    "display_name": object_id.title(),
                    "label": object_id,
                    "videos_root": f"{object_id}/raw",
                    "output_root": f"{object_id}/dataset",
                })
            next(
                item for item in registry["objects"] if item["object_id"] == "boom"
            )["archived"] = True
            registry_path.write_text(json.dumps(registry), encoding="utf-8")

            for candidate in stale.values():
                try:
                    rollout.apply_candidate(
                        candidate,
                        workspace_root=root,
                        database_url=self.url,
                    )
                except AttributeError as exc:
                    self.fail(f"apply sem revalidacao de candidato contextless: {exc}")
            rollout.apply_candidate(
                existing,
                workspace_root=root,
                database_url=self.url,
            )

        for object_id in ("restored", "corrected"):
            record = get_many(object_id, [video_id], connect=self._connect)[video_id]
            self.assertNotIn("object", record.source_identity)
            self.assertIn("annotation", record.source_identity)
        existing_record = get_many(
            "boom", [existing.video_id], connect=self._connect
        )[existing.video_id]
        self.assertEqual(
            existing_record.source_identity.get("object"),
            {"object_id": "boom", "archived": True},
        )

    def test_cli_refuses_missing_schema_without_writing_migration_markers(self):
        from server import pipeline_projection_rollout as rollout

        with self._connect() as connection:
            connection.execute("DROP TABLE video_pipeline_projection_barriers")
            connection.execute(
                "CREATE TABLE schema_migrations(name text PRIMARY KEY, applied_at timestamptz DEFAULT now())"
            )
        before = self._all_schema_rows()
        with patch.dict(os.environ, {"DATABASE_URL": self.url, "PGOPTIONS": f"-c search_path={self.schema}"}):
            with patch("builtins.print") as output:
                code = rollout.main(["--workspace", "/does/not/matter"])
        self.assertEqual(code, 2)
        self.assertIn("006", " ".join(str(call) for call in output.call_args_list))
        self.assertEqual(self._all_schema_rows(), before)

    def test_persisted_removed_video_and_unregistered_object_are_inventoried_in_bulk(self):
        from server import pipeline_projection_rollout as rollout

        root = self._workspace_root()
        for object_id, video_id in (("boom", "removed-video"), ("ghost", "orphan-video")):
            intent = reserve_intent(
                object_id=object_id,
                video_id=video_id,
                event_kind="fixture",
                source_identity={"state": "valid"},
                connect=self._connect,
            )
            apply_intent(intent, {"complete": True}, connect=self._connect)
        with patch.dict(os.environ, {"PGOPTIONS": f"-c search_path={self.schema}"}):
            candidates = rollout.collect_candidates(root, database_url=self.url)
        by_key = {(item.object_id, item.video_id): item for item in candidates}
        self.assertIn(("boom", "removed-video"), by_key)
        self.assertIn(("ghost", "orphan-video"), by_key)
        self.assertEqual(by_key[("boom", "removed-video")].snapshot["stage_status"], "missing")
        self.assertEqual(by_key[("ghost", "orphan-video")].source_identity["object"]["state"], "missing")
        self.assertTrue(by_key[("ghost", "orphan-video")].repairable)

    def test_read_only_apply_does_not_backup_annotations_corrupted_after_inventory(self):
        from server import pipeline_projection_rollout as rollout
        from server import pipeline_reconcile
        from server.sam3_postgres import PostgresSam3Queue

        root = self._workspace_root(with_video=True)
        with patch.dict(os.environ, {"DATABASE_URL": self.url, "PGOPTIONS": f"-c search_path={self.schema}"}):
            with self._connect() as connection:
                migration = Path(__file__).resolve().parents[4] / "migrations" / "001_initial.sql"
                connection.execute(migration.read_text(encoding="utf-8"))
            candidates = rollout.collect_candidates(root, database_url=self.url)
            candidate = candidates[0]
            candidate.context.annotations_path.write_text("{broken", encoding="utf-8")
            pipeline_reconcile.sam3_queue = PostgresSam3Queue(self.url)
            try:
                rollout.run_batch(
                    candidates,
                    limit=1,
                    apply=True,
                    repair=lambda item: pipeline_reconcile.reconcile_video(
                        item.context, item.video_id, read_only=True
                    ),
                    database_url=self.url,
                )
            except TypeError as exc:
                self.fail(f"reconciliacao read-only ausente: {exc}")
        self.assertFalse(candidate.context.annotations_path.with_suffix(".corrupt.json").exists())
        record = get_many("boom", [candidate.video_id], connect=self._connect)[candidate.video_id]
        self.assertEqual(record.snapshot["validation_status"], "invalid")

    def test_structurally_invalid_annotations_are_counted_without_exception(self):
        from server import pipeline_projection_rollout as rollout

        for malformed in ({"videos": []}, {"videos": None}, {"videos": {}, "counts": []}):
            with self.subTest(malformed=malformed):
                root = self._workspace_root(with_video=True, annotations=malformed)
                with patch.dict(os.environ, {"PGOPTIONS": f"-c search_path={self.schema}"}), \
                     patch("server.sam3_postgres.PostgresSam3Queue.public", return_value=None):
                    try:
                        candidates = rollout.collect_candidates(root, database_url=self.url)
                    except (AttributeError, TypeError) as exc:
                        self.fail(f"JSON estrutural invalido abortou inventario: {exc}")
                report = rollout.run_batch(candidates, connect=self._connect)
                self.assertEqual(report["counts"]["invalid"], 1)
    def test_rollout_dry_run_command_reads_workspace_without_changing_files_or_database(self):
        root = Path(tempfile.mkdtemp())
        raw = root / "boom" / "raw"
        dataset = root / "boom" / "dataset"
        raw.mkdir(parents=True)
        dataset.mkdir(parents=True)
        (raw / "clip.mp4").write_bytes(b"")
        (dataset / "annotations.json").write_text(
            json.dumps({"schema_version": 2, "videos": {}, "counts": {}}),
            encoding="utf-8",
        )
        (root / "objects.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "objects": [
                        {
                            "object_id": "boom",
                            "display_name": "Boom",
                            "label": "boom",
                            "videos_root": "boom/raw",
                            "output_root": "boom/dataset",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        migration = Path(__file__).resolve().parents[4] / "migrations" / "001_initial.sql"
        with self._connect() as connection:
            connection.execute(migration.read_text(encoding="utf-8"))
            connection.execute(
                "CREATE TABLE schema_migrations(name text PRIMARY KEY, applied_at timestamptz DEFAULT now())"
            )
            connection.execute(
                "INSERT INTO schema_migrations(name) VALUES (%s), (%s)",
                (
                    "005_video_pipeline_projection.sql",
                    "006_video_pipeline_projection_barriers.sql",
                ),
            )
        before_files = sorted(
            (path.relative_to(root).as_posix(), path.read_bytes())
            for path in root.rglob("*")
            if path.is_file()
        )
        before_database = self._projection_dump()
        env = dict(os.environ)
        env.update(
            DATABASE_URL=self.url,
            PGOPTIONS=f"-c search_path={self.schema}",
            PYTHONDONTWRITEBYTECODE="1",
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "server.pipeline_projection_rollout",
                "--workspace",
                str(root),
                "--limit",
                "1",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        try:
            report = json.loads(completed.stdout)
        except json.JSONDecodeError:
            self.fail(f"comando nao produziu JSON: {completed.stdout!r}")
        self.assertEqual(report["mode"], "dry-run")
        self.assertEqual(report["counts"]["missing"], 1)
        self.assertEqual(self._projection_dump(), before_database)
        after_files = sorted(
            (path.relative_to(root).as_posix(), path.read_bytes())
            for path in root.rglob("*")
            if path.is_file()
        )
        self.assertEqual(after_files, before_files)

    def test_rollout_dry_run_reports_all_states_without_writes_and_has_stable_resume(self):
        from server import pipeline_projection_rollout as rollout

        candidates = self._rollout_candidates()
        before = self._projection_dump()
        runner = getattr(rollout, "run_batch", lambda *args, **kwargs: {})
        first = runner(candidates, limit=2, connect=self._connect)
        second = runner(candidates, limit=2, connect=self._connect)

        self.assertEqual(
            first["counts"],
            {
                "current": 3,
                "stale": 1,
                "missing": 1,
                "pending": 1,
                "legacy": 1,
                "invalid": 1,
            },
        )
        self.assertEqual(first["resume_token"], second["resume_token"])
        self.assertEqual(first["batch_size"], 2)
        self.assertEqual(first["applied"], 0)
        self.assertEqual(self._projection_dump(), before)

    def test_rollout_apply_is_bounded_resumable_projection_only_and_idempotent(self):
        from server import pipeline_projection_rollout as rollout

        candidates = self._rollout_candidates()
        with self._connect() as connection:
            connection.execute("CREATE TABLE protected_artifacts(value text NOT NULL)")
            connection.execute("INSERT INTO protected_artifacts VALUES ('untouched')")

        def repair(candidate):
            intent, current = reserve_repair_intent(
                object_id=candidate.object_id,
                video_id=candidate.video_id,
                source_identity=candidate.source_identity,
                snapshot=candidate.snapshot,
                connect=self._connect,
            )
            if intent is not None:
                apply_intent(intent, candidate.snapshot, connect=self._connect)
            return current

        first = rollout.run_batch(
            candidates, limit=2, apply=True, repair=repair, connect=self._connect
        )
        self.assertEqual(first["applied"], 2)
        self.assertIsNotNone(first["resume_token"])
        middle = rollout.run_batch(
            candidates,
            limit=2,
            resume_token=first["resume_token"],
            apply=True,
            repair=repair,
            connect=self._connect,
        )
        self.assertEqual(middle["applied"], 1)
        self.assertIsNone(middle["resume_token"])
        final = rollout.run_batch(
            candidates, limit=2, apply=True, repair=repair, connect=self._connect
        )
        self.assertEqual(final["applied"], 0)
        self.assertEqual(final["counts"]["current"], 6)
        with self._connect() as connection:
            self.assertEqual(
                connection.execute("SELECT value FROM protected_artifacts").fetchone()[0],
                "untouched",
            )

    def _rollout_candidates(self):
        from server import pipeline_projection_rollout as rollout

        ProjectionCandidate = getattr(rollout, "ProjectionCandidate", None)
        self.assertTrue(callable(ProjectionCandidate), "ProjectionCandidate ausente")

        rows = [
            ProjectionCandidate("boom", "01-current", {"state": "valid", "v": 1}, {"complete": True}),
            ProjectionCandidate("boom", "02-stale", {"state": "valid", "v": 2}, {"complete": False}),
            ProjectionCandidate("boom", "03-missing", {"state": "valid", "v": 1}, {"complete": False}),
            ProjectionCandidate("boom", "04-pending", {"state": "valid", "v": 1}, {"complete": False}),
            ProjectionCandidate("boom", "05-legacy", {"state": "legacy", "v": 1}, {"complete": False}),
            ProjectionCandidate("boom", "06-invalid", {"state": "invalid", "v": 1}, {"complete": False}),
        ]
        for candidate in (rows[0], rows[4], rows[5]):
            intent = reserve_intent(
                object_id=candidate.object_id,
                video_id=candidate.video_id,
                event_kind="fixture",
                source_identity=candidate.source_identity,
                connect=self._connect,
            )
            apply_intent(intent, candidate.snapshot, connect=self._connect)
        stale = reserve_intent(
            object_id="boom", video_id="02-stale", event_kind="fixture",
            source_identity={"state": "valid", "v": 1}, connect=self._connect,
        )
        apply_intent(stale, {"complete": True}, connect=self._connect)
        reserve_intent(
            object_id="boom", video_id="04-pending", event_kind="fixture",
            source_identity=rows[3].source_identity, connect=self._connect,
        )
        return rows

    def _projection_dump(self):
        with self._connect() as connection:
            events = connection.execute(
                "SELECT event_seq, object_id, video_id, status, source_identity, attempts, last_error "
                "FROM video_pipeline_projection_events ORDER BY event_seq"
            ).fetchall()
            rows = connection.execute(
                "SELECT object_id, video_id, event_seq, source_identity, snapshot "
                "FROM video_pipeline_projection ORDER BY object_id, video_id"
            ).fetchall()
        return events, rows

    def _all_schema_rows(self):
        with self._connect() as connection:
            return connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema=%s ORDER BY table_name",
                (self.schema,),
            ).fetchall()

    def _workspace_root(self, *, with_video=False, annotations=None):
        root = Path(tempfile.mkdtemp())
        raw = root / "boom" / "raw"
        dataset = root / "boom" / "dataset"
        raw.mkdir(parents=True)
        dataset.mkdir(parents=True)
        if with_video:
            (raw / "clip.mp4").write_bytes(b"")
        document = annotations if annotations is not None else {"schema_version": 2, "videos": {}, "counts": {}}
        (dataset / "annotations.json").write_text(json.dumps(document), encoding="utf-8")
        (root / "objects.json").write_text(
            json.dumps({"schema_version": 2, "objects": [{
                "object_id": "boom", "display_name": "Boom", "label": "boom",
                "videos_root": "boom/raw", "output_root": "boom/dataset",
            }]}),
            encoding="utf-8",
        )
        return root

    def test_barrier_upgrade_after_005_is_independent_and_replay_safe(self):
        migrations = Path(__file__).resolve().parents[4] / "migrations"
        with self._connect() as connection:
            connection.execute("DROP TABLE video_pipeline_projection_barriers")
            connection.execute((migrations / "005_video_pipeline_projection.sql").read_text())
            self.assertIsNone(connection.execute("SELECT to_regclass('video_pipeline_projection_barriers')").fetchone()[0])
            upgrade = migrations / "006_video_pipeline_projection_barriers.sql"
            self.assertTrue(upgrade.is_file(), "upgrade for already-applied 005 missing")
            connection.execute(upgrade.read_text())
            connection.execute("INSERT INTO video_pipeline_projection_barriers VALUES ('boom', 19)")
            connection.execute(upgrade.read_text())
            self.assertEqual(connection.execute("SELECT event_seq FROM video_pipeline_projection_barriers WHERE object_id='boom'").fetchone()[0], 19)

    def test_paused_canonical_read_cannot_allocate_after_archive_and_restore(self):
        import tempfile
        from concurrent.futures import ThreadPoolExecutor
        from types import SimpleNamespace
        from unittest.mock import patch
        from server import pipeline_projection as store, pipeline_reconcile as reconcile
        from server.tests.test_pipeline_reconcile import _CanonicalVideo, _context
        self.assertTrue(callable(getattr(store, "object_fence", None)), "object fence missing")
        fixture = _CanonicalVideo(Path(tempfile.mkdtemp()))
        ctx = _context(fixture)
        config = SimpleNamespace(archived=False)
        read = threading.Event()
        resume = threading.Event()
        lifecycle_entered = threading.Event()
        real_fence = store.object_fence
        authoritative = reconcile._authoritative_archived
        def paused(ctx, object_id):
            result = authoritative(ctx, object_id)
            read.set()
            if not resume.wait(3):
                raise TimeoutError("test did not release canonical read")
            return result
        def archive():
            with real_fence("boom", connect=self._connect):
                lifecycle_entered.set()
                barrier = store.invalidate_object("boom", connect=self._connect)
                config.archived = True
                return barrier
        with patch.object(store, "object_fence", side_effect=lambda oid: real_fence(oid, connect=self._connect)), patch.object(
            reconcile, "workspace", SimpleNamespace(ready=True, get=lambda oid: config)
        ), patch.object(reconcile.sam3_queue, "public", return_value={"state": "done", "annotation_revision": 7, "run_id": "generation-a"}), patch.object(
            reconcile, "reserve_repair_intent", side_effect=lambda **kw: store.reserve_repair_intent(**kw, connect=self._connect)
        ), patch.object(reconcile, "apply_intent", side_effect=lambda event, snapshot: store.apply_intent(event, snapshot, connect=self._connect)):
            with ThreadPoolExecutor(max_workers=2) as pool, patch.object(reconcile, "_authoritative_archived", side_effect=paused):
                rebuilding = pool.submit(reconcile.reconcile_video, ctx, "video-1")
                self.assertTrue(read.wait(2))
                archiving = pool.submit(archive)
                try:
                    self.assertFalse(lifecycle_entered.wait(0.1))
                finally:
                    resume.set()
                old = rebuilding.result(timeout=3)
                barrier = archiving.result(timeout=3)
            self.assertGreater(barrier, old.event_seq)
            self.assertEqual(store.get_many("boom", ["video-1"], connect=self._connect)["video-1"].projection_status, "stale")
            with real_fence("boom", connect=self._connect):
                restore_barrier = store.invalidate_object("boom", connect=self._connect)
                config.archived = False
            restored = reconcile.reconcile_video(ctx, "video-1")
            self.assertGreater(restored.event_seq, restore_barrier)
            self.assertEqual(restored.source_identity, old.source_identity)
            self.assertEqual(reconcile.reconcile_video(ctx, "video-1").event_seq, restored.event_seq)

    def test_object_barrier_blocks_old_apply_and_forces_fresh_repair(self):
        from server import pipeline_projection as store
        invalidate = getattr(store, "invalidate_object", None)
        self.assertTrue(callable(invalidate), "monotonic object barrier missing")
        older = reserve_intent(object_id="boom", video_id="video-1", event_kind="saved",
                               source_identity={"revision": 1}, connect=self._connect)
        apply_intent(older, {"complete": True}, connect=self._connect)
        delayed = reserve_intent(object_id="boom", video_id="video-2", event_kind="saved",
                                 source_identity={"revision": 1}, connect=self._connect)
        reserve_intent(object_id="boom", video_id="video-1", event_kind="saved",
                       source_identity={"revision": 2}, connect=self._connect)
        barrier = invalidate("boom", connect=self._connect)
        self.assertGreater(barrier, delayed.event_seq)
        self.assertEqual(get_many("boom", ["video-1", "video-2"], connect=self._connect)["video-1"].projection_status, "stale")
        apply_intent(older, {"complete": True}, connect=self._connect)
        apply_intent(delayed, {"complete": True}, connect=self._connect)
        rows = get_many("boom", ["video-1", "video-2"], connect=self._connect)
        self.assertEqual(rows["video-1"].projection_status, "stale")
        self.assertTrue("video-2" not in rows or rows["video-2"].projection_status != "current")
        repair, current = reserve_repair_intent(object_id="boom", video_id="video-1",
            source_identity={"revision": 1}, snapshot={"complete": True}, connect=self._connect)
        self.assertIsNone(current)
        self.assertGreater(repair.event_seq, barrier)
        apply_intent(repair, {"complete": True}, connect=self._connect)
        self.assertEqual(get_many("boom", ["video-1"], connect=self._connect)["video-1"].projection_status, "current")
        with self._connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM video_pipeline_projection").fetchone()[0], 1)

    def setUp(self) -> None:
        import psycopg

        self.psycopg = psycopg
        self.url = os.environ["TEST_DATABASE_URL"]
        self.schema = "pipeline_projection_test_" + uuid.uuid4().hex
        migration = (
            Path(__file__).resolve().parents[4]
            / "migrations"
            / "005_video_pipeline_projection.sql"
        ).read_text(encoding="utf-8")
        migration += (Path(__file__).resolve().parents[4] / "migrations" /
                      "006_video_pipeline_projection_barriers.sql").read_text(encoding="utf-8")
        self.migration = migration
        with psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(f'CREATE SCHEMA "{self.schema}"')
            connection.execute(f'SET search_path TO "{self.schema}"')
            connection.execute(migration)

    def tearDown(self) -> None:
        with self.psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')

    def _connect(self):
        return self.psycopg.connect(
            self.url,
            options=f"-c search_path={self.schema}",
            connect_timeout=5,
        )

    def test_newer_event_wins_when_events_apply_in_reverse_order(self) -> None:
        old = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="annotation_saved",
            source_identity={"annotation_revision": 1},
            connect=self._connect,
        )
        new = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="annotation_saved",
            source_identity={"annotation_revision": 2},
            connect=self._connect,
        )
        self.assertIsNotNone(old)
        self.assertIsNotNone(new)

        apply_intent(new, {"pipeline_stage": "sam3", "revision": 2}, connect=self._connect)
        with self._connect() as connection:
            old_status = connection.execute(
                "SELECT status FROM video_pipeline_projection_events WHERE event_seq=%s",
                (old.event_seq,),
            ).fetchone()[0]
        self.assertEqual(old_status, "superseded")
        current = apply_intent(
            old,
            {"pipeline_stage": "triage", "revision": 1},
            connect=self._connect,
        )

        self.assertEqual(current.event_seq, new.event_seq)
        self.assertEqual(current.snapshot["revision"], 2)
        with self._connect() as connection:
            statuses = dict(
                connection.execute(
                    "SELECT event_seq, status FROM video_pipeline_projection_events"
                ).fetchall()
            )
        self.assertEqual(statuses[old.event_seq], "superseded")
        self.assertEqual(statuses[new.event_seq], "applied")

    def test_same_source_reservation_is_idempotent_and_pending_is_retryable(self) -> None:
        first = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="review_saved",
            source_identity={"review_manifest_sha256": "a" * 64},
            connect=self._connect,
        )
        retry = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="review_saved",
            source_identity={"review_manifest_sha256": "a" * 64},
            connect=self._connect,
        )

        self.assertEqual(first.event_seq, retry.event_seq)
        self.assertEqual(retry.status, "pending")

        failed = fail_intent(
            retry,
            "postgresql://user:database-secret@postgres/db token=worker-secret",
            connect=self._connect,
        )
        self.assertEqual(failed.status, "pending")
        self.assertEqual(failed.attempts, 1)
        self.assertNotIn("database-secret", failed.error)
        self.assertNotIn("worker-secret", failed.error)
        self.assertEqual(
            [item.event_seq for item in pending_intents(connect=self._connect)],
            [first.event_seq],
        )

        applied = apply_intent(
            retry,
            {"pipeline_stage": "completed", "complete": True},
            connect=self._connect,
        )
        rows = get_many("boom", ["video-1", "missing"], connect=self._connect)
        self.assertEqual(rows["video-1"], applied)
        self.assertNotIn("missing", rows)

    def test_same_identity_can_be_reserved_again_after_it_was_superseded(self) -> None:
        first_a = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="reconcile",
            source_identity={"identity": "a"},
            connect=self._connect,
        )
        b = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="reconcile",
            source_identity={"identity": "b"},
            connect=self._connect,
        )
        apply_intent(first_a, {"identity": "a"}, connect=self._connect)
        apply_intent(b, {"identity": "b"}, connect=self._connect)

        second_a = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="reconcile",
            source_identity={"identity": "a"},
            connect=self._connect,
        )
        current = apply_intent(
            second_a,
            {"identity": "a-restored"},
            connect=self._connect,
        )

        self.assertGreater(second_a.event_seq, b.event_seq)
        self.assertEqual(current.event_seq, second_a.event_seq)
        self.assertEqual(current.snapshot["identity"], "a-restored")

    def test_repair_is_newer_than_an_unpublished_pending_mutation(self) -> None:
        source_a = {"identity": "a"}
        snapshot_a = {"pipeline_stage": "completed", "complete": True}
        applied_a = reserve_intent(
            object_id="boom", video_id="video-1", event_kind="reconcile",
            source_identity=source_a, connect=self._connect,
        )
        apply_intent(applied_a, snapshot_a, connect=self._connect)
        pending_b = reserve_intent(
            object_id="boom", video_id="video-1", event_kind="mutation",
            source_identity={"identity": "b"}, connect=self._connect,
        )

        repair, current = reserve_repair_intent(
            object_id="boom", video_id="video-1", source_identity=source_a,
            snapshot=snapshot_a, connect=self._connect,
        )

        self.assertIsNone(current)
        self.assertGreater(repair.event_seq, pending_b.event_seq)
        repaired = apply_intent(repair, snapshot_a, connect=self._connect)
        self.assertEqual(repaired.source_identity, source_a)
        with self._connect() as connection:
            state_b = connection.execute(
                "SELECT status FROM video_pipeline_projection_events WHERE event_seq=%s",
                (pending_b.event_seq,),
            ).fetchone()[0]
        self.assertEqual(state_b, "superseded")

    def test_repair_rewrites_corrupt_snapshot_then_becomes_idempotent(self) -> None:
        source = {"identity": "a"}
        expected = {"pipeline_stage": "completed", "complete": True}
        original = reserve_intent(
            object_id="boom", video_id="video-1", event_kind="reconcile",
            source_identity=source, connect=self._connect,
        )
        apply_intent(original, expected, connect=self._connect)
        with self._connect() as connection:
            connection.execute(
                "UPDATE video_pipeline_projection SET snapshot=%s::jsonb "
                "WHERE object_id='boom' AND video_id='video-1'",
                ('{"complete":false,"pipeline_stage":"broken"}',),
            )

        repair, current = reserve_repair_intent(
            object_id="boom", video_id="video-1", source_identity=source,
            snapshot=expected, connect=self._connect,
        )
        self.assertIsNone(current)
        self.assertGreater(repair.event_seq, original.event_seq)
        fixed = apply_intent(repair, expected, connect=self._connect)

        retry, unchanged = reserve_repair_intent(
            object_id="boom", video_id="video-1", source_identity=source,
            snapshot=expected, connect=self._connect,
        )
        self.assertIsNone(retry)
        self.assertEqual(unchanged.event_seq, fixed.event_seq)
        with self._connect() as connection:
            count = connection.execute(
                "SELECT count(*) FROM video_pipeline_projection_events "
                "WHERE object_id='boom' AND video_id='video-1'"
            ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_concurrent_repair_reservations_reuse_one_pending_event(self) -> None:
        source = {"identity": "a"}
        snapshot = {"pipeline_stage": "review", "complete": False}
        barrier = threading.Barrier(2)
        results = []

        def reserve() -> None:
            barrier.wait(timeout=5)
            results.append(
                reserve_repair_intent(
                    object_id="boom", video_id="video-1",
                    source_identity=source, snapshot=snapshot,
                    connect=self._connect,
                )[0]
            )

        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(len(results), 2)
        self.assertTrue(all(item is not None for item in results))
        self.assertEqual(results[0].event_seq, results[1].event_seq)

    def test_failed_intent_never_persists_unterminated_json_secret_tails(self) -> None:
        intent = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="reconcile",
            source_identity={"revision": 1},
            connect=self._connect,
        )
        for message in (
            r'''db rejected {"password":"\"SYNTHETIC_SECRET_TAIL}''',
            r"""db rejected {'token':'\'SYNTHETIC_SECRET_TAIL}""",
        ):
            with self.subTest(message=message):
                failed = fail_intent(intent, message, connect=self._connect)
                with self._connect() as connection:
                    persisted = connection.execute(
                        "SELECT last_error FROM video_pipeline_projection_events "
                        "WHERE event_seq = %s",
                        (intent.event_seq,),
                    ).fetchone()[0]

                self.assertNotIn("SYNTHETIC_SECRET_TAIL", persisted)
                self.assertIn("[REDACTED]", persisted)
                self.assertEqual(failed.error, persisted)
                self.assertEqual(failed.status, "pending")

    def test_pending_scan_wraps_to_older_failures_after_resume_cursor(self) -> None:
        older = reserve_intent(
            object_id="boom",
            video_id="video-older",
            event_kind="reconcile",
            source_identity={"revision": 1},
            connect=self._connect,
        )
        newer = reserve_intent(
            object_id="boom",
            video_id="video-newer",
            event_kind="reconcile",
            source_identity={"revision": 1},
            connect=self._connect,
        )
        fail_intent(older, "retry later", connect=self._connect)
        apply_intent(newer, {"complete": True}, connect=self._connect)

        resumed = pending_intents(
            after_event_seq=newer.event_seq,
            connect=self._connect,
        )

        self.assertEqual([item.event_seq for item in resumed], [older.event_seq])

    def test_migration_is_safe_to_replay_after_schema_marker_crash(self) -> None:
        with self.psycopg.connect(self.url, autocommit=True) as connection:
            connection.execute(f'SET search_path TO "{self.schema}"')
            connection.execute(self.migration)

        with self._connect() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT tablename
                      FROM pg_tables
                     WHERE schemaname = %s
                    """,
                    (self.schema,),
                ).fetchall()
            }
        self.assertIn("video_pipeline_projection_events", tables)
        self.assertIn("video_pipeline_projection", tables)

    def test_concurrent_applies_for_same_video_do_not_deadlock(self) -> None:
        old = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="annotation_saved",
            source_identity={"revision": 1},
            connect=self._connect,
        )
        new = reserve_intent(
            object_id="boom",
            video_id="video-1",
            event_kind="annotation_saved",
            source_identity={"revision": 2},
            connect=self._connect,
        )
        old_event_locked = threading.Event()
        new_projection_written = threading.Event()
        start = threading.Barrier(2)
        errors: list[BaseException] = []

        class Cursor:
            def __init__(self, cursor, role: str) -> None:
                self._cursor = cursor
                self._role = role
                self._advisory_lock_seen = False

            def __enter__(self):
                self._cursor.__enter__()
                return self

            def __exit__(self, *args):
                return self._cursor.__exit__(*args)

            def execute(self, query, params=None):
                normalized = " ".join(str(query).split())
                if "pg_advisory_xact_lock" in normalized:
                    self._advisory_lock_seen = True
                result = self._cursor.execute(query, params)
                legacy_event_lock = (
                    not self._advisory_lock_seen
                    and "FROM video_pipeline_projection_events" in normalized
                    and "FOR UPDATE" in normalized
                )
                if legacy_event_lock and self._role == "old":
                    old_event_locked.set()
                    if not new_projection_written.wait(3):
                        raise AssertionError("new apply did not reach projection insert")
                elif legacy_event_lock and self._role == "new":
                    if not old_event_locked.wait(3):
                        raise AssertionError("old apply did not lock its event")
                if (
                    legacy_event_lock is False
                    and not self._advisory_lock_seen
                    and self._role == "new"
                    and normalized.startswith("INSERT INTO video_pipeline_projection ")
                ):
                    new_projection_written.set()
                return result

            def fetchone(self):
                return self._cursor.fetchone()

            def fetchall(self):
                return self._cursor.fetchall()

        class Connection:
            def __init__(self, connection, role: str) -> None:
                self._connection = connection
                self._role = role

            def __enter__(self):
                self._connection.__enter__()
                return self

            def __exit__(self, *args):
                return self._connection.__exit__(*args)

            def cursor(self):
                return Cursor(self._connection.cursor(), self._role)

        def run(role: str, intent, revision: int) -> None:
            def connect():
                raw = self.psycopg.connect(
                    self.url,
                    options=(
                        f"-c search_path={self.schema} "
                        "-c statement_timeout=5000 -c deadlock_timeout=100"
                    ),
                    connect_timeout=5,
                )
                return Connection(raw, role)

            try:
                start.wait(timeout=3)
                apply_intent(intent, {"revision": revision}, connect=connect)
            except BaseException as exc:  # collect the exact database failure
                errors.append(exc)

        threads = [
            threading.Thread(target=run, args=("old", old, 1), daemon=True),
            threading.Thread(target=run, args=("new", new, 2), daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=8)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        current = get_many("boom", ["video-1"], connect=self._connect)["video-1"]
        self.assertEqual(current.event_seq, new.event_seq)
        self.assertEqual(current.snapshot["revision"], 2)


if __name__ == "__main__":
    unittest.main()
