$ErrorActionPreference = "Stop"
docker compose -f compose.yml exec -T app python /opt/service/entrypoint.py backup
