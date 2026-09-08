"""Identidade sem senha.

Isto é atribuição, NÃO autorização. A ferramenta roda numa LAN de confiança e
qualquer pessoa que alcança a porta pode escolher qualquer nome — o objetivo é
saber quem triou o quê e evitar que duas pessoas peguem o mesmo vídeo, não
impedir acesso. Se um dia isso precisar sair da LAN, é aqui que entra senha (ou,
melhor, um proxy autenticado na frente).
"""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from .videos import iso

USER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# Cores estáveis para distinguir quem está com qual vídeo na biblioteca.
_PALETTE = (
    "#f59e0b", "#10b981", "#3b82f6", "#a855f7", "#ef4444",
    "#14b8a6", "#f97316", "#8b5cf6", "#22c55e", "#06b6d4",
)


@dataclass
class User:
    user_id: str
    display_name: str
    color: str
    created_at: str
    last_seen_at: str | None = None

    def to_json(self) -> dict:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "color": self.color,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
        }


def slugify_user(name: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", name)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")[:32]
    return slug


class UserExists(Exception):
    """Nome já cadastrado.

    Exceção própria em vez de FileExistsError: aquela é o que o sistema de
    arquivos levanta, e confundir as duas faz uma falha de disco chegar ao
    usuário como "já existe alguém com esse nome" — mandando-o procurar o
    problema no lugar errado.
    """


class UserRegistry:
    def __init__(self) -> None:
        self.path: Path | None = None
        self._users: dict[str, User] = {}
        self._lock = threading.Lock()

    def bind(self, workspace_root: Path) -> None:
        self.path = workspace_root / "users.json"
        self.load()

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            self._users = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._users = {}
            return
        self._users = {
            item["user_id"]: User(
                user_id=item["user_id"],
                display_name=item.get("display_name") or item["user_id"],
                color=item.get("color") or _PALETTE[0],
                created_at=item.get("created_at", ""),
                last_seen_at=item.get("last_seen_at"),
            )
            for item in data.get("users", [])
            if item.get("user_id")
        }

    def _write(self, users: dict[str, "User"]) -> None:
        """Grava um conjunto de usuários. Propaga OSError de propósito: quem
        chama decide o que fazer, e o `create` depende disso para não publicar
        em memória algo que não chegou ao disco."""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "updated_at": iso(),
            "users": [user.to_json() for user in users.values()],
        }
        tmp = self.path.with_name("users.json.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def save(self) -> None:
        self._write(self._users)

    def writable(self) -> str | None:
        """Motivo pelo qual o registro NÃO pode ser gravado, ou None se pode.

        Existe para a falha aparecer no boot, com caminho e causa, em vez de na
        cara de quem só queria entrar na ferramenta.
        """
        if self.path is None:
            return "o workspace ainda não foi configurado"
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            probe = self.path.with_name(".write_test")
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            return f"{self.path.parent}: {exc.strerror or exc}"
        return None

    # -- consulta ----------------------------------------------------------

    def list(self) -> list[User]:
        return sorted(self._users.values(), key=lambda u: u.display_name.lower())

    def get(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    # -- escrita -----------------------------------------------------------

    def create(self, display_name: str) -> User:
        display_name = (display_name or "").strip()
        if not display_name:
            raise ValueError("nome não pode ser vazio")
        user_id = slugify_user(display_name)
        if not USER_ID_RE.match(user_id):
            raise ValueError("nome precisa ter ao menos uma letra ou número")

        with self._lock:
            if user_id in self._users:
                raise UserExists(f"já existe alguém cadastrado como '{display_name}'")
            user = User(
                user_id=user_id,
                display_name=display_name,
                color=_PALETTE[len(self._users) % len(_PALETTE)],
                created_at=iso(),
            )
            # Publica em memória SÓ depois de gravar. Se o disco recusar, o
            # registro não pode ficar com um usuário que não existe no arquivo:
            # ele bloquearia toda nova tentativa com "já existe" enquanto o
            # login continuaria falhando — um erro recuperável virando beco sem
            # saída, com uma mensagem que aponta para o lugar errado.
            candidates = {**self._users, user_id: user}
            self._write(candidates)
            self._users = candidates
        return user

    def touch(self, user_id: str) -> None:
        """Marca presença. Sem flush em disco a cada batida: o heartbeat chega a
        cada 30 s por usuário e este arquivo não vale uma reescrita por batida."""
        user = self._users.get(user_id)
        if user is not None:
            user.last_seen_at = iso()


users = UserRegistry()
