$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $repo ".venv/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "venv da API ausente: $python"
}
$env:PYTHONPATH = @(
    (Join-Path $repo "services/app"),
    (Join-Path $repo "packages/pipeline-core/src"),
    (Join-Path $repo "services/sam3-worker"),
    $repo
) -join ";"

& $python -W error -m unittest discover -s (Join-Path $repo "tests") -p "test_*.py" -v
if ($LASTEXITCODE -ne 0) { throw "testes do monorepo falharam" }
& $python -W error -m unittest discover -s (Join-Path $repo "services/app/server/tests") -p "test_*.py" -v
if ($LASTEXITCODE -ne 0) { throw "testes da API falharam" }
Push-Location (Join-Path $repo "services/app/web")
try {
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "build web falhou" }
} finally {
    Pop-Location
}
docker compose -f (Join-Path $repo "compose.yml") config --quiet
if ($LASTEXITCODE -ne 0) { throw "compose invalido" }
