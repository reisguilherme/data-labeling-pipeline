"""Registro de objetos de anotação e o contexto que os routers recebem.

Cada OBJETO (boom, microfone, …) tem pasta de vídeos, pasta de saída, bucket de
origem, regras de exclusão e `annotations.json` próprios. Este módulo é o único
lugar que sabe onde cada coisa mora.

Por que contexto explícito e não um "objeto ativo" global
---------------------------------------------------------
As extrações de proxy e o export registram callbacks que rodam DEPOIS, numa
thread, quando o semáforo pesado libera — e é nesse momento que eles leem o
diretório de cache para gravar. Com um objeto ativo global, um usuário trocando
de objeto enquanto uma janela de 60 min está na fila faria os frames caírem no
cache do OUTRO objeto: corrupção silenciosa, não apenas disputa. Um contextvar
teria o mesmo defeito, porque a thread do to_thread está longe do request que o
definiu. Passar `ctx` como argumento torna o acoplamento visível.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .config import CACHE_LIMIT_GB, settings
from .manifest import DownloadManifest, ExclusionList
from .object_lifecycle import validate_new_object_roots
from .store import AnnotationStore
from .videos import VideoIndex, iso

# O object_id vira nome de pasta E segmento de URL — é a única superfície de
# injeção que o escopo por objeto cria, então é validado na porta de entrada.
OBJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# Nomes que o Windows recusa como pasta, com ou sem extensão.
_WIN_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

log = logging.getLogger("movies-screening-tool.workspace")

WORKSPACE_SCHEMA_VERSION = 2


def rewrite_path(value: str | None, old_root: str, new_root: str) -> str | None:
    """Troca o prefixo de um caminho, ANCORADO.

    Nunca `str.replace`: um campo `notes` que mencione a pasta antiga seria
    corrompido junto. Só troca em igualdade exata ou quando o valor começa em
    `old_root + "/"`. Compara na forma POSIX, então funciona para reescrever
    caminho de Windows para Linux (que é o caso do rehome).

    Vive aqui, e não no script de migração, porque é semântica de workspace:
    tanto o `migrate_to_workspace` (que move bytes) quanto o `rehome_workspace`
    (que só reescreve) precisam exatamente desta regra.
    """
    if not value:
        return value
    normalized = value.replace("\\", "/")
    old = old_root.replace("\\", "/").rstrip("/")
    new = new_root.replace("\\", "/").rstrip("/")
    if normalized == old:
        return new
    if normalized.startswith(old + "/"):
        return new + normalized[len(old) :]
    return value


class ObjectNotFound(KeyError):
    pass


class InvalidObjectId(ValueError):
    pass


def validate_object_id(object_id: str) -> str:
    if not OBJECT_ID_RE.match(object_id or ""):
        raise InvalidObjectId(
            "id do objeto deve ter de 1 a 32 caracteres, começar com letra ou "
            "número e usar só minúsculas, números, '-' e '_'"
        )
    if object_id.lower() in _WIN_RESERVED:
        raise InvalidObjectId(f"'{object_id}' é um nome reservado do Windows")
    return object_id


def slugify(name: str) -> str:
    """Nome de exibição -> object_id. Usado no cadastro pela interface."""
    import unicodedata

    normalized = unicodedata.normalize("NFKD", name)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")[:32]
    return slug or "objeto"


def _store_root(path: Path, workspace_root: Path | None) -> str:
    """Grava RELATIVO ao workspace quando o caminho está sob ele.

    É o que torna o objects.json portátil: mover o workspace (ou montá-lo em
    outro lugar dentro de um container) deixa de exigir reescrever o registro.
    Caminhos fora do workspace continuam absolutos, porque não há alternativa.
    """
    if workspace_root is not None:
        try:
            return path.relative_to(workspace_root).as_posix()
        except ValueError:
            pass
    return path.as_posix()


_WINDOWS_ABS = re.compile(r"^[A-Za-z]:[\\/]|^\\\\\\\\")


def _is_foreign_absolute(raw: str) -> bool:
    """O caminho é absoluto para OUTRO sistema operacional?

    Esta checagem existe porque a falha silenciosa é péssima: `Path("C:/x/y")`
    em Linux NÃO é absoluto — é um nome relativo. Sem detectar isso, um registro
    feito no Windows e lido em container vira `/workspace/C:UsersFulano...`, e o
    app cria alegremente uma pasta com esse nome em vez de reclamar.
    """
    if os.name == "nt":
        # Em Windows, um caminho POSIX absoluto ("/data/x") é ambíguo demais
        # para tratar como estrangeiro: pode ser um caminho válido na unidade
        # atual. Só o caso Windows-lido-no-Linux é inequívoco.
        return False
    return bool(_WINDOWS_ABS.match(raw))


def _resolve_root(
    raw: str,
    workspace_root: Path | None,
    *,
    object_id: str | None = None,
    kind: str | None = None,
) -> Path:
    """Caminho de um root, tolerando registros feitos noutro sistema.

    Ordem: relativo ao workspace (o caso normal e portátil) -> absoluto local ->
    e, se o registro veio de outro SO ou aponta para um lugar que não existe
    mais, o LAYOUT PADRÃO `<workspace>/<objeto>/{raw,dataset}`. A última regra é
    o que faz mover o workspace de máquina simplesmente funcionar, em vez de
    exigir reescrever caminho a caminho.
    """
    path = Path(raw)

    if not _is_foreign_absolute(raw) and path.is_absolute():
        if path.is_dir() or workspace_root is None:
            return path
    elif not path.is_absolute() and workspace_root is not None and not _is_foreign_absolute(raw):
        return workspace_root / path

    # Chegou aqui: o caminho gravado não serve nesta máquina.
    if workspace_root is not None and object_id and kind:
        fallback = workspace_root / object_id / kind
        log.warning(
            "objeto %s: o caminho gravado (%s) não existe aqui; usando o layout "
            "padrão %s. Rode tools/rehome_workspace.py para gravar isso.",
            object_id, raw, fallback,
        )
        return fallback

    return workspace_root / path if workspace_root is not None else path


@dataclass
class ObjectConfig:
    object_id: str
    display_name: str
    # Rótulo gravado em cada bbox e no prompt.json que o SAM3 consome. Costuma
    # ser igual ao object_id, mas fica separado: o id é chave de URL/pasta, o
    # label é vocabulário do dataset e pode mudar sem quebrar caminho nenhum.
    label: str
    videos_root: Path
    output_root: Path
    gcs_uri: str | None = None
    exclude_patterns: list[dict] = field(default_factory=list)
    # Id do conjunto de heurísticas de flags por nome de arquivo (server/flags.py).
    # None = não sugerir nada. Ver o comentário em flags.suggest_from_name sobre
    # por que sugestão errada é pior que campo em branco.
    suggest_rules: str | None = None
    # Enfileirar automaticamente para o SAM3 ao terminar um export.
    auto_sam3: bool = True
    created_at: str = ""
    created_by: str | None = None
    archived: bool = False

    def to_json(self, workspace_root: Path | None = None) -> dict:
        return {
            "object_id": self.object_id,
            "display_name": self.display_name,
            "label": self.label,
            "videos_root": _store_root(self.videos_root, workspace_root),
            "output_root": _store_root(self.output_root, workspace_root),
            "gcs_uri": self.gcs_uri,
            "exclude_patterns": self.exclude_patterns,
            "suggest_rules": self.suggest_rules,
            "auto_sam3": self.auto_sam3,
            "created_at": self.created_at,
            "created_by": self.created_by,
            "archived": self.archived,
        }

    @classmethod
    def from_json(cls, data: dict, workspace_root: Path | None = None) -> "ObjectConfig":
        return cls(
            object_id=data["object_id"],
            display_name=data.get("display_name") or data["object_id"],
            label=data.get("label") or data["object_id"],
            videos_root=_resolve_root(
                data["videos_root"], workspace_root,
                object_id=data["object_id"], kind="raw",
            ),
            output_root=_resolve_root(
                data["output_root"], workspace_root,
                object_id=data["object_id"], kind="dataset",
            ),
            gcs_uri=data.get("gcs_uri"),
            exclude_patterns=data.get("exclude_patterns") or [],
            suggest_rules=data.get("suggest_rules"),
            auto_sam3=bool(data.get("auto_sam3", True)),
            created_at=data.get("created_at", ""),
            created_by=data.get("created_by"),
            archived=bool(data.get("archived")),
        )


class ObjectContext:
    """Tudo que um request precisa para operar sobre um objeto."""

    def __init__(self, config: ObjectConfig) -> None:
        self.config = config
        self.index = VideoIndex(config.videos_root, self.cache_dir)
        self.store = AnnotationStore(
            annotations_path=self.annotations_path,
            cache_dir=self.cache_dir,
            videos_root=config.videos_root,
            output_root=config.output_root,
            object_id=config.object_id,
            label=config.label,
            total_provider=lambda: len(self.index.all()),
        )
        # Ficam na pasta de SAÍDA, ao lado do annotations.json: são metadados do
        # dataset, e a pasta de vídeos precisa continuar sendo só vídeos.
        self.downloads = DownloadManifest(
            config.output_root / "download_manifest.json", config.object_id
        )
        self.exclusions = ExclusionList(
            config.output_root / "excluded.json", config.object_id
        )
        self._load_lock = threading.Lock()
        self._loaded = False

    # -- atalhos -----------------------------------------------------------

    @property
    def object_id(self) -> str:
        return self.config.object_id

    @property
    def label(self) -> str:
        return self.config.label

    @property
    def videos_root(self) -> Path:
        return self.config.videos_root

    @property
    def output_root(self) -> Path:
        return self.config.output_root

    @property
    def cache_dir(self) -> Path:
        return self.config.output_root / "_cache"

    @property
    def annotations_path(self) -> Path:
        return self.config.output_root / "annotations.json"

    @property
    def trash_dir(self) -> Path:
        """Irmã de videos_root, nunca dentro dela: o rename continua atômico
        (mesmo volume) e o descarte fica fora do alcance da varredura."""
        return self.config.videos_root.parent / "_trash"

    @property
    def incoming_dir(self) -> Path:
        return self.config.videos_root.parent / "_incoming"

    @property
    def cache_limit_gb(self) -> float:
        return CACHE_LIMIT_GB

    def require_roots(self) -> tuple[Path, Path]:
        return self.config.videos_root, self.config.output_root

    def ensure_dirs(self) -> None:
        self.config.videos_root.mkdir(parents=True, exist_ok=True)
        for sub in ("thumbs", "proxy", "windows", "history"):
            (self.cache_dir / sub).mkdir(parents=True, exist_ok=True)

    # -- carga preguiçosa --------------------------------------------------

    def ensure_loaded(self) -> None:
        """Varre o disco e carrega as anotações na primeira vez que o objeto é
        tocado. Preguiçoso de propósito: com 20 objetos registrados, varrer todos
        no boot custaria minutos de tela morta para abrir um só."""
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            self.ensure_dirs()
            self.index.scan()
            self.store.load()
            # A fila do SAM3 precisa saber onde fica o espelho antes que
            # qualquer rota enfileire algo — senão o enqueue funciona em memória
            # e se perde no primeiro restart, silenciosamente.
            from .sam3 import queue as sam3_queue

            sam3_queue.bind(self.object_id, self.output_root)
            self._loaded = True

    def rescan(self) -> None:
        self.index.scan()


class Workspace:
    """Registro dos objetos. Único singleton que sobra — é o processo."""

    def __init__(self) -> None:
        self.root: Path | None = None
        self._objects: dict[str, ObjectConfig] = {}
        self._contexts: dict[str, ObjectContext] = {}
        self._lock = threading.Lock()

    # -- persistência ------------------------------------------------------

    @property
    def registry_path(self) -> Path:
        if self.root is None:
            raise RuntimeError("workspace_root não configurado")
        return self.root / "objects.json"

    @property
    def ready(self) -> bool:
        return self.root is not None

    def load(self) -> None:
        """Lê o registro do disco. Se o workspace ainda não existe mas há roots
        da v1 em settings, adota-os como o objeto legado (ver `adopt_legacy`)."""
        if settings.workspace_root is not None:
            self.root = Path(settings.workspace_root)
            self._read_registry()
        self.adopt_legacy()

    def _read_registry(self) -> None:
        path = self.registry_path
        if not path.exists():
            self._objects = {}
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._objects = {}
            return
        self._objects = {
            item["object_id"]: ObjectConfig.from_json(item, self.root)
            for item in data.get("objects", [])
            if item.get("object_id")
        }

    def save(self) -> None:
        path = self.registry_path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": WORKSPACE_SCHEMA_VERSION,
            "updated_at": iso(),
            "objects": [cfg.to_json(self.root) for cfg in self._objects.values()],
        }
        tmp = path.with_name("objects.json.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def adopt_legacy(self) -> bool:
        """Registra as pastas da v1 como o objeto "boom", SEM mover nada.

        É o que desacopla o escopo por objeto da migração de pastas: o app passa
        a funcionar multi-objeto imediatamente, apontando para onde os 416 vídeos
        e o annotations.json já estão. Mover para o layout
        <workspace>/<objeto>/{raw,dataset} é um passo separado e reversível
        (tools/migrate_to_workspace.py).
        """
        if not settings.legacy or "boom" in self._objects:
            return False
        videos_root = settings.legacy.get("videos_root")
        output_root = settings.legacy.get("output_root")
        if not videos_root or not output_root:
            return False

        if self.root is None:
            # Sem workspace escolhido ainda: a pasta-mãe dos vídeos serve de raiz,
            # que é onde objects.json/users.json fazem sentido para este acervo.
            self.root = Path(videos_root).parent
            settings.workspace_root = self.root
            self._read_registry()
            if "boom" in self._objects:
                return False

        self._objects["boom"] = ObjectConfig(
            object_id="boom",
            display_name="Boom",
            label="boom",
            videos_root=Path(videos_root),
            output_root=Path(output_root),
            suggest_rules="boom",
            created_at=iso(),
        )
        self.save()
        settings.save()
        return True

    # -- consulta ----------------------------------------------------------

    def list(self, *, include_archived: bool = False) -> list[ObjectConfig]:
        items = [
            cfg
            for cfg in self._objects.values()
            if include_archived or not cfg.archived
        ]
        return sorted(items, key=lambda cfg: cfg.display_name.lower())

    def get(self, object_id: str) -> ObjectConfig:
        cfg = self._objects.get(object_id)
        if cfg is None:
            raise ObjectNotFound(object_id)
        return cfg

    def context(self, object_id: str) -> ObjectContext:
        validate_object_id(object_id)
        with self._lock:
            ctx = self._contexts.get(object_id)
            if ctx is None:
                ctx = ObjectContext(self.get(object_id))
                self._contexts[object_id] = ctx
            return ctx

    def invalidate(self, object_id: str) -> None:
        """Descarta o contexto em cache após editar a config do objeto."""
        with self._lock:
            self._contexts.pop(object_id, None)

    # -- escrita -----------------------------------------------------------

    def create(self, cfg: ObjectConfig) -> ObjectConfig:
        validate_object_id(cfg.object_id)
        if self.root is None:
            raise RuntimeError("workspace_root nao configurado")
        if cfg.object_id in self._objects:
            raise ValueError(f"já existe um objeto com o id '{cfg.object_id}'")
        registered_roots = [
            (current.object_id, path)
            for current in self._objects.values()
            for path in (current.videos_root, current.output_root)
        ]
        cfg.videos_root, cfg.output_root = validate_new_object_roots(
            self.root,
            cfg.object_id,
            cfg.videos_root,
            cfg.output_root,
            registered_roots=registered_roots,
        )
        cfg.created_at = cfg.created_at or iso()
        self._objects[cfg.object_id] = cfg
        self.save()
        self.context(cfg.object_id).ensure_dirs()
        return cfg

    def update(self, object_id: str, **changes) -> ObjectConfig:
        cfg = self.get(object_id)
        for key, value in changes.items():
            if value is not None and hasattr(cfg, key):
                setattr(cfg, key, value)
        self.save()
        self.invalidate(object_id)
        return cfg

    def remove_registration(self, object_id: str) -> ObjectConfig:
        """Remove somente o registro, depois que o job de purge terminou.

        A separação impede que uma falha no inventário/armazenamento faça o
        objeto sumir da UI enquanto seus dados ainda precisam de recuperação.
        """
        cfg = self.get(object_id)
        self._objects.pop(object_id)
        self.invalidate(object_id)
        self.save()
        return cfg

    def default_roots(self, object_id: str) -> tuple[Path, Path]:
        """Layout convencionado para objetos novos."""
        if self.root is None:
            raise RuntimeError("workspace_root não configurado")
        base = self.root / object_id
        return base / "raw", base / "dataset"

    def set_root(self, root: Path) -> None:
        self.root = root
        settings.workspace_root = root
        settings.save()
        root.mkdir(parents=True, exist_ok=True)
        self._read_registry()
        with self._lock:
            self._contexts.clear()


workspace = Workspace()
