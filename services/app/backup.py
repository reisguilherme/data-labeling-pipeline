from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from minio import Minio


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path("/backups") / stamp
    root.mkdir(parents=True, exist_ok=False)
    subprocess.run(
        ["pg_dump", "--format=custom", "--file", str(root / "postgres.dump"), os.environ["DATABASE_URL"]],
        check=True,
    )
    shutil.copytree("/registry", root / "config")

    client = Minio(
        os.environ["MINIO_ENDPOINT"],
        access_key=os.environ["MINIO_ROOT_USER"],
        secret_key=os.environ["MINIO_ROOT_PASSWORD"],
        secure=False,
    )
    for bucket in client.list_buckets():
        for item in client.list_objects(bucket.name, recursive=True):
            destination = root / "minio" / bucket.name / item.object_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            client.fget_object(bucket.name, item.object_name, str(destination))

    manifest_root = root / "workspace-manifests"
    wanted = {"dataset_manifest.json", "run.json", "mask_review.json", "prompt.json"}
    for source in Path("/workspace").rglob("*.json"):
        if source.name not in wanted:
            continue
        relative = source.relative_to("/workspace")
        destination = manifest_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    files = {
        path.relative_to(root).as_posix(): {"sha256": digest(path), "bytes": path.stat().st_size}
        for path in root.rglob("*")
        if path.is_file()
    }
    (root / "backup_manifest.json").write_text(
        json.dumps({"created_at": stamp, "files": files}, indent=2), encoding="utf-8"
    )
    print(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
