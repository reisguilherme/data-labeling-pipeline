#!/usr/bin/env python
"""Ponto de entrada.

    python run.py                       # retoma o último workspace
    python run.py --workspace D:\\mst    # define a raiz do workspace
    python run.py --host 0.0.0.0        # servidor da equipe, na LAN
    python run.py --open                # abre o navegador

Os roots do modo antigo (--videos-root/--output-root) continuam aceitos e viram
o objeto "boom", apontando para as pastas onde elas já estão.
"""

from __future__ import annotations

import argparse
import logging
import threading
import webbrowser
from pathlib import Path

# ANTES de importar server.config: o config lê os.environ no import (larguras de
# proxy, teto de cache) e o gcs lê as credenciais. Carregar o .env depois disso
# produziria um ".env silenciosamente ignorado".
from server.env import load as load_env

load_env()

from server.config import APP_NAME, settings  # noqa: E402


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost")


def main() -> None:
    parser = argparse.ArgumentParser(prog=APP_NAME)
    parser.add_argument("--workspace", type=Path, help="raiz do workspace de objetos")
    parser.add_argument("--videos-root", type=Path, help="(legado) pasta com os mp4")
    parser.add_argument("--output-root", type=Path, help="(legado) pasta de saída")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--open", action="store_true", help="abre o navegador")
    parser.add_argument("--reload", action="store_true", help="autoreload (dev)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s"
    )

    saved = settings.load()
    settings.workspace_root = saved.workspace_root
    settings.last_object_id = saved.last_object_id
    settings.legacy = saved.legacy

    if args.workspace:
        settings.workspace_root = args.workspace.expanduser().resolve()
    if args.videos_root and args.output_root:
        settings.legacy = {
            "videos_root": str(args.videos_root.expanduser().resolve()),
            "output_root": str(args.output_root.expanduser().resolve()),
        }
    if args.workspace or (args.videos_root and args.output_root):
        settings.save()

    url = f"http://{args.host}:{args.port}/"
    if args.open:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    print(f"\n  {APP_NAME}  ->  {url}")
    print(f"  workspace: {settings.workspace_root or '(não configurado — defina na UI)'}")
    if not _is_loopback(args.host):
        print(
            "\n  AVISO: escutando fora do loopback, e esta ferramenta NÃO TEM\n"
            "  AUTENTICAÇÃO. O login sem senha serve para saber quem triou o quê\n"
            "  e evitar dois usuários no mesmo vídeo — não para controlar acesso.\n"
            "  Qualquer pessoa que alcance esta porta pode ler e escrever tudo.\n"
            "  Use apenas em rede confiável.\n"
        )
    print()

    import uvicorn

    uvicorn.run(
        "server.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        workers=1,  # estado em memória é autoritativo; nunca mais de um worker
    )


if __name__ == "__main__":
    main()
