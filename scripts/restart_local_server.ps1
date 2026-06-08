param(
    [int]$Port = 8000,
    [string]$HostAddress = "0.0.0.0"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($connection in $connections) {
    $processId = $connection.OwningProcess
    if ($processId) {
        Stop-Process -Id $processId -Force
    }
}

Start-Process `
    -FilePath ".\.venv\Scripts\python.exe" `
    -ArgumentList "-m", "uvicorn", "api:app", "--host", $HostAddress, "--port", "$Port" `
    -WindowStyle Hidden

Start-Sleep -Seconds 2
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $listener) {
    throw "Server did not start listening on port $Port."
}

Write-Output "Server listening on $HostAddress`:$Port"
