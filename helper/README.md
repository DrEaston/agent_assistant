# Dieter helper (kitchen PC)

Turns the kitchen monitor fully off when you tap **Sleep** in Thanksgiving cook mode, watches the webcam for
someone walking by, and turns the monitor back on. The Dieter page then shows the eyes and fades back in.
Step alerts and finished timers wake it too.

## Install

1. Unzip `DieterHelper.zip` anywhere (Downloads is fine).
2. Double-click **Install Dieter helper.cmd**. It copies the helper to `%LOCALAPPDATA%\Dieter`, starts it now, and
   starts it every time you sign in.
3. Look for the small orange face in the tray (you may need the ^ arrow). Right-click it for Sleep, Test the
   eyes, motion sensitivity, and Quit.
4. Open askdieter.ai → Thanksgiving → cook mode in Chrome or Edge. The first time, the browser may ask to let
   askdieter.ai "access other apps and services on this device" (or "devices on your local network"): choose
   **Allow**. That's how the page talks to the helper.

## Privacy

- The camera is only on while the screen is asleep (the webcam light shows it). Frames stay in memory, are only
  compared with each other, and are never saved or sent.
- The helper only listens on this computer (127.0.0.1), and only askdieter.ai pages may tell it what to do.

## Remove

Run `powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall` from the unzipped folder, or delete the
"Dieter helper" task in Task Scheduler and `%LOCALAPPDATA%\Dieter\DieterHelper.exe`.

## Build (developers)

`powershell -ExecutionPolicy Bypass -File helper\build.ps1` makes `helper\dist\DieterHelper.zip`.
