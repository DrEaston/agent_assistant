"""Media tools for ChatGPT and Claude (core/connector).

media_save_image takes a file: ChatGPT marks it with `_meta["openai/fileParams"]` and sends {download_url, file_id,
mime_type, file_name}; Dieter downloads it once and stores it as waiting for Curtis's OK. Claude can't pass files to
tools, so Claude Code uploads on the Media page instead. There is deliberately no tool to keep or discard a picture:
only Curtis does that, on /media.
"""

from urllib.parse import urlsplit

from core.connector.pages import Page
from core.connector.tools import Tool, ToolError, ToolSet, site_url

from . import fetch
from .store import Invalid, MediaStore, WAIT_DAYS

ASSISTANTS = ("chatgpt", "claude")
_state = {"store": None}


def configure(db, blobs):
    _state["store"] = MediaStore(db, blobs)


def store():
    if _state["store"] is None:
        raise ToolError("Media isn't set up on this Dieter.")
    return _state["store"]


def _brief(item):
    out = {"id": item["id"], "title": item["title"], "status": item["status"], "made_by": item["who"],
           "size": f"{item['width']}x{item['height']} {item['format']}", "transparent": item["has_alpha"],
           "url": site_url(item["page"])}
    for field in ("source", "note"):
        if item.get(field):
            out[field] = item[field]
    return out


def save_image(args, download=fetch.download):
    made_by = str(args.get("assistant") or "").strip().lower()
    if made_by not in ASSISTANTS:
        raise ToolError("assistant should be chatgpt or claude (who is sending this).")
    raw = args.get("image")
    if isinstance(raw, str):
        raise ToolError("The image came as text, not a file, so Dieter can't download it. Attach the image itself "
                        "as `image`; if this app can't, Curtis can upload it on the Media page (/media).")
    url = str((raw or {}).get("download_url") or "") if isinstance(raw, dict) else ""
    if not url:
        raise ToolError("No image file came with the call. Attach the image itself as `image`.")
    data, problem = download(url)
    if data is None:
        print(f"Warning: media_save_image couldn't download from {urlsplit(url).hostname}: {problem}")
        raise ToolError(f"Dieter couldn't download the image: {problem}. Try again, or upload it on /media.")
    try:
        item = store().add(data, args.get("title"), made_by, note=args.get("note"), prompt=args.get("prompt"),
                           source=args.get("source"), file_name=raw.get("file_name"))
    except Invalid as exc:
        raise ToolError(str(exc))
    out = _brief(item)
    if item.get("duplicate"):
        out["message"] = f"Dieter already has this exact picture ({item['status']}); nothing new was saved."
    else:
        out["message"] = (f"Saved, waiting for Curtis's OK: he keeps or discards it at {out['url']} (it's on his "
                          f"Today page). Until he keeps it, don't treat it as his. Unkept pictures go after "
                          f"{WAIT_DAYS} days.")
    return out


def list_items(args):
    try:
        items = store().items(str(args.get("show") or "kept"))
    except Invalid as exc:
        raise ToolError(str(exc))
    return {"pictures": [_brief(i) for i in items]}


def get_item(args):
    try:
        item = store().item(args.get("media_id"))
    except Invalid as exc:
        raise ToolError(str(exc))
    out = _brief(item)
    out.update(file_name=item["file_name"], prompt=item["prompt"], bytes=item["bytes"])
    return out


PAGES = [
    Page("media.home", "Media", "His pictures: ones waiting for his OK, and the ones he kept.", "/media"),
    Page("media.item", "A picture", "One picture with where it came from (media_id from media_list).",
         "/media/{media_id}", {"media_id": "slug"}),
]

CHAT_NOTES = ("Media: pictures sent to Dieter wait for his OK on /media; only kept ones are his to use. You can't keep "
              "or discard them.")


def tools():
    return ToolSet("media", [
        Tool("media_save_image", "Save a picture to Dieter",
             "Only when Curtis asks to save or send a picture to Dieter. Attach the image itself as `image` (one per "
             "call). It's stored as waiting for his OK; he keeps or discards it in Dieter. Give it a short title, "
             "and say where it came from in source (e.g. \"made from Logbook: Ragnar legs, v1, verified Nov 24, "
             "2026\") and the prompt you used, so it can be made again.",
             save_image,
             {"image": {"type": "object", "description": "The image file.",
                        "properties": {"download_url": {"type": "string"}, "file_id": {"type": "string"},
                                       "mime_type": {"type": "string"}, "file_name": {"type": "string"}},
                        "required": ["download_url", "file_id"], "additionalProperties": False},
              "title": {"type": "string", "description": "Short, e.g. \"Dieter, eyes half closed\"."},
              "source": {"type": "string", "description": "Optional: what it was made from."},
              "prompt": {"type": "string", "description": "Optional: the prompt that made it."},
              "note": {"type": "string", "description": "Optional: why he wanted it, what it's for."},
              "assistant": {"type": "string", "enum": list(ASSISTANTS), "description": "Who is sending: you."}},
             ("image", "title", "assistant"), meta={"openai/fileParams": ["image"]}),
        Tool("media_list", "Dieter pictures",
             "His pictures, newest first: kept (default; the ones you may use), waiting (sent, not yet OK'd) or all.",
             list_items, {"show": {"type": "string", "enum": ["kept", "waiting", "all"]}}, read_only=True),
        Tool("media_get", "A Dieter picture", "One picture's details: size, transparency, source, prompt, status.",
             get_item, {"media_id": {"type": "string", "description": "From media_list."}}, ("media_id",),
             read_only=True),
    ])
