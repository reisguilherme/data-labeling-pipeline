from __future__ import annotations

import importlib
import importlib.util
import unittest


class Sam3WorkerStatusTests(unittest.TestCase):
    def test_error_is_visible_and_ready_status_becomes_stale(self) -> None:
        spec = importlib.util.find_spec("server.sam3_worker_status")
        self.assertIsNotNone(spec, "server.sam3_worker_status ainda nao foi implementado")
        module = importlib.import_module("server.sam3_worker_status")
        now = [100.0]
        registry = module.WorkerStatusRegistry(clock=lambda: now[0], stale_after=30)

        registry.report("error", "HF_TOKEN ausente", "gpu-1")
        self.assertEqual(registry.public()["state"], "error")
        self.assertEqual(registry.public()["message"], "HF_TOKEN ausente")

        # Erro de configuracao precisa continuar visivel durante o restart loop.
        now[0] = 131.0
        registry.report("loading", "carregando", "gpu-1")
        self.assertEqual(registry.public()["state"], "error")
        self.assertEqual(registry.public()["message"], "HF_TOKEN ausente")

        registry.report("ready", None, "gpu-1")
        now[0] = 162.0
        self.assertEqual(registry.public()["state"], "unavailable")


if __name__ == "__main__":
    unittest.main()
