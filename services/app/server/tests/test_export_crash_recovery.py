from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server import export as export_module
from server.videos import VideoFile, export_folder_name


class SimulatedProcessCrash(BaseException):
    """Bypasses ``except Exception`` just like an abrupt process death."""


class _Store:
    def __init__(self, relpath: str, revision: int, recorded_root: Path | None = None) -> None:
        self.relpath = relpath
        self.revision = revision
        self.recorded_root = recorded_root

    def entry(self, relpath: str) -> dict:
        assert relpath == self.relpath
        export = None
        if self.recorded_root is not None:
            export = {"root": self.recorded_root.as_posix()}
        return {
            "annotation_revision": self.revision,
            "export": export,
        }


class ExportCrashRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.output = Path(self.temporary.name) / "dataset"
        self.output.mkdir()
        self.video = VideoFile(
            video_id="video-1",
            relpath="folder/clip.mp4",
            abspath=Path(self.temporary.name) / "clip.mp4",
            name="clip",
            size_bytes=1,
            file_mtime="2026-09-09T00:00:00Z",
        )
        self.root = self.output / export_folder_name(self.video)
        self.ctx = SimpleNamespace(
            object_id="boom",
            output_root=self.output,
            store=_Store(self.video.relpath, 2, self.root),
        )
        self.old_owner = export_module.export_owner(self.ctx, self.video, 1)
        self.new_owner = export_module.export_owner(self.ctx, self.video, 2)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _staging(self, token: str = "one") -> Path:
        path = self.output / f".{self.root.name}.export-{token}.part"
        path.mkdir()
        return path

    def _trash(self, token: str = "one") -> Path:
        return self.root / f".replace-{token}.trash"

    @staticmethod
    def _segment(parent: Path, name: str, contents: bytes) -> Path:
        segment = parent / name
        segment.mkdir(parents=True)
        (segment / "000000.jpg").write_bytes(contents)
        return segment

    def _assert_owner(self, expected: dict) -> None:
        marker = self.root / export_module.EXPORT_OWNER_MARKER
        self.assertEqual(json.loads(marker.read_text(encoding="utf-8")), expected)

    def test_export_root_recovers_crash_after_moving_an_old_segment(self) -> None:
        old = self._segment(self.root, "seg_00", b"old")
        export_module.write_export_owner(self.root, self.old_owner)
        staging = self._staging()
        staged = self._segment(staging, "seg_00", b"new")
        trash = self._trash()
        path_type = type(self.root)
        original_replace = path_type.replace

        def crash_after_first_move(path: Path, target: Path):
            result = original_replace(path, target)
            if path == old:
                raise SimulatedProcessCrash("kill after old move")
            return result

        with patch.object(path_type, "replace", new=crash_after_first_move):
            with self.assertRaisesRegex(SimulatedProcessCrash, "old move"):
                export_module.publish_staged_export(
                    self.root, staging, ["seg_00"], self.new_owner, trash
                )

        selected = export_module.export_root_for(self.ctx, self.video)

        self.assertEqual(selected, self.root)
        self.assertEqual((self.root / "seg_00" / "000000.jpg").read_bytes(), b"old")
        self.assertEqual((staged / "000000.jpg").read_bytes(), b"new")
        self._assert_owner(self.old_owner)
        self.assertFalse(trash.exists())

    def test_export_root_recovers_crash_after_journal_before_first_root_creation(self) -> None:
        staging = self._staging()
        staged = self._segment(staging, "seg_00", b"new")
        trash = self._trash()
        journal = self.output / f".{self.root.name}.export-transaction.json"
        path_type = type(self.root)
        original_mkdir = path_type.mkdir

        def crash_before_root_creation(path: Path, *args, **kwargs):
            if path == self.root:
                raise SimulatedProcessCrash("kill before root creation")
            return original_mkdir(path, *args, **kwargs)

        with patch.object(path_type, "mkdir", new=crash_before_root_creation):
            with self.assertRaisesRegex(SimulatedProcessCrash, "root creation"):
                export_module.publish_staged_export(
                    self.root, staging, ["seg_00"], self.new_owner, trash
                )

        self.assertTrue(journal.is_file())
        selected = export_module.export_root_for(self.ctx, self.video)

        self.assertEqual(selected, self.root)
        self.assertFalse(self.root.exists())
        self.assertFalse(journal.exists())
        self.assertEqual((staged / "000000.jpg").read_bytes(), b"new")

    def test_export_root_recovers_crash_after_new_segment_before_owner_commit(self) -> None:
        self._segment(self.root, "seg_00", b"old")
        export_module.write_export_owner(self.root, self.old_owner)
        staging = self._staging()
        self._segment(staging, "seg_00", b"new")
        trash = self._trash()

        with patch(
            "server.export.write_export_owner",
            side_effect=SimulatedProcessCrash("kill before owner"),
        ):
            with self.assertRaisesRegex(SimulatedProcessCrash, "before owner"):
                export_module.publish_staged_export(
                    self.root, staging, ["seg_00"], self.new_owner, trash
                )

        export_module.export_root_for(self.ctx, self.video)

        self.assertEqual((self.root / "seg_00" / "000000.jpg").read_bytes(), b"old")
        self.assertEqual((staging / "seg_00" / "000000.jpg").read_bytes(), b"new")
        self._assert_owner(self.old_owner)
        self.assertFalse(trash.exists())

    def test_first_export_recovery_removes_an_orphan_owner_temporary(self) -> None:
        staging = self._staging()
        self._segment(staging, "seg_00", b"new")
        trash = self._trash()

        with patch(
            "server.export.write_export_owner",
            side_effect=SimulatedProcessCrash("kill during owner write"),
        ):
            with self.assertRaisesRegex(SimulatedProcessCrash, "owner write"):
                export_module.publish_staged_export(
                    self.root, staging, ["seg_00"], self.new_owner, trash
                )

        orphan = self.root / (
            f".{export_module.EXPORT_OWNER_MARKER}."
            "0123456789abcdef0123456789abcdef.tmp"
        )
        orphan.write_bytes(b"partial owner")

        export_module.export_root_for(self.ctx, self.video)

        self.assertFalse(orphan.exists())
        self.assertFalse(self.root.exists())
        self.assertEqual(
            (staging / "seg_00" / "000000.jpg").read_bytes(), b"new"
        )

    def test_owner_is_commit_point_when_process_dies_before_journal_cleanup(self) -> None:
        self._segment(self.root, "seg_00", b"old")
        export_module.write_export_owner(self.root, self.old_owner)
        staging = self._staging()
        self._segment(staging, "seg_00", b"new")
        trash = self._trash()
        journal = self.output / f".{self.root.name}.export-transaction.json"
        path_type = type(self.root)
        original_unlink = path_type.unlink

        def crash_on_journal_cleanup(path: Path, *args, **kwargs):
            if path == journal:
                raise SimulatedProcessCrash("kill after owner")
            return original_unlink(path, *args, **kwargs)

        with patch.object(path_type, "unlink", new=crash_on_journal_cleanup):
            with self.assertRaisesRegex(SimulatedProcessCrash, "after owner"):
                export_module.publish_staged_export(
                    self.root, staging, ["seg_00"], self.new_owner, trash
                )

        export_module.export_root_for(self.ctx, self.video)

        self.assertEqual((self.root / "seg_00" / "000000.jpg").read_bytes(), b"new")
        self._assert_owner(self.new_owner)
        self.assertFalse(journal.exists())

    def test_error_reported_after_owner_replace_is_reconciled_as_committed(self) -> None:
        self._segment(self.root, "seg_00", b"old")
        export_module.write_export_owner(self.root, self.old_owner)
        staging = self._staging()
        self._segment(staging, "seg_00", b"new")
        real_write_owner = export_module.write_export_owner

        def write_then_fail(root: Path, owner: dict) -> None:
            real_write_owner(root, owner)
            raise OSError("directory fsync status unknown")

        with patch("server.export.write_export_owner", side_effect=write_then_fail):
            export_module.publish_staged_export(
                self.root,
                staging,
                ["seg_00"],
                self.new_owner,
                self._trash(),
            )

        self.assertEqual((self.root / "seg_00" / "000000.jpg").read_bytes(), b"new")
        self._assert_owner(self.new_owner)

    def test_process_lock_serializes_publishers_for_the_same_root(self) -> None:
        first_staging = self._staging("first")
        second_staging = self._staging("second")
        self._segment(first_staging, "seg_00", b"first")
        self._segment(second_staging, "seg_00", b"second")
        first_owner = export_module.export_owner(self.ctx, self.video, 1)
        second_owner = export_module.export_owner(self.ctx, self.video, 2)
        first_inside_commit = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        second_finished = threading.Event()
        failures: list[BaseException] = []
        real_write_owner = export_module.write_export_owner

        def blocking_write_owner(root: Path, owner: dict) -> None:
            if owner["annotation_revision"] == 1:
                first_inside_commit.set()
                if not release_first.wait(timeout=2):
                    raise RuntimeError("test timeout")
            real_write_owner(root, owner)

        def publish_first() -> None:
            try:
                export_module.publish_staged_export(
                    self.root,
                    first_staging,
                    ["seg_00"],
                    first_owner,
                    self._trash("first"),
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        def publish_second() -> None:
            second_started.set()
            try:
                export_module.publish_staged_export(
                    self.root,
                    second_staging,
                    ["seg_00"],
                    second_owner,
                    self._trash("second"),
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)
            finally:
                second_finished.set()

        with patch("server.export.write_export_owner", side_effect=blocking_write_owner):
            first = threading.Thread(target=publish_first)
            second = threading.Thread(target=publish_second)
            first.start()
            self.assertTrue(first_inside_commit.wait(timeout=1))
            second.start()
            self.assertTrue(second_started.wait(timeout=1))
            time.sleep(0.05)
            self.assertFalse(second_finished.is_set(), "segundo publisher nao aguardou a trava")
            release_first.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual((self.root / "seg_00" / "000000.jpg").read_bytes(), b"second")
        self._assert_owner(second_owner)

    def test_publish_rejects_staging_outside_the_export_parent(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        staged = self._segment(outside, "seg_00", b"outside")

        with self.assertRaisesRegex(ValueError, "staging"):
            export_module.publish_staged_export(
                self.root,
                outside,
                ["seg_00"],
                self.new_owner,
                self._trash(),
            )

        self.assertEqual((staged / "000000.jpg").read_bytes(), b"outside")
        self.assertFalse(self.root.exists())

    def test_publish_rejects_dotdot_that_would_escape_through_a_symlink(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        jump = self.output / "jump"
        try:
            jump.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        staging_name = f".{self.root.name}.export-dotdot.part"
        actual_staging = Path(self.temporary.name) / staging_name
        actual_staging.mkdir()
        staged = self._segment(actual_staging, "seg_00", b"outside")
        deceptive_staging = jump / ".." / staging_name

        with self.assertRaisesRegex(ValueError, "staging"):
            export_module.publish_staged_export(
                self.root,
                deceptive_staging,
                ["seg_00"],
                self.new_owner,
                self._trash(),
            )

        self.assertEqual((staged / "000000.jpg").read_bytes(), b"outside")
        self.assertFalse(self.root.exists())

    def test_export_root_never_follows_a_transaction_journal_symlink(self) -> None:
        outside = Path(self.temporary.name) / "outside-journal.json"
        outside.write_text("do not touch", encoding="utf-8")
        journal = self.output / f".{self.root.name}.export-transaction.json"
        try:
            journal.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")

        with self.assertRaisesRegex(ValueError, "journal.*symlink"):
            export_module.export_root_for(self.ctx, self.video)

        self.assertEqual(outside.read_text(encoding="utf-8"), "do not touch")

    def test_oversized_transaction_is_rejected_before_it_can_become_durable(self) -> None:
        journal = self.output / f".{self.root.name}.export-transaction.json"

        with self.assertRaisesRegex(ValueError, "excede"):
            export_module._write_json_transaction(
                journal,
                {"padding": "x" * (export_module._MAX_TRANSACTION_BYTES + 1)},
            )

        self.assertFalse(journal.exists())

    def test_publish_rejects_a_symlinked_export_parent(self) -> None:
        real_output = Path(self.temporary.name) / "real-output"
        real_output.mkdir()
        linked_output = Path(self.temporary.name) / "linked-output"
        try:
            linked_output.symlink_to(real_output, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        root = linked_output / self.root.name
        staging = linked_output / f".{root.name}.export-linked.part"
        staging.mkdir()
        staged = self._segment(staging, "seg_00", b"new")

        with self.assertRaisesRegex(ValueError, "symlink"):
            export_module.publish_staged_export(
                root,
                staging,
                ["seg_00"],
                self.new_owner,
                root / ".replace-linked.trash",
            )

        self.assertEqual((staged / "000000.jpg").read_bytes(), b"new")
        self.assertFalse(root.exists())

    def test_publish_rejects_a_symlink_inside_a_staged_segment(self) -> None:
        outside = Path(self.temporary.name) / "outside-frame.jpg"
        outside.write_bytes(b"outside")
        staging = self._staging()
        segment = staging / "seg_00"
        segment.mkdir()
        linked_frame = segment / "000000.jpg"
        try:
            linked_frame.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")

        with self.assertRaisesRegex(ValueError, "symlink"):
            export_module.publish_staged_export(
                self.root,
                staging,
                ["seg_00"],
                self.new_owner,
                self._trash(),
            )

        self.assertEqual(outside.read_bytes(), b"outside")
        self.assertTrue(linked_frame.is_symlink())
        self.assertFalse(self.root.exists())

    def test_export_root_rejects_a_symlinked_workspace_root(self) -> None:
        real_output = Path(self.temporary.name) / "real-workspace"
        real_output.mkdir()
        linked_output = Path(self.temporary.name) / "linked-workspace"
        try:
            linked_output.symlink_to(real_output, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        linked_root = linked_output / self.root.name
        ctx = SimpleNamespace(
            object_id="boom",
            output_root=linked_output,
            store=_Store(self.video.relpath, 2, linked_root),
        )
        real_root = real_output / self.root.name
        real_root.mkdir()
        export_module.write_export_owner(
            real_root, export_module.export_owner(ctx, self.video, 2)
        )

        with self.assertRaisesRegex(ValueError, "symlink"):
            export_module.export_root_for(ctx, self.video)

        self.assertEqual(
            export_module.read_export_owner(real_root)["annotation_revision"], 2
        )


if __name__ == "__main__":
    unittest.main()
