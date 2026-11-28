# Media: pictures sent to Dieter

Shared storage (intake answer 3): pictures any app can use. Animation is first (its stills); the Logbook can point at
pictures made from a table. Studio #41 asked for it after the image test (#38–#40) proved ChatGPT can hand Dieter a
file from the phone.

## The contract (same as the Logbook)

1. ChatGPT sends a picture only when Curtis asks, with `media_save_image` (a file parameter, so ChatGPT shows its own
   "Allow?" first). It needs a title and says where it came from (`source`) and the prompt, so it can be made again.
2. It lands as **waiting for his OK**, on his Today page and on `/media`, with a preview and those details.
3. He taps **Keep** or **Discard**. Discarding deletes the file. Waiting pictures he never decides on are deleted
   after 7 days.
4. Assistants use only kept pictures (`media_list` shows kept by default). There's no tool to keep or discard.
5. Pictures he uploads on `/media` himself are kept straight away. Claude can't pass files to tools, so Claude Code
   uploads to `/media/api/upload` with a token from Dieter's own sign-in (he taps Allow once per computer); those
   wait like ChatGPT's, and **Keep all** on `/media` takes a batch at once. The `firefly-intake` skill
   (`.claude/skills/firefly-intake/`) does this for Firefly videos: stills and animations from one clip.

## Where the bytes live

- Not in SQLite (the whole database downloads at every start) and not in the public `/uploads` folder.
- A private folder (`MEDIA_DIR`, default `media/` next to the database), copied to the Cloud Storage bucket under
  `<prefix>/media/<user>/<id>.<ext>` and fetched back **on demand**, so a start doesn't download every picture.
- Served only from `/media/<id>/file`, to the signed-in owner.
- Previews read the live pictures but never write or delete them in Cloud Storage.
- PNG, WebP, JPEG or GIF; up to 25 MB and 40 megapixels; checked with Pillow and stored byte for byte (transparency
  intact). The same bytes twice are saved once.
- ChatGPT's download link is used once (public https only, every redirect checked) and never stored or logged.

## Tools

| Tool | What it does |
| --- | --- |
| `media_save_image` | ChatGPT: attach a picture; saved as waiting. Listed first among Dieter's tools (ChatGPT keeps ~50). |
| `media_list` | Kept (default), waiting or all. Read-only. |
| `media_get` | One picture's size, transparency, source, prompt and status. Read-only. |

After a tool change, ChatGPT only sees it after **Refresh** on chatgpt.com (Settings > Apps > Dieter); reconnecting
doesn't fetch the tool list again.

## Later

- Animation's frames point at Media ids instead of storing their own copies (docs/animation_plan.md).
- Showing a picture to the assistant (MCP image content) when it needs to look, not just read the details.

## Firefly videos (added 2026-11-25)

Firefly can't talk to Dieter, so he uploads its MP4s on /media from his phone (kept straight away, up to 30 MB:
Cloud Run's request limit is 32 MB). Videos play on their Media page (served with byte ranges, which phones need).
When he tells Claude Code "check Media", the `firefly-intake` skill's `inbox` command lists his videos over the API
(`GET /media/api/videos`, Bearer token) and downloads the new ones (`GET /media/api/<id>/file`, kept files only).
What it makes is uploaded with `source` naming the video id, which links the results to the video ("Made from this
video") and marks it as taken in. No new columns: the link is the source text.
