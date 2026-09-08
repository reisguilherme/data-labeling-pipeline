from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote


def ensure_app_import_path(app_root: Path = Path("/app")) -> None:
    """Permite que o worker encontre o pacote ``server`` ao rodar de /opt/service."""
    root = str(app_root)
    if root not in sys.path:
        sys.path.insert(0, root)


def secret(name: str, *, required: bool = False) -> str:
    file_name = os.environ.get(f"{name}_FILE")
    value = Path(file_name).read_text(encoding="utf-8").strip() if file_name else os.environ.get(name, "")
    if required and not value:
        raise RuntimeError(f"secret ausente: {name}_FILE")
    if value:
        os.environ[name] = value
    return value


def database_url() -> str:
    password = secret("POSTGRES_PASSWORD", required=True)
    user = os.environ.get("POSTGRES_USER", "pipeline")
    database = os.environ.get("POSTGRES_DB", "pipeline")
    host = os.environ.get("POSTGRES_HOST", "postgres")
    return f"postgresql://{quote(user)}:{quote(password)}@{host}:5432/{quote(database)}"


def migrate(url: str) -> None:
    import psycopg

    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute("SELECT pg_advisory_lock(731903)")
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            for path in sorted(Path("/opt/migrations").glob("*.sql")):
                exists = connection.execute(
                    "SELECT 1 FROM schema_migrations WHERE name = %s", (path.name,)
                ).fetchone()
                if exists:
                    continue
                connection.execute(path.read_text(encoding="utf-8"))
                connection.execute("INSERT INTO schema_migrations(name) VALUES (%s)", (path.name,))
        finally:
            connection.execute("SELECT pg_advisory_unlock(731903)")


def ensure_buckets() -> None:
    from minio import Minio
    from minio.versioningconfig import ENABLED, VersioningConfig

    client = Minio(
        os.environ.get("MINIO_ENDPOINT", "minio:9000"),
        access_key=os.environ["MINIO_ROOT_USER"],
        secret_key=os.environ["MINIO_ROOT_PASSWORD"],
        secure=os.environ.get("MINIO_SECURE", "false").lower() == "true",
    )
    for name in ("videos", "frames", "masks", "models", "datasets", "backups"):
        if not client.bucket_exists(name):
            client.make_bucket(name)
        client.set_bucket_versioning(name, VersioningConfig(ENABLED))


def main() -> int:
    secret("MST_WORKER_TOKEN", required=True)
    secret("MINIO_ROOT_USER", required=True)
    secret("MINIO_ROOT_PASSWORD", required=True)
    url = database_url()
    os.environ["DATABASE_URL"] = url
    migrate(url)
    mode = sys.argv[1] if len(sys.argv) > 1 else "app"
    if mode == "migrate-legacy":
        ensure_buckets()
        return subprocess.call(
            [
                sys.executable,
                "/opt/scripts/migrate_legacy.py",
                "--workspace",
                "/workspace",
                "--database-url",
                url,
                "--apply",
                "--copy-blobs",
                *sys.argv[2:],
            ]
        )
    if mode == "backup":
        return subprocess.call([sys.executable, "/opt/service/backup.py", *sys.argv[2:]])
    if mode == "restore":
        return subprocess.call([sys.executable, "/opt/service/restore.py", *sys.argv[2:]])
    if mode == "app":
        ensure_buckets()
    if mode == "worker":
        ensure_app_import_path()
        from worker import main as worker_main

        return worker_main()
    return subprocess.call(
        [sys.executable, "run.py", "--workspace", "/workspace", "--host", "0.0.0.0", "--port", "8000"],
        cwd="/app",
    )


if __name__ == "__main__":
    raise SystemExit(main())
