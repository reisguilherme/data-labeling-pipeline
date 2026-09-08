from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from minio import Minio


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup", type=Path)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    root = args.backup.resolve()
    if not args.confirm:
        raise SystemExit("restore altera PostgreSQL e MinIO; repita com --confirm")
    if not (root / "backup_manifest.json").is_file() or not (root / "postgres.dump").is_file():
        raise SystemExit("backup invalido ou incompleto")
    subprocess.run(
        [
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--dbname",
            os.environ["DATABASE_URL"],
            str(root / "postgres.dump"),
        ],
        check=True,
    )
    client = Minio(
        os.environ["MINIO_ENDPOINT"],
        access_key=os.environ["MINIO_ROOT_USER"],
        secret_key=os.environ["MINIO_ROOT_PASSWORD"],
        secure=False,
    )
    minio_root = root / "minio"
    for bucket_dir in minio_root.iterdir() if minio_root.is_dir() else []:
        if not client.bucket_exists(bucket_dir.name):
            client.make_bucket(bucket_dir.name)
        for source in bucket_dir.rglob("*"):
            if source.is_file():
                client.fput_object(bucket_dir.name, source.relative_to(bucket_dir).as_posix(), str(source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
