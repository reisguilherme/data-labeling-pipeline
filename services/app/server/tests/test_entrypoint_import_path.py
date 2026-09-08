from __future__ import annotations

import importlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


def load_entrypoint_module():
    path = Path(__file__).resolve().parents[2] / "entrypoint.py"
    spec = importlib.util.spec_from_file_location("pipeline_entrypoint_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EntrypointImportPathTests(unittest.TestCase):
    def test_makes_the_application_server_package_importable_for_the_worker(self) -> None:
        """Catches worker jobs that cannot import the API package from /opt/service."""
        entrypoint = load_entrypoint_module()
        previous_path = sys.path[:]
        previous_server = sys.modules.pop("server", None)
        try:
            with tempfile.TemporaryDirectory() as temporary:
                app_root = Path(temporary)
                package = app_root / "server"
                package.mkdir()
                (package / "__init__.py").write_text("name = 'pipeline-server'\n", encoding="utf-8")
                sys.path = [item for item in sys.path if item != str(app_root)]

                entrypoint.ensure_app_import_path(app_root)
                server = importlib.import_module("server")

                self.assertEqual(server.name, "pipeline-server")
                self.assertEqual(Path(server.__file__).parent, package)
        finally:
            sys.path = previous_path
            sys.modules.pop("server", None)
            if previous_server is not None:
                sys.modules["server"] = previous_server


if __name__ == "__main__":
    unittest.main()
