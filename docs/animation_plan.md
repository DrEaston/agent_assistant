# Animation: a studio for bringing Dieter (and other characters) to life

Goal: Curtis can animate Dieter, the mascot (the small orange spiky lizard-dragon), from still images. Wake-up,
blink and talk come first. The pipeline: **stills** (made with an image generator, e.g. ChatGPT) → **aligned frames**
→ **in-betweens** (cross-fades, a frame-interpolation model, or a keyframe-to-video service fills the gap) →
**clips** with timing → **exports** that any Dieter page can play. ChatGPT and Claude both work in it through the
connector, under the same rules as the Logbook: assistants propose, Curtis approves.

The shot list lives in the Dieter project "Dieter animation studio" (https://askdieter.ai/projects/19) until the
app has its own (step 2 below moves it in). The starting art is in `docs/animation_start/`:
`dieter_sitting_transparent.gif` is the master pose (every new frame is an edit of it; ask for a PNG copy first),
`dieter_sleeping_transparent.gif` starts the wake-up, and the painted/scene versions show the look.

## Intake: where it goes

Run through the intake step in `CLAUDE.md`:

- **A new app**, `apps/animation/`, with its own tile and pages (characters, frames, clips). It's a new kind of thing
  he'll use, with its own pages, so answer 2.
- Other apps never import it. They play a **published clip by its URL** (`/animation/clips/<slug>/...`) using one
  small shared player in `core/ui/` (`dieter-sprite`), the same way pages already share `core/ui` components.
  Kitchen's sleep screen, Ask Dieter's avatar and empty states use the player; only the Animation app makes clips.

## Where the images live (not in SQLite)

The whole database file is downloaded every time the site starts (see the Logbook plan's size cap), so image bytes
must stay out of it:

- **Bytes** go in the Cloud Storage bucket `cloud_persistence.py` already uses, under `animation/<owner>/...`. On a
  developer machine and in tests, a local folder (`DIETER_MEDIA_DIR`, default `data/media/`).
- **Metadata** goes in additive tables in `apps/animation/store.py` (below).
- Pages get images through the app (`/animation/media/<id>`), checked against the signed-in owner. Published clips
  are public by design (they're the mascot); everything else is private.
- Previews copy the database, not the bucket: a preview reads live media but writes to its own
  `animation-preview/` prefix, so nothing in a preview touches the live frames.

## What's stored

| Table | What it holds |
| --- | --- |
| `anim_characters` | id, owner, name, description, style rules (text), master frame id. Dieter is the first. |
| `anim_shots` | The shot list: id, character, code (`sit_eyes-half`), what it should show, which clips need it, status `wanted` / `has_candidates` / `done`. |
| `anim_frames` | One still: id, character, shot (optional), media key, width, height, has alpha, source (`upload`, `chatgpt`, `claude`, `interpolated`, `video`), how it was made (prompt, model or service), status `inbox` / `approved` / `rejected`, created. |
| `anim_alignments` | Per frame, against the character's master: dx, dy, scale, rotation, how it was found (`auto` / `nudged`), a score. Non-destructive: the original pixels are never changed. |
| `anim_clips` | id, character, name/slug, kind (`loop`, `once`, `talk`), the timeline (JSON: frame id, hold ms, transition into the next: `cut`, `fade` + ms, or `inbetween` + set id), status `draft` / `published`, version. |
| `anim_inbetween_jobs` | Request to fill a gap: from frame, to frame, method, count or duration, cost estimate, status `proposed` / `approved` / `running` / `done` / `failed`, result frame ids, log. |
| `anim_proposals` | What an assistant asked for (new frames, a clip change, an in-between job), with its reason, for Curtis to approve, like Logbook proposals. |

## Stages

### 1. Stills: getting images in

Three ways, all landing in the **inbox** (status `inbox`) until he approves:

- **Upload** on the Frames page (PNG or WebP; GIF accepted but warned about, since its 1-bit transparency leaves
  jagged edges on the spikes).
- **ChatGPT** hands over images it generated through a connector tool with a file parameter. In ChatGPT a tool marks
  a parameter as a file with `_meta["openai/fileParams"]`; ChatGPT then sends `{download_url, file_id, mime_type,
  file_name}` and Dieter downloads the bytes from `download_url` straight away (the URL is short-lived). Known
  limits: this is ChatGPT-only (Claude doesn't pass files to tools), and the ChatGPT phone app has been reported to
  send a bare string instead of a URL, so the tool says so and asks him to use the desktop or upload instead.
- **Claude** can't pass image files through the connector. Claude Code on his computer uploads through a small
  command (`scripts/anim_upload.py`, signed in with his Dieter token) or he uploads by hand.

Every frame records how it was made (prompt and generator) so a good frame can be made again.

### 2. Shot list

The app's own shot list replaces project 19's steps: each shot says what to change from the master ("eyelids
halfway down"), and the Frames page shows **what's still missing** for each clip. The prompt template for
ChatGPT (edit the master, change only X, same canvas, transparent PNG) is generated from the character's style rules
and the shot, so every request is worded the same way.

### 3. Align

Generated frames drift a few pixels and a few percent in scale even with "change only X". Alignment, in this
order:

1. **Silhouette**: from the alpha channel, match bounding box and centroid to the master (position and scale).
2. **Refine**: phase correlation on the alpha masks (and on a grey-scale copy for opaque images) for the last pixel
   or two. Pure numpy; no new heavy dependency.
3. **Score**: overlap (IoU) of the aligned silhouette with the master's. Frames below a threshold are flagged
   "needs a look" rather than silently accepted.
4. **Nudge**: arrow keys and a scale slider on an onion-skin view (the frame over a faded master), saved as
   `nudged`.

Parts that are *meant* to differ (an open mouth) shift the silhouette slightly; the onion skin is how he checks.

### 4. In-betweens: filling the gap

Three methods, cheapest first. Each produces candidate frames that go to the inbox like any other frame.

| Method | Where it runs | Good for | Cost |
| --- | --- | --- | --- |
| **Cross-fade** | In the browser, at play time | Blinks, mouth shapes: small changes between aligned frames | Free |
| **Frame interpolation model** (e.g. RIFE or FILM) | Later on his GPU machine (the Residency plan's local server); until then not available | Smooth motion between close frames without inventing new content | Free once set up |
| **Keyframe-to-video service** (start frame + end frame → a short video; e.g. Kling O1 first/last frame, Pika keyframes, PixVerse transition) | Hosted API, called by Dieter | Big pose changes like the wake-up (curled → sitting) that need new drawings in between | Paid per second of video |

For the video route: Dieter sends the two aligned frames and a prompt, gets an MP4, extracts frames at the chosen
rate, removes the background if needed, aligns them, and puts them in the inbox as `video` frames with the job id.
Paid jobs are **always proposals with a cost estimate**; nothing paid runs without his approval, and there's a
monthly cap (`ANIM_MONTHLY_BUDGET_USD`). The service is behind one small interface (`apps/animation/inbetween.py`:
`estimate()`, `submit()`, `poll()`, `fetch()`) so switching services doesn't touch the rest. Which service is decided
at step 6, after comparing a couple on the real wake-up frames.

### 5. Clips

A clip is a timeline of frames with hold times and transitions. The clip editor shows the frames in order, hold
time per frame, the transition into the next, and a live preview using the same player the app pages use. Clip kinds:

- `loop`: idle breathing, sleeping.
- `once`: wake-up, then hands over to another clip (`then: "idle"`).
- `talk`: mouth frames chosen while audio plays (amplitude → closed / slight / ah; later visemes from the text).
- Blink is a **layer** played over idle at random every 3–6 s, so it never needs its own copy of every pose. Layers
  come after single-layer clips work.

### 6. Exports and the player

Publishing a clip writes, under `/animation/clips/<slug>/v<version>/`:

- `sheet.webp` (sprite sheet of the aligned frames) and `clip.json` (frame rects, holds, transitions, loop/then),
- `preview.webp` (animated WebP) for places that just need a moving picture,
- optionally `clip.mp4`.

`core/ui/static/dieter-sprite.js` plays `clip.json` + `sheet.webp` (cross-fades done in the browser), exposes
`play(name)`, `then(name)`, `talk(audioElement)`, and respects reduced-motion settings. Pages refer to clips by
slug; publishing a new version doesn't break a page that's showing the old one.

## Connector tools (ChatGPT and Claude)

Structured JSON, stable ids, scoped to the signed-in user. All listed by `dieter_find_tools`.

| Tool | What it does |
| --- | --- |
| `animation_characters` | Characters with style rules and master frame. |
| `animation_shot_list` | Shots for a character with status and what each clip still needs. |
| `animation_get_frame` | One frame's details, and the image itself (MCP image content, downscaled), so the assistant can look at it. |
| `animation_add_frame` | **ChatGPT only (file parameter).** Adds a generated image to the inbox for a shot, with the prompt used. |
| `animation_propose_shots` | Suggest new shots for a clip (e.g. extra wake-up poses), with reasons. |
| `animation_propose_clip` | Suggest a timeline for a clip from approved frames. |
| `animation_propose_inbetweens` | Ask for in-betweens between two frames: method, count/duration, and the cost estimate Dieter computes. |
| `animation_review` | Alignment scores and "needs a look" flags, so the assistant can say which frames to redo. |

Only Curtis approves frames, clips, publishing and paid jobs; there's no tool for any of those. The connector
instructions gain an "Animation" paragraph restating that.

Later: an **MCP Apps view** (the `ui/*` standard ChatGPT implements) so ChatGPT can show a clip preview or the
onion skin inside the chat. Not needed for v1.

## Order of work

1. **This plan** (docs only).
2. **App skeleton and frames**: `apps/animation/` with manifest and tile, media storage (bucket + local), tables,
   Characters and Frames pages, upload, inbox and approve, shot list (moved in from project 19), tests. Seed Dieter
   with the five starting images.
3. **Align**: silhouette + refine + score, onion-skin nudge, tests with synthetic shapes shifted and scaled by
   known amounts.
4. **Clips and player**: clip editor with cross-fades, `dieter-sprite.js`, publish to sheet + json + animated WebP.
   First real clip: blink.
5. **Connector tools**: the table above, including ChatGPT's file parameter (tested with a fake `download_url`),
   proposals on Today and in the app.
6. **In-betweens**: the interface, one hosted keyframe-to-video service behind it, budget cap, extraction and
   background removal; then the wake-up clip.
7. **Use it around Dieter**: Ask Dieter avatar, empty states, and the kitchen sleep screen (wake-up on webcam wake),
   talk while Dieter speaks.
8. **Later**: local frame interpolation on the GPU machine; layers (blink over anything); visemes from text; the
   MCP Apps preview in ChatGPT.

Each step is its own pull request. Steps 2–4 need no paid service.

## Open questions for Curtis

- Which keyframe-to-video service (step 6)? Compare two on the real wake-up frames before choosing.
- Monthly budget cap for paid in-betweens.
- Should published clips be public (handy for the menu page and sharing), or only for signed-in pages?
- Other characters later, or is it Dieter only for now? (The tables allow more than one either way.)

## Research angle (OpenAI Residency 2027)

Optional, but cheap to keep: every frame records its generator, prompt, alignment transform and score, and every
in-between records its method. That makes a small, clean dataset for questions like "how much does an image
model's 'edit only X' drift, and does alignment + interpolation hide it?" (identity and pose drift measured by
silhouette IoU and a perceptual metric, per method). Not a goal of the app; just don't throw the data away.
