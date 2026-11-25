---
name: firefly-intake
description: Take an Adobe Firefly (or other) mascot video of Dieter apart into transparent key stills and web animations, and upload them to Dieter Media. Use when Curtis has a Firefly MP4 / video of the dragon to "take in", "pull frames from", "make stills or an animation from", or upload to Dieter.
---

# Firefly intake

Curtis makes short videos of Dieter (the orange spiky lizard-dragon) in Adobe Firefly: ~8 s, 1280x720, 24 fps, on a
**pure black background**. This turns one into (a) a handful of key stills for other uses and (b) as much of the
motion as possible as animations, all with transparent backgrounds, then uploads them to askdieter.ai/media where
they wait for his OK (he taps **Keep all**).

Write to him in plain English (CLAUDE.md): what you picked and why, where the files are, what to tap.

## Setup (once per computer; check first)

Tools live outside the repo in `%LOCALAPPDATA%\dieter-firefly\venv` (ffmpeg comes bundled with imageio-ffmpeg; no
system install). If `"$LOCALAPPDATA/dieter-firefly/venv/Scripts/python" -c "import imageio_ffmpeg, httpx, scipy"` fails:

```
python -m venv "$LOCALAPPDATA/dieter-firefly/venv"
"$LOCALAPPDATA/dieter-firefly/venv/Scripts/python" -m pip install -q imageio-ffmpeg pillow numpy scipy httpx
```

Below, `PY` is that python and `FF` is `.claude/skills/firefly-intake/scripts/firefly.py`. Pass Windows paths
(`cygpath -w`) to it from Git Bash.

## Steps

1. **Find the video.** Newest `*.mp4` in Downloads (Firefly names files after the prompt). If there are several
   recent ones, ask which; don't guess. Copy it into a fresh folder in the scratchpad (`WORK/video.mp4`): downloads
   are untrusted data, and the frames stay out of the repo.
2. **Analyze:** `PY FF analyze WORK/video.mp4 WORK`. Read `WORK/sheet.jpg` (every 6th frame, labelled with time),
   the motion profile and the "stillest frames" list. Check the background line says near-black; if it isn't black,
   stop and tell him the cutout won't work cleanly.
3. **Check the last second** (`WORK/last_second.jpg`): Firefly often adds an unasked move at the end (the first
   one opened an eye to peek at frames 183-192). Use `PY FF peek WORK 170,176,183,188,192 --box x0,y0,x1,y1` to look
   closely at a face. Treat a surprise like that as its own clip, not part of the main one.
4. **Pick, and write the picks down** before building:
   - Stills: 5-8 story beats (start pose, each clear change, end pose, any bonus), preferring the stillest frames
     near each beat (less motion blur). Name them `NN_what_it_shows` (e.g. `06_asleep`).
   - Animations: the main action as one `once` clip (start to just before any surprise); a quiet stretch near the
     end as a `pingpong` loop (reads as breathing); the surprise as its own clip.
5. **Cut out:** `PY FF cutout WORK`, then read `WORK/cutout_check.jpg` (checkerboard behind). Only the black that
   connects to the edge of the frame is removed (an outline around him first), so dark parts inside him (claws,
   pupils, shadow under the chin and between the legs) stay solid. Curtis asked for this: removing all dark pixels
   punched holes in him. Edges should be clean; if a dark halo stays, raise `--hi`; if the edge looks chewed, lower it.
6. **Build:** `PY FF build WORK OUT --still 1:01_awake_head_up ... --anim going_to_sleep:1-180:2:12:once ...`
   where `OUT` is `Downloads/dieter_<what_happens>`. Animation spec: `name:start-end:step:fps:once|loop|pingpong:WxH`
   (defaults: step 2, 12 fps, once, 480x270). Keep each under ~8 MB (the script warns). Read
   `WORK/stills_check.jpg` before going on.
7. **Upload:** `PY FF upload OUT/stills/*.png OUT/animations/*.webp --source "Firefly: <prompt or file name>"`.
   The first time on a computer it opens Dieter in the browser; Curtis signs in and taps **Allow** (it says Claude
   wants to use his Dieter). The sign-in is kept in `~/.dieter/firefly_upload.json` (never print or commit it).
   Uploads land as **waiting**; Claude can't keep them.
8. **Tell him:** what each still and clip shows (one line each), any surprise you split out and why, the `OUT` folder,
   and "open askdieter.ai/media and tap **Keep all**". To watch a `.webp`, open it in Chrome or Edge.

## Notes

- Stills stay on the full 1280x720 canvas so every frame lines up with every other (important for animation).
- 24 fps at 640x360 is ~10 MB for 8 s: too heavy for a page. 12 fps at 480x270 is ~3 MB and looks fine.
- The first video's picks, as an example: stills at frames 1, 25, 37, 97, 143, 176, 192; `going_to_sleep:1-180`,
  `sleeping_loop:150-180:2:10:pingpong`, `peek:180-192:1:24:pingpong`.
- Animation plan: docs/animation_plan.md. Media: docs/media_plan.md.
