"""Integração com o gcloud CLI: listar o bucket e baixar o que falta.

Espelha a estratégia de server/ffmpeg.py — o binário é resolvido por env, depois
pelo PATH — e reusa o JobManager para o download, que já sabe rodar subprocesso
em thread, publicar progresso por SSE e cancelar.
"""

from __future__ import annotations

import json
import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .config import VIDEO_EXTS

# Quantos arquivos por invocação de `gcloud storage cp`. Um `cp` único do lote
# inteiro não dá ponto de flush para o manifesto nem cancelamento limpo; um
# processo por arquivo paga ~0,5-1 s de startup do gcloud (é Python) vezes
# centenas de arquivos. Vinte mantém o paralelismo interno do gcloud, dá barra de
# progresso honesta e dá fronteira para gravar o manifesto.
CHUNK = int(os.environ.get("MST_GCS_CHUNK", 20))

_NO_WINDOW = (
    {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
)

# Caracteres que o cmd.exe interpreta. Ver `argv()` para o porquê.
_CMD_METACHARS = set('&|^<>"')


class GcsError(RuntimeError):
    pass


def parse_gs_uri(uri: str) -> tuple[str, str]:
    parsed = urlsplit(uri)
    if parsed.scheme != "gs" or not parsed.netloc:
        raise ValueError(f"URI GCS sem bucket valido: {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/")


def _storage_client():
    try:
        from google.cloud import storage
    except ImportError as exc:
        raise GcsError("google-cloud-storage nao instalado") from exc
    credentials = credentials_path()
    try:
        if credentials:
            return storage.Client.from_service_account_json(credentials)
        return storage.Client()
    except Exception as exc:  # noqa: BLE001
        raise GcsError(f"credencial GCS invalida: {exc}") from exc


@dataclass(frozen=True)
class GcloudBinary:
    path: str
    source: str  # "env" | "path"
    needs_shell: bool


def resolve() -> GcloudBinary:
    override = os.environ.get("MST_GCLOUD_BIN")
    candidate = override or shutil.which("gcloud")
    if not candidate:
        raise GcsError(
            "gcloud não encontrado. Instale o Google Cloud CLI ou aponte "
            "MST_GCLOUD_BIN no .env para o executável."
        )
    if override and not Path(override).exists() and not shutil.which(override):
        raise GcsError(f"MST_GCLOUD_BIN aponta para algo que não existe: {override}")
    resolved = shutil.which(candidate) or candidate
    return GcloudBinary(
        path=resolved,
        source="env" if override else "path",
        needs_shell=resolved.lower().endswith((".cmd", ".bat")),
    )


def credentials_path() -> str | None:
    return os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")


def credentials_file_valid(path: str | Path | None) -> bool:
    """Confirma o formato minimo sem expor nem autenticar a credencial."""
    if not path:
        return False
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("type") == "service_account"
        and payload.get("project_id")
        and payload.get("client_email")
        and payload.get("private_key")
    )


def info() -> dict:
    """Estado da integração, para a interface explicar o que falta configurar."""
    creds = credentials_path()
    creds_ok = credentials_file_valid(creds)
    try:
        binary = resolve()
    except GcsError as exc:
        try:
            python_storage = importlib.util.find_spec("google.cloud.storage") is not None
        except (ImportError, ModuleNotFoundError):
            python_storage = False
        if python_storage and creds_ok:
            return {
                "ok": True,
                "path": "google-cloud-storage",
                "source": "python",
                "error": None,
                "credentials": creds,
                "credentials_ok": True,
                "default_bucket": os.environ.get("MST_GCS_BUCKET"),
                "chunk": CHUNK,
            }
        return {
            "ok": False,
            "path": None,
            "source": None,
            "error": str(exc),
            "credentials": creds,
            "credentials_ok": creds_ok,
            "default_bucket": os.environ.get("MST_GCS_BUCKET"),
            "chunk": CHUNK,
        }
    return {
        "ok": True,
        "path": binary.path,
        "source": binary.source,
        "error": None if creds_ok or not creds else f"credencial não encontrada: {creds}",
        "credentials": creds,
        "credentials_ok": creds_ok,
        "default_bucket": os.environ.get("MST_GCS_BUCKET"),
        "chunk": CHUNK,
    }


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    creds = credentials_path()
    if creds:
        env["GOOGLE_APPLICATION_CREDENTIALS"] = creds
    return env


def argv(binary: GcloudBinary, *args: str) -> list[str]:
    """Linha de comando do gcloud.

    No Windows o `which` devolve `gcloud.cmd`, e CreateProcess não executa um
    .cmd diretamente — é preciso passar por `cmd /c`. Isso reintroduz o parsing
    do cmd.exe (BatBadBut / CVE-2024-3566), e os nomes deste acervo já têm espaço
    e parêntese. Por isso qualquer metacaractere de shell no argumento é recusado
    em vez de mandado adiante torcendo para dar certo.
    """
    if not binary.needs_shell:
        return [binary.path, *args]

    for arg in args:
        if _CMD_METACHARS & set(arg):
            raise GcsError(
                f"o caminho ou nome contém um caractere que o cmd.exe interpreta "
                f"({''.join(sorted(_CMD_METACHARS & set(arg)))}): {arg!r}. "
                "Renomeie o arquivo ou aponte MST_GCLOUD_BIN para o .exe."
            )
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    return [comspec, "/c", binary.path, *args]


def normalize_uri(uri: str) -> str:
    uri = (uri or "").strip()
    if not uri.startswith("gs://"):
        raise GcsError(f"URI precisa começar com gs:// — recebido: {uri!r}")
    return uri.rstrip("/") + "/"


@dataclass(frozen=True)
class RemoteFile:
    name: str  # basename do blob
    uri: str
    size_bytes: int | None
    generation: str | None
    updated: str | None

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "uri": self.uri,
            "size_bytes": self.size_bytes,
            "generation": self.generation,
            "updated": self.updated,
        }


def list_uri(uri: str, *, timeout: int = 180) -> list[RemoteFile]:
    """`gcloud storage ls --json` do prefixo. Síncrono — chame em thread."""
    try:
        binary = resolve()
    except GcsError:
        bucket, prefix = parse_gs_uri(normalize_uri(uri))
        client = _storage_client()
        files = []
        for blob in client.list_blobs(bucket, prefix=prefix, timeout=timeout):
            if not blob.name or blob.name.endswith("/"):
                continue
            files.append(
                RemoteFile(
                    name=blob.name.rsplit("/", 1)[-1],
                    uri=f"gs://{bucket}/{blob.name}",
                    size_bytes=blob.size,
                    generation=str(blob.generation) if blob.generation else None,
                    updated=blob.updated.isoformat() if blob.updated else None,
                )
            )
        return files
    prefix = normalize_uri(uri)
    result = subprocess.run(
        argv(binary, "storage", "ls", "--json", prefix + "**"),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=child_env(),
        **_NO_WINDOW,
    )
    if result.returncode != 0:
        raise GcsError((result.stderr or "").strip()[-2000:] or "gcloud ls falhou")

    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise GcsError(f"resposta do gcloud não é JSON: {exc}") from exc

    files: list[RemoteFile] = []
    for item in data:
        url = item.get("url") or ""
        if not url or url.endswith("/"):
            continue
        metadata = item.get("metadata") or {}
        name = url.rsplit("/", 1)[-1]
        size = metadata.get("size")
        files.append(
            RemoteFile(
                name=name,
                uri=url,
                size_bytes=int(size) if str(size or "").isdigit() else None,
                generation=str(metadata.get("generation") or "") or None,
                updated=metadata.get("updated") or metadata.get("timeCreated"),
            )
        )
    return files


def download_file(uri: str, destination: Path) -> None:
    bucket, name = parse_gs_uri(uri)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        _storage_client().bucket(bucket).blob(name).download_to_filename(str(destination))
    except Exception as exc:  # noqa: BLE001
        destination.unlink(missing_ok=True)
        raise GcsError(f"download GCS falhou para {uri}: {exc}") from exc


def is_video(name: str) -> bool:
    return Path(name).suffix.lower() in VIDEO_EXTS


def safe_name(name: str) -> bool:
    """Nome de blob que pode virar arquivo local sem sair da pasta.

    O nome vem do bucket, ou seja, de fora — tratar como caminho confiável é
    travessia de diretório de graça.
    """
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name or ":" in name:
        return False
    return ".." not in name


def download_argv(binary: GcloudBinary, uris: list[str], dest: Path) -> list[str]:
    return argv(binary, "storage", "cp", *uris, str(dest))
