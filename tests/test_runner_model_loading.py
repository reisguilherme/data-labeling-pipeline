from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "pipeline-core" / "src"))
sys.path.insert(0, str(ROOT / "services" / "sam3-worker"))

from pipeline_core.model_registry import ModelSource, ModelSpec
from sam3_runner import model as runner_model
from sam3_runner import queue_client
from sam3_runner.model import checkpoint_builder_kwargs


class RunnerModelLoadingTests(unittest.TestCase):
    def test_startup_configuration_error_is_held_without_restart_loop(self) -> None:
        reports = []

        class Client:
            def status(self, state, message=None):
                reports.append((state, message))

        result = queue_client.hold_startup_error(
            Client(), RuntimeError("HF_TOKEN ausente"), once=True
        )

        self.assertEqual(result, 2)
        self.assertEqual(reports, [("error", "RuntimeError: HF_TOKEN ausente")])

    def test_finetuned_checkpoint_disables_hf_and_requires_strict_state_dict(self) -> None:
        path = Path(tempfile.mkdtemp()) / "fine-tuned.pt"
        spec = ModelSpec(
            model_id="boom-v3",
            source=ModelSource(type="local", path=path),
            sha256="a" * 64,
            sam3_commit="8f0b7f4d4e7eda2ed606ebde6702c93359ad01da",
        )

        kwargs = checkpoint_builder_kwargs(spec, path)

        self.assertEqual(kwargs["checkpoint_path"], str(path))
        self.assertIs(kwargs["load_from_HF"], False)
        self.assertIs(kwargs["strict_state_dict_loading"], True)

    @unittest.skipUnless(
        importlib.util.find_spec("torch") is not None,
        "PyTorch pertence à imagem do sam3-worker",
    )
    def test_training_checkpoint_is_reduced_to_inference_state_dict(self) -> None:
        import torch

        prepare = getattr(runner_model, "prepare_inference_checkpoint", None)
        if prepare is None:
            self.fail("prepare_inference_checkpoint ainda nao foi implementado")
        root = Path(tempfile.mkdtemp())
        source = root / "training.pt"
        torch.save(
            {
                "model": {"layer.weight": torch.arange(4)},
                "optimizer": {"state": {1: torch.ones(4096)}},
                "epoch": 12,
            },
            source,
        )

        prepared = prepare(source, root / "cache", "a" * 64)
        payload = torch.load(prepared, map_location="cpu", weights_only=True)

        self.assertEqual(set(payload), {"layer.weight"})
        self.assertTrue(torch.equal(payload["layer.weight"], torch.arange(4)))
        self.assertLess(prepared.stat().st_size, source.stat().st_size)

    def test_detector_overlay_rejects_unapproved_partial_loading(self) -> None:
        validate = getattr(runner_model, "validate_detector_overlay", None)
        if validate is None:
            self.fail("validate_detector_overlay ainda nao foi implementado")

        validate(
            ["backbone.vision_backbone.sam2_convs.0.conv_1x1.weight"],
            [],
        )
        with self.assertRaisesRegex(RuntimeError, "incompativel"):
            validate(["backbone.vision_backbone.trunk.pos_embed"], [])
        with self.assertRaisesRegex(RuntimeError, "incompativel"):
            validate([], ["camada.desconhecida"])


if __name__ == "__main__":
    unittest.main()
