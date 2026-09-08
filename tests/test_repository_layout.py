from __future__ import annotations

import unittest
from pathlib import Path


class RepositoryLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def test_build_sources_are_owned_by_the_new_services(self) -> None:
        required = (
            "services/app/run.py",
            "services/app/server/main.py",
            "services/app/web/package-lock.json",
            "services/app/web/src/components/AppShell.tsx",
            "services/app/web/src/views/GlobalDatasetView.tsx",
            "services/app/server/pipeline_state.py",
            "services/app/server/multiclass_dataset.py",
            "services/sam3-worker/sam3_runner/segment.py",
            "packages/pipeline-core/pyproject.toml",
        )
        for relative in required:
            self.assertTrue((self.root / relative).is_file(), relative)

    def test_legacy_project_directories_are_absent(self) -> None:
        self.assertFalse((self.root / "movies-screening-tool").exists())
        self.assertFalse((self.root / "sam3-annotation-tool").exists())

    def test_dockerfiles_do_not_reference_legacy_roots(self) -> None:
        for relative in ("services/app/Dockerfile", "services/sam3-worker/Dockerfile"):
            contents = (self.root / relative).read_text(encoding="utf-8")
            self.assertNotIn("movies-screening-tool", contents)
            self.assertNotIn("sam3-annotation-tool", contents)

    def test_dockerfiles_install_pinned_python_build_backend(self) -> None:
        for relative in ("services/app/Dockerfile", "services/sam3-worker/Dockerfile"):
            contents = (self.root / relative).read_text(encoding="utf-8")
            backend = contents.index("setuptools==80.9.0 wheel==0.45.1")
            package = contents.index("--no-build-isolation /opt/pipeline-core")
            self.assertLess(backend, package, relative)

    def test_gpu_is_not_required_during_image_build(self) -> None:
        contents = (self.root / "services/sam3-worker/Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("torch.cuda.is_available()", contents)
        self.assertIn("torch.version.cuda", contents)

    def test_sam3_image_uses_numeric_non_root_user_without_creating_host_ids(self) -> None:
        contents = (self.root / "services/sam3-worker/Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("groupadd", contents)
        self.assertNotIn("useradd", contents)
        self.assertIn("USER ${UID}:${GID}", contents)


if __name__ == "__main__":
    unittest.main()
