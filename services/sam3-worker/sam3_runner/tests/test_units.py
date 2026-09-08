"""Testes das partes puras do runner — sem torch, sem CUDA, sem SAM3.

    python -m sam3_runner.tests.test_units

Cobre exatamente as três coisas que, se estiverem erradas, produzem um dataset
silenciosamente corrompido em vez de um erro: a geometria da caixa, a
estabilidade do índice de classe, e a invalidação do marcador.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sam3_runner import marker  # noqa: E402
from sam3_runner.boxes import Box, bbox_from_mask, reject_reason, to_yolo, yolo_line  # noqa: E402
from sam3_runner.classes import ClassMap  # noqa: E402

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"[{'  ok  ' if ok else ' FALHA'}] {label}{f' — {detail}' if detail else ''}")
    if not ok:
        failures.append(label)


def test_bbox() -> None:
    import numpy as np

    # Máscara com um retângulo conhecido: linhas 2..4, colunas 3..7.
    mask = np.full((10, 10), -1.0)
    mask[2:5, 3:8] = 1.0
    box = bbox_from_mask(mask)
    check(box == Box(3, 2, 7, 4), "bbox acha o retângulo", str(box))
    check(box.width == 5 and box.height == 3, "cantos são INCLUSIVOS", f"{box.width}x{box.height}")

    check(bbox_from_mask(np.full((10, 10), -1.0)) is None, "máscara vazia devolve None")

    # Um pixel só: largura 1, não 0. É o caso que o "+1" existe para acertar.
    single = np.full((10, 10), -1.0)
    single[5, 5] = 1.0
    box = bbox_from_mask(single)
    check(box.width == 1 and box.height == 1, "pixel isolado tem lado 1")

    # Formato extra do SAM3: (1, 1, H, W) precisa ser reduzido.
    check(bbox_from_mask(mask[None, None, ...]) == Box(3, 2, 7, 4), "reduz dimensões extras")

    # O limiar é em LOGIT 0, como no notebook — 0.0 não conta como positivo.
    zeros = np.zeros((4, 4))
    check(bbox_from_mask(zeros) is None, "limiar é > 0, não >= 0")


def test_yolo() -> None:
    # Caixa cobrindo o frame 10x10 inteiro -> centro no meio, lado 1.0.
    cx, cy, w, h = to_yolo(Box(0, 0, 9, 9), 10, 10)
    check((cx, cy, w, h) == (0.5, 0.5, 1.0, 1.0), "caixa cheia = centro 0.5 e lado 1.0",
          f"{cx},{cy},{w},{h}")

    # Um pixel no canto (0,0) de um frame 10x10: centro em 0.05, lado 0.1.
    cx, cy, w, h = to_yolo(Box(0, 0, 0, 0), 10, 10)
    check(abs(cx - 0.05) < 1e-9 and abs(w - 0.1) < 1e-9,
          "pixel do canto tem lado 1/10, não 0", f"cx={cx} w={w}")

    # Nunca extrapola [0,1].
    cx, cy, w, h = to_yolo(Box(0, 0, 100, 100), 10, 10)
    check(all(0.0 <= v <= 1.0 for v in (cx, cy, w, h)), "valores ficam em [0,1]")

    line = yolo_line(0, Box(0, 0, 9, 9), 10, 10)
    parts = line.split()
    check(len(parts) == 5 and parts[0] == "0", "linha YOLO tem 5 campos", line)
    check(all("." in p for p in parts[1:]), "coordenadas são decimais", line)


def test_filters() -> None:
    check(reject_reason(Box(0, 0, 1, 1), 100, 100, min_box_px=3) == "small",
          "caixa de 2px é descartada")
    check(reject_reason(Box(0, 0, 5, 5), 100, 100, min_box_px=3) is None,
          "caixa de 6px passa")
    check(reject_reason(Box(0, 0, 99, 99), 100, 100, max_box_frac=0.5) == "huge",
          "caixa cobrindo o frame é descartada")
    check(reject_reason(Box(0, 0, 9, 9), 100, 100, max_box_frac=0.5) is None,
          "caixa de 1% do frame passa")


def test_classmap() -> None:
    tmp = Path(tempfile.mkdtemp()) / "sam3_classes.json"
    cmap = ClassMap(tmp)
    check(cmap.index_of("boom") == 0, "primeiro rótulo recebe índice 0")
    check(cmap.index_of("microfone") == 1, "segundo recebe 1")
    check(cmap.index_of("boom") == 0, "consulta repetida é estável")

    # O ponto do módulo: recarregar não pode renumerar nada.
    again = ClassMap(tmp)
    check(again.get("boom") == 0 and again.get("microfone") == 1,
          "índices sobrevivem ao reload", str(again.names))

    # Pedir na ordem inversa também não renumera.
    again.ensure(["microfone", "boom"])
    check(again.names == ["boom", "microfone"], "append-only: a ordem não muda",
          str(again.names))

    check("0: boom" in again.data_yaml(), "data.yaml sai no formato do Ultralytics")


def test_marker() -> None:
    tmp = Path(tempfile.mkdtemp()) / "run.json"
    params = {"min_box_px": 3, "max_side": 0}
    report = marker.RunReport(
        status="done", prompt_digest="abc", frame_count=10, frames_written=10, params=params
    )
    marker.write(tmp, report)

    check(marker.is_complete(tmp, prompt_digest="abc", frame_count=10, params=params),
          "marcador íntegro conta como pronto")
    check(not marker.is_complete(tmp, prompt_digest="OUTRO", frame_count=10, params=params),
          "prompt reanotado invalida o marcador")
    check(not marker.is_complete(tmp, prompt_digest="abc", frame_count=10,
                                 params={"min_box_px": 10, "max_side": 0}),
          "mudar parâmetro invalida o marcador")
    check(not marker.is_complete(tmp, prompt_digest="abc", frame_count=99, params=params),
          "contagem de frames diferente invalida")

    marker.write(tmp, marker.RunReport(status="error", prompt_digest="abc",
                                       frame_count=10, frames_written=10, params=params))
    check(not marker.is_complete(tmp, prompt_digest="abc", frame_count=10, params=params),
          "erro nunca conta como pronto")

    check(marker.is_complete(tmp.with_name("nao_existe.json"), prompt_digest="abc",
                             frame_count=10, params=params) is False,
          "marcador ausente conta como pendente")

    data = json.loads(tmp.read_text(encoding="utf-8"))
    check(data["schema_version"] == marker.SCHEMA_VERSION, "marcador carrega schema_version")


def main() -> int:
    print("--- bbox ---");      test_bbox()
    print("--- yolo ---");      test_yolo()
    print("--- filtros ---");   test_filters()
    print("--- classmap ---");  test_classmap()
    print("--- marcador ---");  test_marker()
    print(f"\n{'FALHOU: ' + ', '.join(failures) if failures else 'todos os testes passaram'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
