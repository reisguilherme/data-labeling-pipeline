[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SourcePath,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z0-9][a-z0-9_-]*$')]
    [string]$ModelId,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')]
    [string]$Version
)

$ErrorActionPreference = 'Stop'
$source = Get-Item -LiteralPath $SourcePath
if (-not $source.PSIsContainer -and $source.Extension -ne '.pt') {
    throw "O checkpoint precisa ter extensao .pt: $($source.FullName)"
}
if ($source.PSIsContainer) {
    throw "SourcePath precisa apontar para um arquivo .pt, nao para uma pasta."
}

$root = Join-Path $PSScriptRoot '..\models\releases'
$targetDir = Join-Path (Join-Path $root $ModelId) $Version
$target = Join-Path $targetDir 'model.pt'
if (Test-Path -LiteralPath $targetDir) {
    throw "A release ja existe e e imutavel: $targetDir. Escolha outro -Version."
}

New-Item -ItemType Directory -Path $targetDir | Out-Null
try {
    Copy-Item -LiteralPath $source.FullName -Destination $target -ErrorAction Stop
    $digest = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
    [ordered]@{
        model_id = $ModelId
        version = $Version
        source_filename = $source.Name
        bytes = (Get-Item -LiteralPath $target).Length
        sha256 = $digest
        staged_at_utc = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $targetDir 'manifest.json') -Encoding utf8
} catch {
    if (Test-Path -LiteralPath $targetDir) {
        Remove-Item -LiteralPath $targetDir -Recurse -Force
    }
    throw
}

$containerPath = "/models/releases/$ModelId/$Version/model.pt"
Write-Host "Release criada: $target"
Write-Host "SHA-256: $digest"
Write-Host ''
Write-Host 'Atualize config/models.local.yaml:'
Write-Host "  model_id: $ModelId-$Version"
Write-Host "  path: $containerPath"
Write-Host "  sha256: $digest"
Write-Host 'Mantenha sam3_commit igual ao commit do codigo SAM3 compativel com este checkpoint.'
