param(
    [string]$ProjectId = "recipes-442702",
    [string]$ServiceName = "dieter",
    [string]$Region = "us-central1",
    [string]$WorkerProject = "dieter",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

$repo = Resolve-Path (Join-Path $PSScriptRoot "..")
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

$gcloud = Join-Path $env:LOCALAPPDATA "Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
if (-not (Test-Path $gcloud)) {
    $gcloud = "gcloud"
}

$serviceJson = & $gcloud run services describe $ServiceName --region $Region --project $ProjectId --format json
$service = $serviceJson | ConvertFrom-Json
$tokenEntry = $service.spec.template.spec.containers[0].env | Where-Object { $_.name -eq "CODEX_WORKER_TOKEN" } | Select-Object -First 1
if (-not $tokenEntry -or -not $tokenEntry.value) {
    throw "CODEX_WORKER_TOKEN was not found on Cloud Run service '$ServiceName'."
}

$env:CODEX_WORKER_TOKEN = $tokenEntry.value
$env:CODEX_WORKER_PROJECT = $WorkerProject
$codex = $env:CODEX_BIN
if (-not $codex) {
    $codexCommand = Get-Command codex -ErrorAction SilentlyContinue
    if ($codexCommand) {
        $codex = $codexCommand.Source
    }
}
if (-not $codex -or -not (Test-Path $codex)) {
    $fallbackCodex = Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA "OpenAI\Codex\bin") -Recurse -Filter codex.exe -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -ExpandProperty FullName -First 1
    if ($fallbackCodex) {
        $codex = $fallbackCodex
    }
}
if (-not $codex -or -not (Test-Path $codex)) {
    throw "Codex executable was not found. Set CODEX_BIN to the full path of codex.exe."
}
$env:CODEX_BIN = $codex
$workerSlug = $WorkerProject.Replace("_", "-")
$url = "http://127.0.0.1:$Port"
Start-Process $url
& $python (Join-Path $repo "scripts\codex_worker_dashboard.py") --repo $repo --port $Port --project $WorkerProject --worker "curtis-workstation-$workerSlug-codex" --codex $codex
