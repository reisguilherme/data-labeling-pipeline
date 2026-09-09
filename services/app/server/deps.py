"""Dependências do FastAPI: resolvem objeto, usuário e aba a partir do request."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Path as PathParam, Request

from .users import User, users
from .workspace import (
    InvalidObjectId,
    ObjectContext,
    ObjectNotFound,
    ObjectRootUnavailable,
    validate_object_id,
    workspace,
)

USER_COOKIE = "mst_user"
CLIENT_HEADER = "X-MST-Client"


def get_object(object_id: str = PathParam(...)) -> ObjectContext:
    """Contexto do objeto, já varrido e com anotações carregadas.

    A validação do id acontece aqui porque ele vira nome de pasta e segmento de
    URL — é a única superfície de injeção que o escopo por objeto cria.
    """
    try:
        validate_object_id(object_id)
    except InvalidObjectId as exc:
        raise HTTPException(400, str(exc)) from exc

    try:
        ctx = workspace.context(object_id)
    except ObjectNotFound as exc:
        raise HTTPException(404, f"objeto '{object_id}' não existe") from exc

    if ctx.config.archived:
        raise HTTPException(409, f"objeto '{object_id}' está arquivado")
    try:
        ctx.ensure_loaded()
    except ObjectRootUnavailable as exc:
        raise HTTPException(409, str(exc)) from exc
    return ctx


def current_user_optional(request: Request) -> User | None:
    user_id = request.cookies.get(USER_COOKIE)
    if not user_id:
        return None
    user = users.get(user_id)
    if user is not None:
        users.touch(user.user_id)
    return user


def current_user(request: Request) -> User:
    user = current_user_optional(request)
    if user is None:
        raise HTTPException(401, "escolha um usuário para continuar")
    return user


def require_user(_: User = Depends(current_user)) -> None:
    """Para pendurar no router inteiro sem tocar em 25 assinaturas."""


def current_client(request: Request) -> str:
    """Identifica a ABA, não a pessoa.

    Duas abas do mesmo usuário são dois clientes: é o que permite cancelar as
    extrações da aba que trocou de vídeo sem matar as da outra, e o que faz a
    trava ser renovada por quem realmente está com o vídeo aberto.
    """
    return request.headers.get(CLIENT_HEADER) or "anon"


def is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")
