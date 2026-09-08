param([Parameter(Mandatory = $true)][string]$BackupName)
$ErrorActionPreference = "Stop"
if ($BackupName -notmatch '^[A-Za-z0-9_.-]+$') { throw "nome de backup invalido" }
docker compose -f compose.yml exec -T app python /opt/service/entrypoint.py restore "/backups/$BackupName" --confirm
