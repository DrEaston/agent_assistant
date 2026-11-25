"""The Media pages (/media): pictures waiting for his OK, the ones he kept, and an upload form.

Only these pages keep or discard a picture (they need his signed-in session; the connector can't). The file itself
is served from /media/<id>/file to its owner only, never from the public /uploads folder.
"""

import re
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from core import ui
from database import reset_current_user_id, set_current_user_id

from . import connector
from .store import MAX_BYTES, MAX_VIDEO_BYTES, WAIT_DAYS, Invalid, who

APP = {"id": "media", "title": "Media", "nav_label": "Media", "accent": "today"}
STATUS_TONE = {"waiting": "info", "kept": "ok", "discarded": None}

router = APIRouter()
_api = {"authenticate": None}  # request -> user or None: Dieter's connector sign-in (a Bearer token), set by the site


def configure_api(authenticate):
    _api["authenticate"] = authenticate
_templates = ui.templates(Path(__file__).parent / "templates")
_templates.globals.update(who=who, tone=STATUS_TONE.get, wait_days=WAIT_DAYS)


def render(template, request, status_code=200, **context):
    context.setdefault("app", APP)
    context.setdefault("page_url", str(request.url.path))
    return HTMLResponse(_templates.get_template(template).render(**context), status_code=status_code)


def back(url, message=None, error=None):
    sep = "&" if "?" in url else "?"
    if message:
        url += f"{sep}done={quote(message)}"
    elif error:
        url += f"{sep}error={quote(error)}"
    return RedirectResponse(url, status_code=303)


def _notes(request):
    return {"done": request.query_params.get("done"), "error": request.query_params.get("error")}


@router.get("/media")
def media_home(request: Request):
    s = connector.store()
    return render("media_home.html", request, waiting=s.items("waiting"), kept=s.items("kept"), **_notes(request))


@router.post("/media/upload")
async def media_upload(request: Request, file: UploadFile = File(...), title: str = Form(""), source: str = Form("")):
    data = await file.read(max(MAX_BYTES, MAX_VIDEO_BYTES) + 1)
    title = " ".join(title.split()) or Path(file.filename or "").stem.replace("_", " ").strip() or "Picture"
    try:
        item = connector.store().add(data, title, "you", source=source, file_name=file.filename or "")
    except Invalid as exc:
        return back("/media", error=str(exc))
    saved = ("Saved. Tell Claude \"check Media\" to turn it into stills and animations." if item["kind"] == "video"
             else "Saved.")
    return back(item["page"], message="Already here: this exact file was saved before." if item.get("duplicate")
                else saved)


@router.post("/media/keep-all")
def media_keep_all():
    s = connector.store()
    waiting = s.items("waiting")
    for item in waiting:
        s.keep(item["id"])
    return back("/media", message=f"Kept {len(waiting)} picture{'s' if len(waiting) != 1 else ''}.")


def _api_user(request):
    return _api["authenticate"](request) if _api["authenticate"] else None


def _unauthorized():
    return JSONResponse({"error": "Sign in to Dieter first (Bearer token from /oauth)."}, status_code=401,
                        headers={"WWW-Authenticate": "Bearer"})


@router.get("/media/api/videos")
def media_api_videos(request: Request):
    """Claude Code's Firefly intake: the videos he uploaded, and whether each has been taken in yet."""
    user = _api_user(request)
    if not user:
        return _unauthorized()
    token = set_current_user_id(user["id"])
    try:
        videos = connector.store().videos()
    finally:
        reset_current_user_id(token)
    return {"videos": [{k: v[k] for k in ("id", "title", "source", "file_name", "width", "height", "bytes",
                                         "created_at", "taken_in")} for v in videos]}


@router.get("/media/api/{media_id}/file")
def media_api_file(request: Request, media_id: str):
    """A kept file, for Claude Code (Bearer token)."""
    user = _api_user(request)
    if not user:
        return _unauthorized()
    token = set_current_user_id(user["id"])
    try:
        s = connector.store()
        if s.item(media_id)["status"] != "kept":
            return JSONResponse({"error": "Only kept files can be fetched."}, status_code=403)
        data, mime = s.read(media_id)
    except Invalid as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    finally:
        reset_current_user_id(token)
    return Response(data, media_type=mime, headers={"X-Content-Type-Options": "nosniff"})


@router.post("/media/api/upload")
async def media_api_upload(request: Request, file: UploadFile = File(...), title: str = Form(""),
                           source: str = Form(""), prompt: str = Form(""), note: str = Form("")):
    """Claude Code's upload (the Firefly intake skill): signed in with a Bearer token from Dieter's own sign-in, like
    /mcp. The picture waits for his OK like anything an assistant sends."""
    user = _api_user(request)
    if not user:
        return _unauthorized()
    data = await file.read(MAX_BYTES + 1)
    token = set_current_user_id(user["id"])
    try:
        item = connector.store().add(data, title, "claude", note=note, prompt=prompt, source=source,
                                     file_name=file.filename or "")
    except Invalid as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    finally:
        reset_current_user_id(token)
    return {"id": item["id"], "status": item["status"], "duplicate": bool(item.get("duplicate")),
            "url": str(request.base_url).rstrip("/") + item["page"]}


@router.get("/media/{media_id}")
def media_item(request: Request, media_id: str):
    try:
        item = connector.store().item(media_id)
    except Invalid as exc:
        return render("media_home.html", request, status_code=404, waiting=[], kept=[], error=str(exc))
    made = connector.store().made_from(item["id"]) if item["kind"] == "video" else []
    return render("media_item.html", request, item=item, made=made, **_notes(request))


@router.get("/media/{media_id}/file")
def media_file(request: Request, media_id: str):
    try:
        data, mime = connector.store().read(media_id)
    except Invalid:
        return Response(status_code=404)
    headers = {"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff", "Accept-Ranges": "bytes"}
    # Phones (Safari especially) only play a video if it can be fetched in pieces.
    wanted = re.fullmatch(r"bytes=(\d*)-(\d*)", (request.headers.get("range") or "").strip())
    if wanted and (wanted[1] or wanted[2]):
        size = len(data)
        if wanted[1]:
            start, end = int(wanted[1]), min(int(wanted[2]) if wanted[2] else size - 1, size - 1)
        else:
            start, end = max(0, size - int(wanted[2])), size - 1
        if start >= size or start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        return Response(data[start:end + 1], status_code=206, media_type=mime,
                        headers={**headers, "Content-Range": f"bytes {start}-{end}/{size}"})
    return Response(data, media_type=mime, headers=headers)


@router.post("/media/{media_id}/keep")
def media_keep(media_id: str):
    try:
        connector.store().keep(media_id)
    except Invalid as exc:
        return back(f"/media/{quote(media_id)}", error=str(exc))
    return back(f"/media/{quote(media_id)}", message="Kept. ChatGPT and Claude can use it now.")


@router.post("/media/{media_id}/discard")
def media_discard(media_id: str):
    try:
        connector.store().discard(media_id)
    except Invalid as exc:
        return back(f"/media/{quote(media_id)}", error=str(exc))
    return back("/media", message="Discarded and deleted.")
