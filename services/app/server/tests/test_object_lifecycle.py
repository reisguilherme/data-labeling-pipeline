from __future__ import annotations

import unittest
from pathlib import Path


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
