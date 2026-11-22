---
name: install-kitchen-helper
description: Build and install the Dieter kitchen helper (helper/) on this Windows PC so Thanksgiving cook mode's Sleep button turns the monitor off and the webcam wakes it. Use when asked to install, update, reinstall, test, troubleshoot or remove the Dieter helper / kitchen screen / sleep-and-eyes app on this computer.
---

# Install the Dieter kitchen helper on this PC

You are on the kitchen PC (not Curtis's dev machine). Curtis is not a developer-at-the-keyboard here: tell him
in plain English what you're doing and what he should see. Read `helper/README.md` for what the helper does.

**What it is:** `helper/dieter_helper.py`, a small Windows tray app. It runs a server on `127.0.0.1:47613`
that only askdieter.ai pages may control. When cook mode's 🌙 Sleep is tapped, it turns the monitor fully
off and watches the webcam; when someone walks by it turns the monitor back on and the page shows the eyes.
Settings live in `%LOCALAPPDATA%\Dieter\helper.json` (`sensitivity`: high/medium/low, `camera`: index).

## Steps

1. **Check where you are.** This must be Windows and the repo root (has `helper/build.ps1`). If the repo
   isn't here, clone it: `git clone https://github.com/DrEaston/agent_assistant.git` and work in it.
   Pull the latest: `git pull`.

2. **Python 3.11+.** Check `py -3.11 --version` (or `py -3 --version`). If none, ask Curtis before
   installing, then: `winget install -e --id Python.Python.3.11 --accept-source-agreements --accept-package-agreements`
   and open a new shell so `py` is on PATH.

3. **Build.** `powershell -NoProfile -ExecutionPolicy Bypass -File helper\build.ps1`
   (creates `helper\.venv`, installs `helper\requirements.txt`, runs PyInstaller; takes a few minutes).
   Result: `helper\dist\DieterHelper.exe` and `helper\dist\DieterHelper.zip`.

4. **Install.** Copy the installer next to the exe and run it:
   ```
   Copy-Item helper\install.ps1 helper\dist\ -Force
   powershell -NoProfile -ExecutionPolicy Bypass -File helper\dist\install.ps1
   ```
   It copies the exe to `%LOCALAPPDATA%\Dieter`, registers the "Dieter helper" scheduled task (at sign-in,
   restarts if it stops, no admin needed) and starts it. Running it again updates in place.

5. **Verify** (don't turn the monitor off yourself; Curtis may be using the screen):
   - `Invoke-RestMethod http://127.0.0.1:47613/status -Headers @{Origin="https://askdieter.ai"}` → `ok: True`
     and the version.
   - Camera check with the build venv (opens and releases the webcam; the light blinks briefly):
     `helper\.venv\Scripts\python.exe -c "import cv2; c=cv2.VideoCapture(0, cv2.CAP_DSHOW); print('camera ok' if c.isOpened() and c.read()[0] else 'camera NOT ok'); c.release()"`
     If it fails, try indexes 1 and 2; if one works, set `"camera": <n>` in `%LOCALAPPDATA%\Dieter\helper.json`
     and restart the task (`Stop-ScheduledTask`/`Start-ScheduledTask -TaskName "Dieter helper"`).
   - `Get-ScheduledTask -TaskName "Dieter helper"` exists.

6. **Hand over to Curtis** with these steps:
   - The orange face is in the tray (^ arrow). Right-click: Sleep, Test the eyes, sensitivity, Quit.
   - Open askdieter.ai → Thanksgiving → Open cook mode in Chrome or Edge. If the browser asks to allow
     access to apps/devices on this computer or local network, click **Allow**.
   - Tap 🌙 Sleep, step away for a few seconds, then walk past the webcam.
   - Too jumpy or not waking: change sensitivity from the tray menu.

## Troubleshooting

- **Page doesn't find the helper** (Sleep only blacks out the page): check `/status` above; check the browser
  didn't block local network access (site settings for askdieter.ai → allow "Local network access" /
  "Apps on device"); the helper only accepts the origins listed in `ALLOWED_ORIGINS` in `dieter_helper.py`.
- **Port 47613 in use:** `Get-NetTCPConnection -LocalPort 47613` to find the process; an old copy is usually
  the cause (`Get-Process DieterHelper | Stop-Process`), then start the task again.
- **Monitor doesn't come back on with motion** but does with the mouse: that's a real bug in the wake code
  (`Monitor.on`) for this monitor; report exactly what happened to Curtis and, if he wants, fix it in the
  repo, rebuild, reinstall, and commit on a branch (see `CLAUDE.md`).
- **Remove:** `powershell -ExecutionPolicy Bypass -File helper\dist\install.ps1 -Uninstall`.

Never commit `helper\.venv`, `helper\build` or `helper\dist` (they're gitignored).
