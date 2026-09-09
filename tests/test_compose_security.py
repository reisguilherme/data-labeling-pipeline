from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml


class ComposeSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "compose.yml").read_text(encoding="utf-8")
        )
        cls.services = cls.document["services"]

    def test_only_expected_services_exist_and_app_publishes_loopback(self) -> None:
        self.assertEqual(
            set(self.services),
            {"app", "worker", "sam3-worker", "postgres", "minio-init", "minio"},
        )
        published_port = self.services["app"]["ports"]
        self.assertEqual(
            published_port,
            ["${APP_BIND:-127.0.0.1}:${APP_PORT:-8000}:8000"],
        )
        defaults = re.sub(r"\$\{[A-Z_]+:-([^}]+)\}", r"\1", published_port[0])
        self.assertEqual(defaults, "127.0.0.1:8000:8000")
        for name in set(self.services) - {"app"}:
            self.assertNotIn("ports", self.services[name])

    def test_every_service_drops_capabilities_and_prevents_privilege_escalation(self) -> None:
        for name, service in self.services.items():
            self.assertEqual(service.get("cap_drop"), ["ALL"], name)
            self.assertIn("no-new-privileges:true", service.get("security_opt") or [], name)

    def test_minio_initializer_has_only_chown_capability(self) -> None:
        initializer = self.services["minio-init"]
        self.assertEqual(initializer["user"], "0:0")
        self.assertEqual(initializer["cap_add"], ["CHOWN"])
        self.assertEqual(initializer["restart"], "no")
        self.assertEqual(
            self.services["minio"]["depends_on"]["minio-init"]["condition"],
            "service_completed_successfully",
        )

    def test_sensitive_environment_uses_secret_files(self) -> None:
        sensitive = {
            "POSTGRES_PASSWORD",
            "MINIO_ROOT_USER",
            "MINIO_ROOT_PASSWORD",
            "MST_WORKER_TOKEN",
            "HF_TOKEN",
        }
        for name, service in self.services.items():
            environment = service.get("environment") or {}
            self.assertFalse(sensitive & set(environment), name)

    def test_model_and_registry_mounts_are_read_only(self) -> None:
        volumes = self.services["sam3-worker"]["volumes"]
        self.assertIn("./config:/config:ro", volumes)
        self.assertIn("./models:/models:ro", volumes)

    def test_huggingface_cache_is_persistent_and_writable(self) -> None:
        worker = self.services["sam3-worker"]
        self.assertEqual(worker["environment"]["HF_HOME"], "/model-cache/huggingface")
        self.assertIn("model-cache:/model-cache", worker["volumes"])

    def test_gcs_credentials_are_mounted_as_a_secret(self) -> None:
        for name in ("app", "worker"):
            self.assertIn("gcs_credentials", self.services[name]["secrets"], name)
            self.assertEqual(
                self.services[name]["environment"]["GOOGLE_APPLICATION_CREDENTIALS"],
                "/run/secrets/gcs_credentials",
            )
        self.assertEqual(
            self.document["secrets"]["gcs_credentials"]["file"],
            "./secrets/gcs_credentials.json",
        )

if __name__ == "__main__":
    unittest.main()
