"""Aplicação FastAPI: monta os routers e serve o SPA buildado."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.staticfiles import StaticFiles

from . import ffmpeg
from .config import APP_NAME, APP_VERSION, settings
from .locks import locks
from .routers import annotations, dataset, ingest
from .routers import jobs as jobs_router
from .routers import library, objects, review
from .routers import sam3 as sam3_router
from .routers import sam3_session as sam3_session_router
from .routers import video
from .users import users
from .workspace import workspace

log = logging.getLogger(APP_NAME)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        binaries = ffmpeg.resolve()
        log.info("ffmpeg %s (%s)", binaries.version, binaries.source)
    except ffmpeg.FFmpegError as exc:
        # Não derruba o processo: a UI precisa subir para mostrar o erro e deixar
        # o usuário apontar MST_FFMPEG. Toda rota que use ffmpeg falha com 4xx.
        log.error("ffmpeg indisponível\n%s", exc)

    # Sem varredura aqui: cada objeto é carregado na primeira vez que alguém o
    # abre (ObjectContext.ensure_loaded). Com vários objetos registrados, varrer
    # todos no boot custaria minutos de tela morta para abrir um só.
    try:
        workspace.load()
        if workspace.ready and settings.workspace_root:
            users.bind(Path(settings.workspace_root))
            locks.bind(Path(settings.workspace_root))
            log.info(
                "workspace %s — %d objeto(s)",
                settings.workspace_root,
                len(workspace.list()),
            )
            # Descobrir que o workspace não é gravável só na hora de criar o
            # primeiro usuário é tarde: o erro aparece na tela de login, longe
            # da causa. Em container, isto quase sempre é o UID/GID do .env não
            # batendo com o dono da pasta montada.
            problema = users.writable()
            if problema:
                log.error("WORKSPACE NÃO É GRAVÁVEL — %s", problema)
                log.error("  A ferramenta sobe, mas não vai cadastrar usuários,")
                log.error("  travar vídeos nem salvar anotações.")
                log.error("  Em container: confira se UID/GID no .env batem com")
                log.error("  o dono da pasta montada em /workspace (id -u / id -g).")
    except Exception as exc:  # noqa: BLE001
        log.error("falha ao carregar o workspace: %s", exc)

    yield


app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)

# Só existe para o `npm run dev` (porta 5173) falar com o backend em dev.
# Em produção o SPA é servido por este mesmo processo, ou seja, mesma origem —
# a origem da LAN NÃO entra aqui. allow_credentials fica explicitamente falso:
# o cookie de identidade não deve viajar em requisição cross-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(objects.router)
app.include_router(objects.user_router)
app.include_router(library.router)
app.include_router(video.router)
app.include_router(jobs_router.router)
app.include_router(jobs_router.scoped)
app.include_router(annotations.router)
app.include_router(ingest.router)
app.include_router(sam3_router.router)
app.include_router(sam3_router.worker_router)
app.include_router(sam3_session_router.router)
app.include_router(sam3_session_router.worker_router)
app.include_router(review.router)
app.include_router(dataset.router)
app.include_router(dataset.global_router)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "version": APP_VERSION}


if STATIC_DIR.is_dir():
    # Montado por ÚLTIMO para não sombrear /api.
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/robots.txt", response_class=PlainTextResponse)
    async def robots() -> str:
        return "User-agent: *\nDisallow: /\n"

    @app.get("/{full_path:path}")
    async def spa(full_path: str):
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "not found"}, status_code=404)
        candidate = STATIC_DIR / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
