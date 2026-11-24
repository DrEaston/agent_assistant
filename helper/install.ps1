# Install the Dieter helper for the signed-in Windows user: copy it to %LOCALAPPDATA%\Dieter, start it at
# sign-in (restarting if it stops), and start it now. No administrator rights needed. Run again to update.
# To remove: run with -Uninstall.
param([switch]$Uninstall)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dir = Join-Path $env:LOCALAPPDATA "Dieter"
$exe = Join-Path $dir "DieterHelper.exe"
$task = "Dieter helper"
$shortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Dieter helper.lnk"

Get-Process DieterHelper -ErrorAction SilentlyContinue | Stop-Process -Force
if (Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $task -Confirm:$false
}
if ($Uninstall) {
    Remove-Item $exe -ErrorAction SilentlyContinue
    Remove-Item $shortcut -ErrorAction SilentlyContinue
    Write-Host "Dieter helper removed. (Settings in $dir were kept.)"
    return
}

New-Item -ItemType Directory -Force -Path $dir | Out-Null
Copy-Item (Join-Path $here "DieterHelper.exe") $exe -Force

# A desktop icon to start it by hand (it also starts at sign-in). Only one copy ever runs, so a second click is harmless.
$link = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut)
$link.TargetPath = $exe
$link.WorkingDirectory = $dir
$link.IconLocation = "$exe,0"
$link.Description = "Start the Dieter kitchen helper (screen dimming and webcam wake for cook mode)"
$link.Save()

$action = New-ScheduledTaskAction -Execute $exe
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Turns the kitchen screen off and wakes it when someone walks by (askdieter.ai)." | Out-Null
Start-ScheduledTask -TaskName $task

Start-Sleep -Seconds 3
try {
    $status = Invoke-RestMethod -Uri "http://127.0.0.1:47613/status" -TimeoutSec 5
    Write-Host "Dieter helper $($status.version) is running. Look for the orange face in the tray."
} catch {
    Write-Host "Installed, but it didn't answer yet. Sign out and back in, or run $exe."
}
