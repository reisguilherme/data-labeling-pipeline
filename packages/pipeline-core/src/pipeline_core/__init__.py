"""Contratos compartilhados pelos serviços do Boom Pipeline."""

from .masks import MaskInfo, MaskValidationError, encode_binary_png, inspect_binary_png
from .model_registry import CheckpointIntegrityError, ModelRegistry, ModelSpec
from .review_store import FileMaskReviewStore, MaskEdit, RevisionConflict
from .segmentation import Polygonization, coco_rle, decode_coco_rle, yolo_polygons

__all__ = [
    "MaskInfo",
    "MaskValidationError",
    "encode_binary_png",
    "inspect_binary_png",
    "FileMaskReviewStore",
    "MaskEdit",
    "RevisionConflict",
    "Polygonization",
    "coco_rle",
    "decode_coco_rle",
    "yolo_polygons",
    "CheckpointIntegrityError",
    "ModelRegistry",
    "ModelSpec",
]
