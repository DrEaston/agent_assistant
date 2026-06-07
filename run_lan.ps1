$ErrorActionPreference = "Stop"

$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
}

$ips = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.PrefixOrigin -ne "WellKnown" } |
    Select-Object -ExpandProperty IPAddress

Write-Host ""
Write-Host "Project Agent local Wi-Fi URLs"
Write-Host "--------------------------------"
Write-Host "This computer: http://localhost:8000"
foreach ($ip in $ips) {
    Write-Host "Phone / other Wi-Fi device: http://$ip`:8000"
    Write-Host "Recipe upload: http://$ip`:8000/apps/recipes/import?project_id=2&action_id=10"
}
Write-Host ""
Write-Host "Leave this window open while using the app."
Write-Host ""

& $python -m uvicorn api:app --host 0.0.0.0 --port 8000
