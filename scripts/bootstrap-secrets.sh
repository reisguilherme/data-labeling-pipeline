#!/bin/sh
set -eu
secret_dir="$(CDPATH= cd -- "$(dirname -- "$0")/../secrets" && pwd)"
make_secret() {
  target="$secret_dir/$1"
  bytes="$2"
  if [ ! -f "$target" ]; then
    umask 077
    openssl rand -hex "$bytes" > "$target"
  fi
}
make_secret postgres_password.txt 32
make_secret minio_password.txt 32
make_secret worker_token.txt 48
[ -f "$secret_dir/minio_user.txt" ] || printf '%s\n' pipeline-admin > "$secret_dir/minio_user.txt"
[ -f "$secret_dir/hf_token.txt" ] || : > "$secret_dir/hf_token.txt"
[ -f "$secret_dir/gcs_credentials.json" ] || printf '{}\n' > "$secret_dir/gcs_credentials.json"
printf 'Secrets criados em %s. Preencha hf_token.txt se usar Hugging Face.\n' "$secret_dir"
