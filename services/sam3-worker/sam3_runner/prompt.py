"""Leitura e validação do prompt.json que a triagem exporta.

Este módulo é a fronteira do contrato entre as duas etapas. O `prompt.json` já
traz tudo que o SAM3 precisa — inclusive a linha de uso literal, no campo
`sam3_usage` — então aqui não há decisão nenhuma a tomar: só ler, conferir e
recusar cedo o que estiver inconsistente.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pipeline_core.sam3_runs import (
    Sam3GenerationError,
    effective_prompt_override_path,
)

SCHEMA_VERSION = 1

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp")


class PromptError(ValueError):
    """Prompt inválido. A mensagem sempre diz qual segmento e o quê."""


@dataclass(frozen=True)
class PromptObject:
    obj_id: int
    label: str
    box_normalized: tuple[float, float, float, float]

    @property
    def box_list(self) -> list[float]:
        """Na forma que o torch.tensor([...]) espera: shape (1, 4)."""
        return list(self.box_normalized)


@dataclass(frozen=True)
class Prompt:
    segment_dir: Path
    video: str
    video_relpath: str
    segment: str
    interval_index: int
    source_start_frame: int
    frame_count: int
    image_width: int
    image_height: int
    prompt_frame_idx: int
    objects: tuple[PromptObject, ...]
    flags: dict
    digest: str

    @property
    def out_dir(self) -> Path:
        """`_sam3` é subpasta do próprio segmento de propósito.

        Duas propriedades de graça: o loader de frames do SAM3 ignora
        subdiretórios, então isto não polui a lista de frames; e o export da
        triagem apaga o segmento inteiro antes de reexportar, então um resultado
        obsoleto não pode sobreviver a uma reexportação.
        """
        return self.segment_dir / "_sam3"

    @property
    def labels_dir(self) -> Path:
        return self.out_dir / "labels"

    @property
    def marker_path(self) -> Path:
        return self.out_dir / "run.json"

    @property
    def override_path(self) -> Path:
        """Caixa inicial ajustada na tela de controle, ao lado do prompt.json.

        Não substitui o original: o que a triagem marcou é dado de origem, e
        sobrescrevê-lo apagaria a evidência de que aquela caixa precisou ser
        corrigida — que é justamente o sinal de que o vídeo é difícil.
        """
        return self.out_dir / "prompt_override.json"

    def source_frame(self, local_index: int) -> int:
        return self.source_start_frame + local_index

    def labels(self) -> list[str]:
        seen: list[str] = []
        for obj in self.objects:
            if obj.label not in seen:
                seen.append(obj.label)
        return seen


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _parse_objects(data: dict, origem: Path) -> list[PromptObject]:
    """Valida e converte a lista de objetos de um prompt (ou de um override)."""
    objects: list[PromptObject] = []
    for item in data.get("objects", []):
        box = item.get("box_normalized")
        if not isinstance(box, list) or len(box) != 4:
            raise PromptError(f"{origem}: box_normalized precisa ter 4 números")
        x1, y1, x2, y2 = (float(v) for v in box)
        if not all(0.0 <= v <= 1.0 for v in (x1, y1, x2, y2)):
            raise PromptError(f"{origem}: box_normalized fora de [0,1]: {box}")
        if x2 <= x1 or y2 <= y1:
            raise PromptError(f"{origem}: box degenerado (x2<=x1 ou y2<=y1): {box}")
        label = (item.get("label") or "").strip()
        if not label:
            raise PromptError(f"{origem}: objeto sem label")
        objects.append(
            PromptObject(obj_id=int(item["obj_id"]), label=label, box_normalized=(x1, y1, x2, y2))
        )
    return objects


def load_prompt(segment_dir: Path) -> Prompt:
    """Lê e valida o prompt.json de um segmento."""
    path = segment_dir / "prompt.json"
    if not path.exists():
        raise PromptError(f"{segment_dir}: sem prompt.json")

    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromptError(f"{path}: JSON ilegível ({exc})") from exc

    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise PromptError(
            f"{path}: schema_version {version!r}, esperado {SCHEMA_VERSION} — "
            "a triagem e o runner estão em versões diferentes"
        )

    objects = _parse_objects(data, path)

    # A caixa inicial pode ter sido ajustada na tela de controle do SAM3. O
    # override troca SÓ as caixas — nunca o frame do prompt, a contagem de
    # frames ou as dimensões, que descrevem o segmento em disco.
    try:
        override = effective_prompt_override_path(
            segment_dir, migrate_legacy=True
        )
    except Sam3GenerationError as exc:
        raise PromptError(str(exc)) from exc
    if override is not None:
        try:
            raw_override = override.read_bytes()
            ajustadas = _parse_objects(json.loads(raw_override.decode("utf-8")), override)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PromptError(f"{override}: JSON ilegível ({exc})") from exc
        if ajustadas:
            objects = ajustadas
            # O digest entra no marcador de conclusão: mudar a caixa inicial
            # precisa invalidar um resultado já propagado, senão o segmento
            # continuaria "pronto" com o rótulo antigo.
            raw = raw + raw_override

    if not objects:
        raise PromptError(f"{path}: nenhum objeto — nada para o SAM3 rastrear")

    ids = [obj.obj_id for obj in objects]
    if len(set(ids)) != len(ids):
        raise PromptError(f"{path}: obj_id repetido em {ids}")

    frame_count = int(data.get("frame_count") or 0)
    if frame_count <= 0:
        raise PromptError(f"{path}: frame_count inválido ({frame_count})")

    prompt_frame_idx = int(data.get("prompt_frame_idx") or 0)
    if not (0 <= prompt_frame_idx < frame_count):
        raise PromptError(
            f"{path}: prompt_frame_idx {prompt_frame_idx} fora de [0,{frame_count})"
        )

    width = int(data.get("image_width") or 0)
    height = int(data.get("image_height") or 0)
    if width <= 0 or height <= 0:
        raise PromptError(f"{path}: dimensões inválidas ({width}x{height})")

    return Prompt(
        segment_dir=segment_dir,
        video=data.get("video", ""),
        video_relpath=data.get("video_relpath", ""),
        segment=data.get("segment", segment_dir.name),
        interval_index=int(data.get("interval_index") or 0),
        source_start_frame=int(data.get("source_start_frame") or 0),
        frame_count=frame_count,
        image_width=width,
        image_height=height,
        prompt_frame_idx=prompt_frame_idx,
        objects=tuple(objects),
        flags=data.get("flags") or {},
        digest=_digest(raw),
    )


def frame_files(segment_dir: Path) -> list[Path]:
    """Frames do segmento, na MESMA ordem que o SAM3 vai usar.

    A regra é a do loader do SAM3: numérica quando os nomes são inteiros,
    lexicográfica caso contrário. Isso importa porque os `out_frame_idx` que a
    propagação devolve são posições NESTA lista — errar a ordenação desalinha
    todos os rótulos sem nenhum erro visível.
    """
    files = [
        p for p in segment_dir.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    ]
    try:
        return sorted(files, key=lambda p: int(p.stem))
    except ValueError:
        return sorted(files, key=lambda p: p.name)


def check_against_disk(prompt: Prompt) -> list[str]:
    """Problemas entre o que o prompt declara e o que está no disco."""
    problems: list[str] = []
    files = frame_files(prompt.segment_dir)

    if len(files) != prompt.frame_count:
        problems.append(
            f"frame_count={prompt.frame_count} mas há {len(files)} imagens no disco"
        )
    if files and files[0].stem != "000000":
        problems.append(f"primeiro frame é {files[0].name}, esperado 000000.jpg")

    if files:
        try:
            from PIL import Image

            with Image.open(files[0]) as image:
                size = image.size
            if size != (prompt.image_width, prompt.image_height):
                problems.append(
                    f"prompt diz {prompt.image_width}x{prompt.image_height}, "
                    f"mas o frame é {size[0]}x{size[1]}"
                )
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001
            problems.append(f"não consegui abrir {files[0].name}: {exc}")

    return problems


def find_segments(workspace: Path, object_id: str | None = None) -> list[Path]:
    """Todos os `seg_*` exportados sob o workspace, em ordem estável."""
    found: list[Path] = []
    for object_dir in sorted(workspace.iterdir()):
        if not object_dir.is_dir() or object_dir.name.startswith((".", "_")):
            continue
        if object_id and object_dir.name != object_id:
            continue
        dataset = object_dir / "dataset"
        if not dataset.is_dir():
            continue
        for video_dir in sorted(dataset.iterdir()):
            if not video_dir.is_dir() or video_dir.name.startswith((".", "_")):
                continue
            for segment_dir in sorted(video_dir.iterdir()):
                if segment_dir.is_dir() and segment_dir.name.startswith("seg_"):
                    if (segment_dir / "prompt.json").exists():
                        found.append(segment_dir)
    return found
