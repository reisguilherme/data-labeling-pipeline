"""Configuração e estado global de roots.

Os roots podem vir da linha de comando ou serem definidos pela UI; são
persistidos para que `python run.py` sem argumentos retome a última sessão.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "movies-screening-tool"
APP_VERSION = "0.2.0"

VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".m4v", ".mpg", ".mpeg", ".webm")

# Acima deste número de frames o proxy completo é caro demais e caímos no modo
# janela sob demanda. ~4000 frames a 480px ≈ 130 MB e ~40 s de extração.
PROXY_MAX_FRAMES = int(os.environ.get("MST_PROXY_MAX_FRAMES", 4000))

# Largura do proxy. Vale mais do que parece: o material típico é 4K, e o objeto de
# interesse costuma ser pequeno ou aparecer só como reflexo — num proxy de 480px
# ele simplesmente não é visível. Medido num 4K HEVC de 87 s (2083 frames):
#
#     480px   ~12 s    3 KB/frame     ilegível para reflexos
#     960px   ~12,5 s  10 KB/frame
#    1280px   ~12,7 s  15 KB/frame    <- padrão
#    1920px   ~26 s    28 KB/frame    2x o tempo, ganho invisível a 1:1 na tela
#
# Até 1280px o custo é dominado por decodificar o 4K, não por escalar — subir de
# 480 para 1280 é praticamente de graça. Suba para 1920 via env se precisar de
# zoom; o acervo inteiro ainda cabe folgado no teto do cache.
PROXY_WIDTH = int(os.environ.get("MST_PROXY_WIDTH", 1280))
PROXY_QSCALE = int(os.environ.get("MST_PROXY_QSCALE", 3))

# Segunda resolução, para a imagem grande do palco. 0 = resolução original.
#
# Num 4K, mesmo 1280px perde detalhe decisivo — os olhos de alguém ao fundo viram
# uma massa, e é exatamente esse tipo de pista que diz se um reflexo é o boom. As
# duas resoluções saem do MESMO passe de ffmpeg via `split`, então o custo extra é
# só de encodar: medido em 4K/87 s, 12,7 s (só 1280) contra 19,4 s (1280 + 4K).
#
# Extrair um frame 4K sob demanda seria a alternativa óbvia e é pior: `trim`
# decodifica desde o frame 0, o que dá 8,6 s para um frame no fim do vídeo —
# inviável para navegar frame a frame.
STAGE_WIDTH = int(os.environ.get("MST_STAGE_WIDTH", 0))
STAGE_QSCALE = int(os.environ.get("MST_STAGE_QSCALE", 2))

# Raio padrão (em frames) da janela extraída sob demanda em vídeos longos.
WINDOW_RADIUS = 300

THUMB_WIDTH = 320
THUMB_QSCALE = 5

EXPORT_QSCALE = 2

# Frames de palco em 4K pesam ~125 KB cada, então o cache cresce bem mais rápido
# do que com só o proxy pequeno. A evicção é LRU, então o teto é conforto, não
# limite rígido de trabalho.
CACHE_LIMIT_GB = float(os.environ.get("MST_CACHE_LIMIT_GB", 40.0))

# Cada processo CPU tem orçamento próprio. Sem um limite explícito, duas
# réplicas do worker permitem que cada processo do FFmpeg ocupe todos os cores,
# aumentando a latência de todas as ações concorrentes em vez de reduzi-la.
FFMPEG_THREADS = max(1, int(os.environ.get("MST_FFMPEG_THREADS", "4")))

# Codecs que Chrome/Edge conseguem decodificar em <video>. HEVC fica de fora.
BROWSER_CODECS = frozenset({"h264", "vp8", "vp9", "av1"})


def _settings_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / APP_NAME / "settings.json"
    return Path.home() / f".{APP_NAME}" / "settings.json"


SETTINGS_SCHEMA_VERSION = 2


@dataclass
class Settings:
    """Preferências do processo, não de um objeto de anotação.

    Na v1 isto guardava um único par videos_root/output_root — o app inteiro
    servia um objeto só. Na v2 guarda a raiz do workspace; os roots de cada
    objeto vivem em `objects.json` (ver server/workspace.py). Os roots antigos
    são preservados em `legacy` para o bootstrap adotar a pasta atual como o
    objeto "boom" sem mover um único byte.
    """

    workspace_root: Path | None = None
    last_object_id: str | None = None
    legacy: dict[str, str] | None = None

    def save(self) -> None:
        path = _settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "schema_version": SETTINGS_SCHEMA_VERSION,
            "workspace_root": str(self.workspace_root) if self.workspace_root else None,
            "last_object_id": self.last_object_id,
            "legacy": self.legacy,
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls) -> "Settings":
        path = _settings_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()

        if data.get("schema_version") == SETTINGS_SCHEMA_VERSION:
            return cls(
                workspace_root=(
                    Path(data["workspace_root"]) if data.get("workspace_root") else None
                ),
                last_object_id=data.get("last_object_id"),
                legacy=data.get("legacy"),
            )

        # v1: roots soltos. Vira `legacy`, para o workspace adotá-los.
        legacy = {
            key: data[key]
            for key in ("videos_root", "output_root")
            if data.get(key)
        }
        return cls(legacy=legacy or None)


settings = Settings()
