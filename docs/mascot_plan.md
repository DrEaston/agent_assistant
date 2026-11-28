# Dieter, alive: the mascot player and the animation lab

Studio #44 (ChatGPT, for Curtis): bring the kept animations into Dieter's pages so he feels like a calm living
companion. Curtis: "a fancy version of the blinking eyes in cook mode", sitting still most of the time (breathe,
blink), and "we need a process for testing animation sequences".

**Intake:** answer 3, shared. Today, the chat bubble, cook mode and later the kitchen PC all show him, so the player
lives in `core/mascot/` and each app just places it on a page. The test bench is a page under Media (`/media/lab`),
next to the pictures it plays. Not a new app.

## Why not just play the GIFs

A GIF or animated WebP in an `<img>` can't be paused on a frame, can't say when it has finished, and restarts from
frame 1 whenever it's swapped. That is where jumps come from. So:

- **Every clip becomes frames.** Intake decodes each kept GIF/WebP into frames on one shared canvas (512×512,
  transparent), with Dieter scaled and placed the same way in every clip (anchored on his front paws), and saves them
  as one sprite sheet per clip plus a line in a manifest.
- **One `<canvas>` draws him.** The player picks the frame; it always knows where it is, so it can finish a clip,
  hold a frame, or wait for a safe frame before switching.

## The manifest (`core/mascot/clips.json`)

One entry per clip, made by the intake script and edited by hand only for timing:

| Field | Example | Meaning |
| --- | --- | --- |
| `name` | `thinking_hold` | |
| `media_id` | `med-a02ab4d835cd` | the kept picture it came from (only kept ones are allowed) |
| `frames`, `fps` | 12, 10 | |
| `play` | `once` / `loop` / `pingpong` | |
| `starts`, `ends` | `alert` → `thinking` | the pose at each end; a clip may only follow a clip that ends in its start pose |
| `safe_frames` | [0, 6] | frames where a loop may be left early (looks like the start pose) |

Poses so far: `asleep`, `alert`, `thinking`, `listening`, `speaking`.

## The states (what the app tells him)

Pages don't pick clips. They say what's happening, and the player works out how to get there:

| Signal from the page | He does |
| --- | --- |
| nothing going on | **alert**: breathing loop; blink every 3–8 s; a small head move every 30–90 s; a yawn and stretch every few minutes or just after waking. All tunable. |
| `listen()` (mic open / typing) | listening; idle gestures stop |
| `think()` (waiting on an answer) | thinking enter → hold loops for as long as it takes → exit |
| `speak(audio)` | speaking while the audio actually plays, then back to alert; no audio, no mouth |
| `done()` | acknowledgment nod |
| `confused()` | curious tilt (only when Dieter needs to ask something) |
| `sleep()` / `wake()` | going to sleep → sleeping loop; waking up → alert (then maybe a yawn) |

Rules: a new signal waits for the current one-shot to finish or for the next safe frame (no cuts mid-move); if the
target pose differs, he goes through the right in-between clip (e.g. thinking → `thinking_exit` → alert → speaking).
A quick chain of signals keeps only the latest. Hidden tab: stop drawing. "Reduce motion" on the phone: one still,
blinking only. Pictures load before the first frame shows.

## Testing: the animation lab (`/media/lab`)

This is the process for every new or changed clip:

1. **Automatic checks (run in CI, and shown at the top of the lab).** For every clip in the manifest:
   same canvas; paws within 4 px of the anchor; no frame with a visible background (alpha edges); file size.
   For every allowed join (clip A's end → clip B's start, and each loop's last → first frame): a **seam score**
   (how much the picture jumps, 0 = identical). Anything over the limit is listed as a problem with the two frames
   shown, so a jumpy join can't go live unnoticed.
2. **Watch it, step by step.** Pick any clip or join; play at ¼, ½ or full speed; step frame by frame; **onion
   skin** shows A's last frame over B's first so drift is obvious.
3. **Run a script.** Buttons for each signal, plus saved scenarios that run by themselves:
   - "a normal question": wake → listen 3 s → think 8 s → speak 4 s → nod → alert 60 s → sleep
   - "impatient": listen → think 0.5 s → speak → listen again straight away
   - "long wait": think 2 minutes
   - "idle hour, sped up": alert for 60 min at 20× (do blinks, head moves and yawns feel natural, or too often?)
   An event log shows what he was told and which clip played when.
4. **Say what's wrong where it happened.** A **Report this** button on the lab saves a Studio report with the clip,
   the frame number and a picture of the frame, so ChatGPT or Claude can fix that exact asset. Nothing replaces kept
   art silently; a fix comes back through Media for his OK.
5. **Then a page.** Only clips with no open problems are used outside the lab. First page: Today (his face in the
   header). Cook mode comes after Thanksgiving (`apps/kitchen/thanksgiving/` stays untouched until then).

## The jitter problem, and the better rig (phase 2)

Generated clips redraw the whole dragon every frame, so even "breathing only" wobbles. The calm look he wants
comes from not redrawing what shouldn't move:

- **Layers on one fixed body.** One locked still of alert Dieter; only small patches change: eyelids (blink),
  jaw (speaking, driven by the audio's loudness), a gentle rise and fall of the chest (breathing, drawn by the player,
  not generated). Nothing else can drift because nothing else is redrawn.
- Head moves, yawns, thinking and the sleep/wake moves stay as whole clips, used rarely, each starting and ending on
  the locked still.
- Asks for new art become specific: "the alert still with eyes ¼, ½, fully closed, nothing else changed", which is
  much easier to get right than a whole animation.

## Build order

1. `core/mascot/`: intake script (GIF/WebP → frames on the shared canvas, manifest), checks + seam scores, tests.
   Run it over the 14 kept clips and report what fails.
2. `/media/lab`: player + state machine, clip viewer, onion skin, scripted scenarios, Report this.
3. Phase 2 rig: eyelid and jaw layers on the locked still (needs the eyelid/jaw art; ask ChatGPT).
4. Today header; after Thanksgiving, cook mode and the chat bubble.
