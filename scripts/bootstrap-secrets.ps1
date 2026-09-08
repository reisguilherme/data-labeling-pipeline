$ErrorActionPreference = "Stop"
$secretDir = Join-Path $PSScriptRoot "..\secrets"
New-Item -ItemType Directory -Force -Path $secretDir | Out-Null

function New-SecretFile([string]$Name, [int]$Bytes = 32) {
    $Path = Join-Path $secretDir $Name
    if (Test-Path -LiteralPath $Path) { return }
    $buffer = New-Object byte[] $Bytes
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($buffer) } finally { $generator.Dispose() }
    $value = ([BitConverter]::ToString($buffer) -replace "-", "").ToLowerInvariant()
    [IO.File]::WriteAllText($Path, $value + [Environment]::NewLine)
}

New-SecretFile "postgres_password.txt"
New-SecretFile "minio_password.txt"
New-SecretFile "worker_token.txt" 48
$minioUser = Join-Path $secretDir "minio_user.txt"
if (-not (Test-Path -LiteralPath $minioUser)) {
    [IO.File]::WriteAllText($minioUser, "pipeline-admin" + [Environment]::NewLine)
}
$hfToken = Join-Path $secretDir "hf_token.txt"
if (-not (Test-Path -LiteralPath $hfToken)) {
    [IO.File]::WriteAllText($hfToken, "")
}
$gcsCredentials = Join-Path $secretDir "gcs_credentials.json"
if (-not (Test-Path -LiteralPath $gcsCredentials)) {
    [IO.File]::WriteAllText($gcsCredentials, "{}" + [Environment]::NewLine)
}
Write-Host "Secrets criados em $secretDir. Preencha hf_token.txt se usar Hugging Face."
