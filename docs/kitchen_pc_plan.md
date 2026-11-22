# The kitchen PC becomes Dieter

Goal: the HP All-in-One in the kitchen stops being "a PC with Dieter open" and becomes Dieter itself: it's
always on, it notices when someone walks in, it looks at them, it listens for "hey Dieter", it answers out loud,
and it shows what's useful right now (timers, what's cooking, the shopping list).

## The big decision: the web app stays the brain, the PC becomes the body

Dieter keeps running on Cloud Run (askdieter.ai). The kitchen PC gets the senses and the face: screen, eyes,
webcam, microphone and speaker, through the helper (`helper/`). The page on the kitchen screen is the normal
Dieter site, and it talks to the helper on `127.0.0.1:47613`.

Why not run the web app on the kitchen PC:

- **One copy of the data.** Live data is one SQLite file restored from Cloud Storage. A second copy running in
  the kitchen would drift from the live one, and whichever saved last would win. The phone and the kitchen
  need to see the same plan, timers and lists.
- **One way to ship changes.** Changes already go issue → pull request → preview → merge → deploy. A local copy
  would need its own update path, and the kitchen would lag behind or break on its own.
- **The PC only needs to do what a website can't:** turn the screen off and on, use the camera and microphone
  without a browser prompt, listen for a wake word all the time, and speak with no tab open. That's the helper.

When to revisit: if the kitchen needs to keep working through internet outages, add a small offline cache in
the helper (the last plan and timers, read-only). It shouldn't become a second server.

## Where things stand (Nov 2026)

- Helper runs from source at sign-in (`Dieter helper` scheduled task). Smart App Control blocks the unsigned
  `DieterHelper.exe` on this PC, so it runs `helper\.venv\Scripts\pythonw.exe dieter_helper.py`.
- Sleep dims the built-in screen to 0 behind a black window. The real "monitor off" made this PC (Modern Standby)
  go into standby and lock, which froze the webcam.
- **One home for Dieter: the Thanksgiving app (cook mode).** The helper draws nothing; it's plumbing (screen,
  webcam, events). Cook mode listens to the helper the whole time it's open. When the screen wakes, the "Talk to
  Dieter" speech bubble comes alive with big cartoon eyes and plays one of five performances in turn
  (`window.dieterBubble` in `tg_voice.html`), then the page fades back in. The tray's "Test the eyes" plays one.
  These page changes only reach the kitchen once the Thanksgiving service is redeployed.
- A Startup-folder shortcut opens askdieter.ai Thanksgiving cook mode full screen in Chrome at sign-in
  (`dieter_page` in `%LOCALAPPDATA%\Dieter\helper.json` changes it).

## Phases

### 1. The kitchen screen as home (next)

- A **Kitchen home page** in `apps/kitchen/` that is the default kitchen screen: clock, what's cooking today,
  running timers, the shopping list, and Dieter's eyes resting in a corner. The helper opens it instead of
  Thanksgiving cook mode once Thanksgiving is over.
- The page **always listens to the helper**, not only after Sleep. That lets the helper tell it "someone's here",
  "going to sleep" or "timer done" at any time, and makes the page's eyes and the helper's eyes one thing.
- **Auto-sleep:** after N minutes with no motion, the screen rests by itself. Motion wakes it.
- **Helper updates itself:** at sign-in it pulls a `kitchen-helper-*` tag (not `main`) and restarts, so a fix
  merged from a phone reaches the kitchen without anyone at the keyboard.

### 2. Voice

- **Wake word on the PC:** an on-device "hey Dieter" model (openWakeWord runs on the CPU, free) in the helper,
  using the webcam's microphone. Nothing is recorded or sent until the wake word is heard.
- **Speech to text** for the request: OpenAI transcription (the site already uses OpenAI), or a local Whisper
  model if it needs to work offline.
- **One "Tell Dieter" endpoint:** generalize Thanksgiving's `voice.py` (understand → check → apply, with undo)
  into a Dieter-wide endpoint, so the same flow can handle the timers, shopping list, schedule and plans.
- **Speaking back:** reuse `/thanksgiving/api/speak`, or Windows voices for instant short replies. The eyes
  show listening, thinking and talking.

### 3. Presence and personality

- The eyes **follow the person**. The helper already works out which side the movement is on, and would stream
  that while awake.
- **Expressions:** sleepy, curious, thinking, happy, "timer's done!".
- **Speaks first, sparingly:** timers, "the pie needs to go in at 3", a short morning rundown when someone
  first walks in. Each of these can be turned off.
- **Privacy stays as it is:** no face recognition, camera frames never leave the PC, and the camera light is on
  whenever the camera is.

### 4. Reliability

- **Health check-in:** the helper reports "alive, version, camera ok" to Dieter every few minutes, and Studio
  shows the kitchen PC's state.
- **Device sign-in:** the helper gets a device token and calls Dieter with signed requests (`core/internal.py`),
  so it can do things without a browser tab, for example speak a timer alert.
- **Packaging:** keep running from source (works with Smart App Control and updates with `git pull`), or pay
  for code signing if the exe matters.

## Questions for Curtis

1. What should the kitchen screen show when nothing is cooking?
2. Does voice need to work when the internet is down?
3. Who else is in the kitchen? This affects how chatty Dieter should be and how strict the wake word is.
4. Is the webcam microphone good enough from across the room, or is a USB speakerphone worth it?
