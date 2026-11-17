<#
.SYNOPSIS
Sincroniza el código limpio desde Google Drive hacia un repositorio Git local
FUERA de Google Drive y lo sube a tu cuenta personal de GitHub sin contaminar
la configuración Git corporativa del equipo ni pisar state.json en GitHub.

.EXAMPLE
.\scripts\sync_and_push_github.ps1 -RepoUrl "https://github.com/Andresillo10/poke-alerts.git"
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$RepoUrl,

    [string]$GitUserName = "Cristhian Camelo",
    [string]$GitEmail = "cristhian.camelo@gmail.com",
    [string]$StagingDir = "$env:LOCALAPPDATA\poke-alerts-git"
)

$ErrorActionPreference = "Stop"
$SourceDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

Write-Host "1. Preparando carpeta staging fuera de Google Drive: $StagingDir" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $StagingDir | Out-Null

# Si ya existe el repo en staging, actualiza primero desde origin/main para conservar state.json vivo
Push-Location $StagingDir
try {
    if (Test-Path ".git") {
        git config --local user.name $GitUserName
        git config --local user.email $GitEmail
        git config --local credential.https://github.com.useHttpPath true

        $existingOrigin = git remote 2>$null | Where-Object { $_ -eq "origin" }
        if ($existingOrigin) {
            git remote set-url origin $RepoUrl
        } else {
            git remote add origin $RepoUrl
        }

        Write-Host "   Trayendo ultimos cambios de GitHub (state.json)..." -ForegroundColor Cyan
        $prevPref = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        git pull --rebase --autostash origin main
        $pullExit = $LASTEXITCODE
        $ErrorActionPreference = $prevPref
        if ($pullExit -ne 0) {
            throw "No se pudo sincronizar con origin/main (exit $pullExit). Verifica tus credenciales de GitHub."
        }
    }
}
finally {
    Pop-Location
}

$stagingStateExists = Test-Path (Join-Path $StagingDir ".git")
$excludeFiles = @(".env", "*.pyc")
if ($stagingStateExists) {
    # Protege el state.json ya actualizado en GitHub Actions
    $excludeFiles += "state.json"
}

$robocopyArgs = @(
    $SourceDir,
    $StagingDir,
    "/MIR",
    "/XD", ".git", ".venv", "venv", "out", "preview", "__pycache__", ".pytest_cache",
    "/XF"
) + $excludeFiles

& robocopy @robocopyArgs | Out-Null
if ($LASTEXITCODE -ge 8) {
    throw "Robocopy falló con código $LASTEXITCODE"
}

# Refleja state.json vivo de vuelta en Google Drive sin alterar el código fuente
$stagingStateFile = Join-Path $StagingDir "state.json"
if ($stagingStateExists -and (Test-Path $stagingStateFile)) {
    Copy-Item $stagingStateFile (Join-Path $SourceDir "state.json") -Force
}

Push-Location $StagingDir
try {
    if (-not (Test-Path ".git")) {
        Write-Host "2. Inicializando repositorio Git limpio en rama main..." -ForegroundColor Cyan
        git init -b main
        git config --local user.name $GitUserName
        git config --local user.email $GitEmail
        git config --local credential.https://github.com.useHttpPath true
        git remote add origin $RepoUrl
    }

    Write-Host "3. Verificando pruebas unitarias antes de hacer commit..." -ForegroundColor Cyan
    python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        throw "Las pruebas unitarias fallaron; abortando push."
    }

    git add -A
    $changes = git status --porcelain
    if ($changes) {
        git commit -m "chore: sync poke-alerts from Google Drive workspace"
    } else {
        Write-Host "Sin cambios nuevos para commitear." -ForegroundColor Yellow
    }

    Write-Host "4. Subiendo a GitHub personal ($RepoUrl)..." -ForegroundColor Cyan
    git push -u origin main
}
finally {
    Pop-Location
}
