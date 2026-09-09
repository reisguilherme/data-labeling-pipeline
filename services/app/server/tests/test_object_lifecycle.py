from __future__ import annotations

import unittest
from pathlib import Path
import tempfile


class ObjectLifecycleTests(unittest.TestCase):
    def test_confirmation_must_equal_excluir_object_id(self) -> None:
        from server.object_lifecycle import validate_purge_confirmation

        with self.assertRaisesRegex(ValueError, "confirmação"):
            validate_purge_confirmation("boom", "EXCLUIR microfone")
        validate_purge_confirmation("boom", "EXCLUIR boom")

    def test_external_roots_are_never_managed(self) -> None:
        from server.object_lifecycle import partition_managed_paths

        root = Path("C:/workspace")
        managed, skipped = partition_managed_paths(
            root,
            [Path("C:/workspace/boom/dataset"), Path("D:/acervo/boom")],
        )
        self.assertEqual(managed, [Path("C:/workspace/boom/dataset")])
        self.assertEqual(skipped, [Path("D:/acervo/boom")])

    def test_workspace_root_itself_is_never_a_managed_delete_target(self) -> None:
        from server.object_lifecycle import partition_managed_paths

        root = Path("C:/workspace")
        managed, skipped = partition_managed_paths(root, [root])
        self.assertEqual(managed, [])
        self.assertEqual(skipped, [root])

    def test_purge_manages_only_the_objects_exact_default_roots(self) -> None:
        from server.object_lifecycle import partition_owned_object_paths

        root = Path(tempfile.mkdtemp()).resolve()
        own_raw = root / "microfone" / "raw"
        own_dataset = root / "microfone" / "dataset"
        boom_raw = root / "boom" / "raw"
        for path in (own_raw, own_dataset, boom_raw):
            path.mkdir(parents=True)

        managed, skipped = partition_owned_object_paths(
            root,
            "microfone",
            [own_raw, own_dataset, boom_raw, root / "microfone"],
            registered_roots=[("boom", boom_raw)],
        )

        self.assertEqual(managed, [own_raw, own_dataset])
        self.assertEqual(skipped, [boom_raw, root / "microfone"])

    def test_purge_rejects_symlink_even_when_it_resolves_inside_workspace(self) -> None:
        from server.object_lifecycle import partition_owned_object_paths

        root = Path(tempfile.mkdtemp()).resolve()
        target = root / "boom" / "raw"
        target.mkdir(parents=True)
        object_root = root / "microfone"
        object_root.mkdir()
        try:
            (object_root / "raw").symlink_to(target, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink indisponivel: {exc}")
        dataset = object_root / "dataset"
        dataset.mkdir()

        managed, skipped = partition_owned_object_paths(
            root,
            "microfone",
            [object_root / "raw", dataset],
            registered_roots=[("boom", target)],
        )

        self.assertEqual(managed, [dataset])
        self.assertEqual(skipped, [object_root / "raw"])

    def test_new_object_roots_must_use_the_exclusive_default_layout(self) -> None:
        from server.object_lifecycle import validate_new_object_roots

        root = Path(tempfile.mkdtemp()).resolve()
        boom_raw = root / "boom" / "raw"
        boom_raw.mkdir(parents=True)

        with self.assertRaisesRegex(ValueError, "layout"):
            validate_new_object_roots(
                root,
                "microfone",
                boom_raw,
                root / "microfone" / "dataset",
                registered_roots=[("boom", boom_raw)],
            )

        expected = validate_new_object_roots(
            root,
            "microfone",
            root / "microfone" / "raw",
            root / "microfone" / "dataset",
            registered_roots=[("boom", boom_raw)],
        )
        self.assertEqual(
            expected,
            (root / "microfone" / "raw", root / "microfone" / "dataset"),
        )

    def test_workspace_create_rejects_custom_root_before_registration(self) -> None:
        from server.workspace import ObjectConfig, Workspace

        root = Path(tempfile.mkdtemp()).resolve()
        workspace = Workspace()
        workspace.root = root
        config = ObjectConfig(
            object_id="microfone",
            display_name="Microfone",
            label="microfone",
            videos_root=root / "boom" / "raw",
            output_root=root / "microfone" / "dataset",
        )

        with self.assertRaisesRegex(ValueError, "layout"):
            workspace.create(config)

        self.assertFalse((root / "objects.json").exists())

    def test_duplicate_active_class_label_is_rejected(self) -> None:
        from server.object_lifecycle import validate_unique_label

        with self.assertRaisesRegex(ValueError, "classe já usada"):
            validate_unique_label(
                [("boom", "boom", False), ("microfone", "microphone", False)],
                object_id="microfone",
                label=" BOOM ",
            )
        validate_unique_label(
            [("boom", "boom", True), ("microfone", "microphone", False)],
            object_id="microfone",
            label="BOOM",
        )

    def test_empty_label_is_rejected(self) -> None:
        from server.object_lifecycle import validate_unique_label

        with self.assertRaisesRegex(ValueError, "obrigatório"):
            validate_unique_label([], object_id="boom", label="  ")


if __name__ == "__main__":
    unittest.main()
