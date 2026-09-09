from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from server.workspace import ObjectConfig, Workspace


def object_config(root: Path, object_id: str, display_name: str | None = None) -> ObjectConfig:
    return ObjectConfig(
        object_id=object_id,
        display_name=display_name or object_id.title(),
        label=object_id,
        videos_root=root / object_id / "raw",
        output_root=root / object_id / "dataset",
    )


class WorkspaceRegistrySafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()) / "workspace"
        self.root.mkdir()

    def workspace(self) -> Workspace:
        workspace = Workspace()
        workspace.root = self.root
        workspace._read_registry()
        return workspace

    def test_stale_process_create_reloads_registry_before_writing(self) -> None:
        first = self.workspace()
        stale = self.workspace()

        first.create(object_config(self.root, "boom"))
        stale.create(object_config(self.root, "microfone"))

        payload = json.loads((self.root / "objects.json").read_text(encoding="utf-8"))
        self.assertEqual(
            {item["object_id"] for item in payload["objects"]},
            {"boom", "microfone"},
        )

    def test_stale_purge_removes_only_target_and_preserves_concurrent_edit(self) -> None:
        creator = self.workspace()
        creator.create(object_config(self.root, "boom"))
        creator.create(object_config(self.root, "microfone"))
        stale_worker = self.workspace()

        creator.update("microfone", display_name="Microfone atualizado")
        stale_worker.remove_registration("boom")

        self.assertEqual([item.object_id for item in creator.list()], ["microfone"])
        self.assertEqual(creator.get("microfone").display_name, "Microfone atualizado")

    def test_missing_registered_roots_fail_without_creating_empty_replacements(self) -> None:
        missing_raw = self.root / "boom" / "raw"
        missing_dataset = self.root / "boom" / "dataset"
        (self.root / "objects.json").write_text(
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
        workspace = self.workspace()

        with self.assertRaisesRegex(RuntimeError, "raiz.*nao existe"):
            workspace.context("boom").ensure_loaded()

        self.assertFalse(missing_raw.exists())
        self.assertFalse(missing_dataset.exists())

    def test_missing_absolute_root_is_not_silently_rehomed(self) -> None:
        missing_raw = self.root.parent / "old-machine" / "raw"
        config = ObjectConfig.from_json(
            {
                "object_id": "boom",
                "display_name": "Boom",
                "label": "boom",
                "videos_root": str(missing_raw),
                "output_root": str(self.root.parent / "old-machine" / "dataset"),
            },
            self.root,
        )

        self.assertEqual(config.videos_root, missing_raw)
        self.assertNotEqual(config.videos_root, self.root / "boom" / "raw")


if __name__ == "__main__":
    unittest.main()
