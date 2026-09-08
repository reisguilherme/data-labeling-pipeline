"""Rotas da sessão interativa do SAM3.

Dois públicos, como no resto da fila: a interface (escopada no objeto) e o
runner (global, autenticado por token de worker).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from starlette.responses import FileResponse

from ..deps import current_user, get_object
from ..sam3_session import COMMAND_WAIT, sessions
from ..users import User
from ..workspace import ObjectContext
from .review import _export_root, _segment_paths
from .sam3 import require_worker

router = APIRouter(prefix="/api/objects/{object_id}", tags=["sam3-session"])
worker_router = APIRouter(prefix="/api/sam3/session", tags=["sam3-session"])


# --------------------------------------------------------------------------
# interface
# --------------------------------------------------------------------------


class OpenIn(BaseModel):
    segment: str
    frame_idx: int = 0


@router.post("/videos/{video_id}/sam3/session")
async def open_session(
    video_id: str,
    payload: OpenIn,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
) -> dict:
    """Abre (ou reaproveita) a sessão interativa de um segmento."""
    root, segments, _ = _export_root(ctx, video_id)
    paths = _segment_paths(root, segments, payload.segment)
    if not paths.segment_dir.is_dir():
        raise HTTPException(404, "segmento não encontrado no disco")

    session = sessions.open(
        object_id=ctx.object_id,
        video_id=video_id,
        segment=payload.segment,
        segment_dir=str(paths.segment_dir),
        frame_idx=payload.frame_idx,
        user=user.user_id,
    )

    # Acorda também o long-poll da fila de PROPAGAÇÃO. O runner alterna entre as
    # duas filas, e sem isto a sessão esperaria os 25 s do poll de jobs terminar
    # antes de ser sequer notada — o oposto de interativo.
    from ..sam3 import queue as sam3_queue

    sam3_queue._wake()
    return session.public()


@router.get("/videos/{video_id}/sam3/session")
async def session_state(
    video_id: str,
    segment: str,
    wait: int = 0,
    ctx: ObjectContext = Depends(get_object),
) -> dict:
    """Estado atual. Com `wait`, espera uma mudança antes de responder — é o que
    deixa a interface reagir ao resultado sem ficar consultando em laço."""
    session = sessions.for_segment(ctx.object_id, video_id, segment)
    if session is None:
        return {"state": "none"}

    # Consultar renova o prazo: se a interface está perguntando, alguém está com
    # a tela aberta olhando o frame — e a sessão existe para essa pessoa. O
    # long-poll do RUNNER de propósito não renova: se renovasse, a VRAM ficaria
    # presa enquanto o runner estivesse conectado, que é sempre.
    session.touch()

    if wait > 0 and session.state == "busy":
        event = sessions.event(session.session_id)
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=min(wait, 60))
        except asyncio.TimeoutError:
            pass
        session = sessions.get(session.session_id) or session

    return session.public()


class PreviewIn(BaseModel):
    boxes: list[dict] = Field(default_factory=list)


@router.post("/videos/{video_id}/sam3/session/{session_id}/preview")
async def request_preview(
    video_id: str,
    session_id: str,
    payload: PreviewIn,
    ctx: ObjectContext = Depends(get_object),
    _: User = Depends(current_user),
) -> dict:
    if not payload.boxes:
        raise HTTPException(422, "mande ao menos uma caixa para testar")
    session = sessions.request_preview(session_id, payload.boxes)
    if session is None:
        raise HTTPException(409, "sessão encerrada ou expirada — abra outra")
    return session.public()


@router.delete("/videos/{video_id}/sam3/session/{session_id}")
async def close_session(
    video_id: str,
    session_id: str,
    ctx: ObjectContext = Depends(get_object),
    _: User = Depends(current_user),
) -> dict:
    return {"closed": sessions.close(session_id)}


class OverrideIn(BaseModel):
    segment: str
    boxes: list[dict]


@router.put("/videos/{video_id}/sam3/override")
async def save_override(
    video_id: str,
    payload: OverrideIn,
    ctx: ObjectContext = Depends(get_object),
    user: User = Depends(current_user),
) -> dict:
    """Grava a caixa inicial ajustada, ao lado do prompt.json da triagem."""
    import json
    import os

    from ..videos import iso

    root, segments, _ = _export_root(ctx, video_id)
    paths = _segment_paths(root, segments, payload.segment)

    boxes = []
    for position, box in enumerate(payload.boxes, start=1):
        normalized = box.get("normalized")
        if not isinstance(normalized, list) or len(normalized) != 4:
            raise HTTPException(422, "cada caixa precisa de normalized com 4 números")
        x1, y1, x2, y2 = (float(v) for v in normalized)
        if x2 <= x1 or y2 <= y1:
            raise HTTPException(422, "caixa degenerada")
        boxes.append(
            {
                "obj_id": int(box.get("obj_id") or position),
                "label": box.get("label") or ctx.label,
                "box_normalized": [x1, y1, x2, y2],
            }
        )

    document = {
        "schema_version": 1,
        "segment": payload.segment,
        "objects": boxes,
        "by": user.user_id,
        "at": iso(),
        "note": "caixa inicial ajustada na tela de controle; o prompt.json da triagem fica intacto",
    }
    target = paths.out_dir / "prompt_override.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name("prompt_override.json.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(document, ensure_ascii=False, indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)
    return document


@router.delete("/videos/{video_id}/sam3/override")
async def clear_override(
    video_id: str,
    segment: str,
    ctx: ObjectContext = Depends(get_object),
    _: User = Depends(current_user),
) -> dict:
    """Descarta o ajuste: volta a valer a caixa que a triagem marcou."""
    root, segments, _ = _export_root(ctx, video_id)
    paths = _segment_paths(root, segments, segment)
    target = paths.out_dir / "prompt_override.json"
    existed = target.exists()
    target.unlink(missing_ok=True)
    return {"cleared": existed}


@router.get("/videos/{video_id}/segments/{segment}/sam3/preview/{name}")
async def preview_image(
    video_id: str, segment: str, name: str, ctx: ObjectContext = Depends(get_object)
) -> Response:
    """Serve a máscara que o runner escreveu no disco compartilhado."""
    root, segments, _ = _export_root(ctx, video_id)
    paths = _segment_paths(root, segments, segment)
    target = (paths.out_dir / "_preview" / name).resolve()
    # `name` vem da URL: confina antes de servir.
    if not target.is_relative_to((paths.out_dir / "_preview").resolve()):
        raise HTTPException(400, "caminho inválido")
    if not target.exists():
        raise HTTPException(404, "prévia não encontrada")
    return FileResponse(
        target,
        media_type="image/png",
        # A prévia é reescrita a cada teste, com nome novo por sequência.
        headers={"Cache-Control": "no-cache"},
    )


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------


@worker_router.get("/next")
async def next_session(
    worker: str = "sam3-runner", wait: int = COMMAND_WAIT, _: str = Depends(require_worker)
) -> Response:
    """Próxima sessão a abrir. 204 quando não há nenhuma."""
    session = sessions.take(worker)
    if session is None and wait > 0:
        event = sessions.arrivals()
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=min(wait, 60))
        except asyncio.TimeoutError:
            pass
        session = sessions.take(worker)

    if session is None:
        return Response(status_code=204)

    from fastapi.responses import JSONResponse

    return JSONResponse(
        {
            "session_id": session.session_id,
            "object_id": session.object_id,
            "video_id": session.video_id,
            "segment": session.segment,
            "segment_dir": session.segment_dir,
            "frame_idx": session.frame_idx,
        }
    )


@worker_router.post("/{session_id}/ready")
async def mark_ready(session_id: str, _: str = Depends(require_worker)) -> dict:
    session = sessions.mark_ready(session_id)
    if session is None:
        raise HTTPException(409, "sessão não está mais viva")
    return {"ok": True}


class FailIn(BaseModel):
    error: str


@worker_router.post("/{session_id}/fail")
async def mark_failed(
    session_id: str, payload: FailIn, _: str = Depends(require_worker)
) -> dict:
    sessions.fail(session_id, payload.error)
    return {"ok": True}


@worker_router.get("/{session_id}/command")
async def next_command(
    session_id: str, wait: int = COMMAND_WAIT, _: str = Depends(require_worker)
) -> Response:
    """Long-poll do runner: devolve o próximo comando da sessão.

    204 significa "nada agora, pergunte de novo" — não que a sessão acabou. O
    fim vem como o comando `close`, para o runner liberar a VRAM sabendo que foi
    de propósito.
    """
    command = sessions.take_command(session_id)
    if command is None and wait > 0:
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(404, "sessão desconhecida")
        event = sessions.event(session_id)
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=min(wait, 60))
        except asyncio.TimeoutError:
            pass
        command = sessions.take_command(session_id)

    if command is None:
        return Response(status_code=204)

    from fastapi.responses import JSONResponse

    return JSONResponse(command)


class ResultIn(BaseModel):
    seq: int
    mask: str | None = None  # nome do arquivo em _sam3/_preview/
    bbox: list[float] | None = None
    area_frac: float | None = None
    error: str | None = None


@worker_router.post("/{session_id}/result")
async def deliver_result(
    session_id: str, payload: ResultIn, _: str = Depends(require_worker)
) -> dict:
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(404, "sessão desconhecida")

    mask_url = None
    if payload.mask:
        mask_url = (
            f"/api/objects/{session.object_id}/videos/{session.video_id}"
            f"/segments/{session.segment}/sam3/preview/{payload.mask}"
        )
    sessions.deliver(
        session_id,
        seq=payload.seq,
        mask_url=mask_url,
        bbox=payload.bbox,
        area_frac=payload.area_frac,
        error=payload.error,
    )
    return {"ok": True}
