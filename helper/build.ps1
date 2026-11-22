# Build DieterHelper.exe and the zip to copy to the kitchen PC: helper\dist\DieterHelper.zip
# Run from the repo root or this folder:  powershell -ExecutionPolicy Bypass -File helper\build.ps1
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

if (-not (Test-Path ".venv")) {
    # Python 3.11 or newer (the py launcher picks the newest installed if 3.11 is missing)
    try { py -3.11 -m venv .venv 2>$null } catch { }
    if (-not (Test-Path ".venv")) { py -3 -m venv .venv }
}
.\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt

.\.venv\Scripts\pyinstaller.exe --noconfirm --clean --onefile --noconsole --name DieterHelper `
    --distpath dist --workpath build --specpath build dieter_helper.py

$zip = Join-Path $here "dist\DieterHelper.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "dist\DieterHelper.exe", "install.ps1", "Install Dieter helper.cmd", "README.md" -DestinationPath $zip
Write-Host "Built $zip"
