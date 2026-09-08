[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SourceWorkspace,
    [string]$Destination
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Destination)) {
    if ([string]::IsNullOrWhiteSpace($PSScriptRoot)) {
        throw "nao foi possivel localizar a pasta do script; execute com &: & .\scripts\prepare-portable-workspace.ps1 -SourceWorkspace <pasta>"
    }
    $Destination = Join-Path $PSScriptRoot "..\data\workspace"
}

function Get-PortableRelativePath([string]$Root, [string]$Candidate) {
    # System.IO.Path.GetRelativePath so existe no PowerShell 7/.NET Core.
    # Uri.MakeRelativeUri funciona tambem no Windows PowerShell 5.1.
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd([char[]]@('\', '/')) + [IO.Path]::DirectorySeparatorChar
    $candidateFull = [IO.Path]::GetFullPath($Candidate)
    $rootUri = [Uri]$rootFull
    $candidateUri = [Uri]$candidateFull
    if ($rootUri.Scheme -ne $candidateUri.Scheme) {
        return $candidateFull
    }
    return [Uri]::UnescapeDataString($rootUri.MakeRelativeUri($candidateUri).ToString()).Replace("/", [IO.Path]::DirectorySeparatorChar)
}

function Assert-WithinRoot([string]$Root, [string]$Candidate) {
    $relative = Get-PortableRelativePath $Root $Candidate
    if ($relative -eq ".." -or $relative.StartsWith("..$([IO.Path]::DirectorySeparatorChar)") -or [IO.Path]::IsPathRooted($relative)) {
        throw "caminho fora do workspace de origem: $Candidate"
    }
}

function Copy-Tree([string]$Source, [string]$Target, [string[]]$ExcludedDirectories = @()) {
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    $arguments = @($Source, $Target, "/E", "/COPY:DAT", "/DCOPY:DAT", "/R:2", "/W:1", "/NFL", "/NDL", "/NJH", "/NJS")
    if ($ExcludedDirectories.Count -gt 0) {
        $arguments += "/XD"
        $arguments += $ExcludedDirectories
    }
    & robocopy @arguments
    if ($LASTEXITCODE -ge 8) {
        throw "robocopy falhou ($LASTEXITCODE): $Source"
    }
}

$sourceRoot = (Resolve-Path -LiteralPath $SourceWorkspace).Path
$destinationRoot = [IO.Path]::GetFullPath($Destination)
if ($sourceRoot -eq $destinationRoot) { throw "origem e destino nao podem ser iguais" }

$objectsPath = Join-Path $sourceRoot "objects.json"
if (-not (Test-Path -LiteralPath $objectsPath)) { throw "objects.json nao encontrado em $sourceRoot" }
$objects = (Get-Content -LiteralPath $objectsPath -Raw | ConvertFrom-Json).objects
if (-not $objects) { throw "nenhum objeto cadastrado em $objectsPath" }

New-Item -ItemType Directory -Force -Path $destinationRoot | Out-Null
Copy-Item -LiteralPath $objectsPath -Destination (Join-Path $destinationRoot "objects.json") -Force
foreach ($name in @("users.json")) {
    $source = Join-Path $sourceRoot $name
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $destinationRoot $name) -Force
    }
}

$roots = @{}
foreach ($object in $objects) {
    foreach ($property in @("videos_root", "output_root")) {
        $relativePath = [string]$object.$property
        if ([string]::IsNullOrWhiteSpace($relativePath) -or [IO.Path]::IsPathRooted($relativePath)) {
            throw "o objeto '$($object.object_id)' usa $property nao portatil: $relativePath"
        }
        $fullPath = [IO.Path]::GetFullPath((Join-Path $sourceRoot $relativePath))
        Assert-WithinRoot $sourceRoot $fullPath
        if (-not (Test-Path -LiteralPath $fullPath)) {
            throw "raiz registrada nao existe: $fullPath"
        }
        $relative = Get-PortableRelativePath $sourceRoot $fullPath
        $excluded = if ($property -eq "output_root") { @((Join-Path $fullPath "_cache")) } else { @() }
        $roots[$relative] = [string[]]$excluded
    }
}

foreach ($relative in $roots.Keys) {
    $root = Join-Path $sourceRoot $relative
    Copy-Tree $root (Join-Path $destinationRoot $relative) -ExcludedDirectories ([string[]]$roots[$relative])
}

Write-Host "Workspace portatil preparado em: $destinationRoot"
Write-Host "Caches de proxy (_cache) nao foram copiados; eles serao recriados sob demanda no servidor."
Write-Host "Copie agora a pasta boom-pipeline-prod inteira para o servidor remoto."
