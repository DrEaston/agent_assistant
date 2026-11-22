"""Music app routes: playlist drafts from dictation, Spotify connect and export.

Moved out of api.py (the first app to move; Stage 1 of the split plan). The main app calls configure() at startup
and includes `router`. Ask Dieter's playlist requests come through message_requests_playlist_action() and
handle_playlist_action_request().
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode, urlsplit, urlunsplit, parse_qsl

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from database import get_current_user_id

router = APIRouter()

# Set by configure(): the shared database, the page template environment, and helpers the main app owns.
db = None
jinja_env = None
llm_service = None
_platform = {}


def configure(database, templates, llm=None, **helpers):
    global db, jinja_env, llm_service
    db, jinja_env, llm_service = database, templates, llm
    _platform.update(helpers)
    for name, fn in helpers.items():
        globals()[name] = fn


def dict_from_row(row):
    """Convert sqlite rows to plain dictionaries."""
    return dict(row) if row else None


def dicts_from_rows(rows):
    return [dict(row) for row in rows]


REGISTRATION_CODE = os.getenv("DIETER_REGISTRATION_CODE", "")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "")
SPOTIFY_ACCOUNT_BASE = "https://accounts.spotify.com"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
SPOTIFY_SCOPES = "playlist-modify-private playlist-modify-public playlist-read-private user-read-private user-read-email"


def spotify_configured():
    """Return true when Spotify OAuth credentials are available."""
    return bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET)


def spotify_callback_url(request):
    """Build the Spotify OAuth callback URL."""
    if SPOTIFY_REDIRECT_URI:
        return SPOTIFY_REDIRECT_URI
    return str(request.url_for("music_spotify_callback"))


def spotify_oauth_state(user_id):
    """Create a state token tied to the active Dieter user."""
    payload = str(user_id or 0)
    secret = (os.getenv("SECRET_KEY") or REGISTRATION_CODE or "dieter-local-secret").encode("utf-8")
    signature = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def verify_spotify_oauth_state(state, user_id):
    """Validate Spotify OAuth state."""
    return bool(state and hmac.compare_digest(state, spotify_oauth_state(user_id)))


def spotify_http_json(path_or_url, method="GET", access_token="", data=None, form=None):
    """Call Spotify Accounts or Web API and return decoded JSON."""
    url = path_or_url if str(path_or_url).startswith("http") else f"{SPOTIFY_API_BASE}{path_or_url}"
    body = None
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if form is not None:
        body = urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        credentials = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode("utf-8")
        headers["Authorization"] = f"Basic {base64.b64encode(credentials).decode('ascii')}"
    elif data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"Spotify request failed: {exc.code} {detail[:300]}")
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Spotify: {exc.reason}")


def exchange_spotify_code(code, request):
    """Exchange a Spotify authorization code for tokens."""
    return spotify_http_json(
        f"{SPOTIFY_ACCOUNT_BASE}/api/token",
        method="POST",
        form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": spotify_callback_url(request),
        },
    )


def refresh_playlist_spotify_token(profile):
    """Refresh or return a valid Spotify access token."""
    if not profile or not profile.get("spotify_refresh_token"):
        raise HTTPException(status_code=400, detail="Connect Spotify before submitting playlists.")
    expires_at = int(profile.get("spotify_token_expires_at") or 0)
    if profile.get("spotify_access_token") and expires_at > int(time.time()) + 60:
        return profile.get("spotify_access_token")
    payload = spotify_http_json(
        f"{SPOTIFY_ACCOUNT_BASE}/api/token",
        method="POST",
        form={
            "grant_type": "refresh_token",
            "refresh_token": profile.get("spotify_refresh_token"),
        },
    )
    db.update_playlist_spotify_tokens(
        access_token=payload.get("access_token", ""),
        refresh_token=payload.get("refresh_token", profile.get("spotify_refresh_token", "")),
        expires_at=int(time.time()) + int(payload.get("expires_in", 3600)),
        scope=payload.get("scope", profile.get("spotify_scope", "")),
    )
    return payload.get("access_token", "")


def spotify_current_user(access_token):
    """Get the connected Spotify user profile."""
    return spotify_http_json("/me", access_token=access_token)


def spotify_search_track(access_token, title, artist=""):
    """Search Spotify for the best track match."""
    query = f'track:"{title}"'
    if artist:
        query += f' artist:"{artist}"'
    params = urlencode({"q": query, "type": "track", "limit": 5})
    result = spotify_http_json(f"/search?{params}", access_token=access_token)
    items = (((result or {}).get("tracks") or {}).get("items") or [])
    candidates = []
    for track in items:
        candidates.append({
            "id": track.get("id", ""),
            "uri": track.get("uri", ""),
            "name": track.get("name", ""),
            "artists": ", ".join(artist_item.get("name", "") for artist_item in track.get("artists", [])),
            "url": ((track.get("external_urls") or {}).get("spotify") or ""),
        })
    return candidates


def spotify_create_playlist(access_token, title, description="", is_public=False):
    """Create an empty Spotify playlist for the current user."""
    return spotify_http_json(
        "/me/playlists",
        method="POST",
        access_token=access_token,
        data={"name": title, "description": description, "public": bool(is_public)},
    )


def spotify_add_playlist_items(access_token, playlist_id, uris):
    """Add tracks to a Spotify playlist in batches."""
    snapshot = ""
    for index in range(0, len(uris), 100):
        payload = spotify_http_json(
            f"/playlists/{playlist_id}/tracks",
            method="POST",
            access_token=access_token,
            data={"uris": uris[index:index + 100]},
        )
        snapshot = payload.get("snapshot_id", snapshot)
    return snapshot


def parse_playlist_song_line(line):
    """Parse a dictated song line into title/artist fields."""
    text = re.sub(r"^\s*(?:\d+[\).\s-]*|[-*]\s*)", "", line or "").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return None
    for sep in [" by ", " - ", " -- ", " — "]:
        if sep in text:
            left, right = text.split(sep, 1)
            return {"raw_text": text, "title": left.strip(), "artist": right.strip()}
    return {"raw_text": text, "title": text, "artist": ""}


def parse_playlist_dictation(text):
    """Extract playlist title and song rows from free text."""
    source = text or ""
    title = ""
    title_match = re.search(r"(?:playlist (?:called|named)|call it|title)\s+['\"]?([^'\"\n.;]+)", source, flags=re.IGNORECASE)
    if title_match:
        title = title_match.group(1).strip()
    lines = [line.strip() for line in re.split(r"[\n;]+", source) if line.strip()]
    songs = []
    for line in lines:
        cleaned = re.sub(r"^(?:add|include|song|songs|playlist|called|named)\b[:\s-]*", "", line, flags=re.IGNORECASE).strip()
        if re.match(r"^(?:make|create|new)\s+(?:a\s+)?playlist\s+(?:called|named)\b", cleaned, flags=re.IGNORECASE):
            continue
        if title and title.lower() in cleaned.lower() and len(lines) > 1:
            continue
        if "," in cleaned and not re.search(r"\bby\b|\s-\s", cleaned, flags=re.IGNORECASE):
            pieces = [piece.strip() for piece in cleaned.split(",") if piece.strip()]
        else:
            pieces = [cleaned]
        for piece in pieces:
            song = parse_playlist_song_line(piece)
            if song and len(song["title"]) > 1 and not re.match(r"^(make|create|new)\s+playlist$", song["title"], flags=re.IGNORECASE):
                songs.append(song)
    return {"title": title or "Dieter Music Playlist", "songs": songs}


def parse_playlist_target(page_url):
    """Find playlist and repository ids from the Dieter Music page URL."""
    try:
        params = dict(parse_qsl(urlsplit(page_url or "").query, keep_blank_values=True))
        playlist_target = params.get("playlist_id")
        collection_target = params.get("collection_id")
        return {
            "playlist_id": int(playlist_target) if playlist_target and str(playlist_target).isdigit() else 0,
            "collection_id": int(collection_target) if collection_target and str(collection_target).isdigit() else 0,
        }
    except (TypeError, ValueError):
        return {"playlist_id": 0, "collection_id": 0}


def message_requests_playlist_action(text, page_url=""):
    """Detect Ask Dieter playlist dictation/edit requests."""
    normalized = (text or "").lower()
    issue_cues = [
        "bug",
        "broken",
        "doesn't work",
        "does not work",
        "isn't working",
        "not working",
        "wrong",
        "problem",
        "issue",
        "error",
        "feedback",
        "fix",
        "won't connect",
        "wont connect",
        "can't connect",
        "cannot connect",
    ]
    playlist_action_cues = [
        "make a playlist",
        "create a playlist",
        "new playlist",
        "add song",
        "add songs",
        "include song",
        "include songs",
        "call it",
        "playlist called",
        "playlist named",
    ]
    if any(cue in normalized for cue in issue_cues) and not any(cue in normalized for cue in playlist_action_cues):
        return False
    if (page_url or "").startswith(("/apps/music/playlists", "/apps/playlists")):
        if any(cue in normalized for cue in playlist_action_cues):
            return True
        return bool(re.search(r"\b(song|songs)\b", normalized) and re.search(r"\b(add|include|remove|delete)\b", normalized))
    return any(cue in normalized for cue in ["parrisa playlist", "spotify playlist", "make a playlist", "create a playlist"])


def add_songs_to_playlist_draft(playlist_id, songs):
    """Add parsed song rows to a draft."""
    added = 0
    for song in songs:
        db.add_playlist_item(
            playlist_id,
            raw_text=song.get("raw_text", ""),
            title=song.get("title", ""),
            artist=song.get("artist", ""),
        )
        added += 1
    return added


def playlist_redirect_url(playlist_id, **params):
    """Build a Dieter Music URL that keeps the playlist's repository selected."""
    playlist = dict_from_row(db.get_playlist_draft(playlist_id)) if playlist_id else None
    query = {}
    if playlist and playlist.get("collection_id"):
        query["collection_id"] = playlist["collection_id"]
    if playlist_id:
        query["playlist_id"] = playlist_id
    for key, value in params.items():
        if value:
            query[key] = value
    return f"/apps/music/playlists?{urlencode(query)}" if query else "/apps/music/playlists"


def handle_playlist_action_request(message, page_url):
    """Create or edit playlist drafts from Ask Dieter."""
    parsed = parse_playlist_dictation(message.content)
    target = parse_playlist_target(page_url)
    playlist_id = target["playlist_id"]
    collection_id = target["collection_id"] or db.ensure_default_playlist_collection()
    playlist = dict_from_row(db.get_playlist_draft(playlist_id)) if playlist_id else None
    created = False
    if not playlist:
        playlist_id = db.add_playlist_draft(parsed["title"], collection_id=collection_id)
        playlist = dict_from_row(db.get_playlist_draft(playlist_id))
        created = True
    elif parsed.get("title") and re.search(r"\b(rename|call it|title|named)\b", message.content or "", flags=re.IGNORECASE):
        db.update_playlist_draft(playlist_id, title=parsed["title"])
        playlist["title"] = parsed["title"]
    added = add_songs_to_playlist_draft(playlist_id, parsed["songs"])
    if not added and not created:
        return {
            "assistant_message": "I found the playlist, but I did not detect any new songs. Dictate songs like `Song Title by Artist`, one per line.",
            "changed_fields": [],
            "playlist_context": True,
            "redirect_url": playlist_redirect_url(playlist_id),
            "redirect_label": "Open Playlist",
        }
    return {
        "assistant_message": f"{'Created' if created else 'Updated'} {playlist.get('title') or parsed['title']} with {added} song{'s' if added != 1 else ''}. Review matches before submitting to Spotify.",
        "changed_fields": ["playlist_draft", "playlist_items"],
        "playlist_context": True,
        "redirect_url": playlist_redirect_url(playlist_id),
        "redirect_label": "Open Playlist",
    }


def playlist_context(request, playlist_id=0, collection_id=0):
    """Build Dieter Music template context."""
    profile = dict_from_row(db.get_playlist_profile())
    default_collection_id = db.ensure_default_playlist_collection()
    if default_collection_id:
        db.assign_uncollected_playlist_drafts(default_collection_id)
    collections = dicts_from_rows(db.get_playlist_collections())
    selected_collection = None
    if collection_id:
        selected_collection = dict_from_row(db.get_playlist_collection(collection_id))
    if not selected_collection and collections:
        selected_collection = collections[0]
        collection_id = selected_collection["id"]
    playlists = dicts_from_rows(db.get_playlist_drafts(limit=50, collection_id=collection_id))
    selected = None
    if playlist_id:
        selected = dict_from_row(db.get_playlist_draft(playlist_id))
        if selected and collection_id and selected.get("collection_id") != collection_id:
            selected = None
    if not selected and playlists:
        selected = playlists[0]
    items = dicts_from_rows(db.get_playlist_items(selected["id"])) if selected else []
    return {
        "request": request,
        "spotify_configured": spotify_configured(),
        "spotify_connected": bool(profile and profile.get("spotify_refresh_token")),
        "playlist_profile": profile,
        "playlist_collections": collections,
        "selected_collection": selected_collection,
        "playlists": playlists,
        "selected_playlist": selected,
        "playlist_items": items,
    }


@router.get("/apps/playlists")
def legacy_playlists_app(request: Request):
    """Redirect the old Dieter Music path to the canonical music route."""
    query = request.url.query
    suffix = f"?{query}" if query else ""
    return RedirectResponse(url=f"/apps/music/playlists{suffix}", status_code=303)


@router.get("/apps/music/playlists")
def playlists_app(request: Request, playlist_id: int = 0, collection_id: int = 0):
    """Dieter Music home."""
    template = jinja_env.get_template("playlists.html")
    return HTMLResponse(template.render(playlist_context(request, playlist_id, collection_id)))


@router.post("/apps/music/playlists/collections/create")
@router.post("/apps/playlists/collections/create")
def create_playlist_collection(
    title: str = Form("Parrisa's Playlists"),
    description: str = Form(""),
    visibility: str = Form("visible"),
):
    """Create a visible playlist repository."""
    collection_id = db.add_playlist_collection(title, description=description, visibility=visibility)
    return RedirectResponse(url=f"/apps/music/playlists?collection_id={collection_id}", status_code=303)


@router.post("/apps/music/playlists/create")
@router.post("/apps/playlists/create")
def create_playlist_draft(
    title: str = Form("Dieter Music Playlist"),
    description: str = Form(""),
    is_public: str = Form(""),
    collection_id: int = Form(0),
):
    """Create a playlist draft."""
    target_collection_id = collection_id or db.ensure_default_playlist_collection()
    playlist_id = db.add_playlist_draft(title, description=description, is_public=bool(is_public), collection_id=target_collection_id)
    return RedirectResponse(url=playlist_redirect_url(playlist_id), status_code=303)


@router.post("/apps/music/playlists/{playlist_id}/update")
@router.post("/apps/playlists/{playlist_id}/update")
def update_playlist_draft_form(
    playlist_id: int,
    title: str = Form(""),
    description: str = Form(""),
    is_public: str = Form(""),
    collection_id: int = Form(0),
):
    """Update playlist metadata."""
    db.update_playlist_draft(playlist_id, title=title, description=description, is_public=bool(is_public), collection_id=collection_id or None)
    return RedirectResponse(url=playlist_redirect_url(playlist_id), status_code=303)


@router.post("/apps/music/playlists/{playlist_id}/items/add")
@router.post("/apps/playlists/{playlist_id}/items/add")
def add_playlist_item_form(
    playlist_id: int,
    title: str = Form(""),
    artist: str = Form(""),
    raw_text: str = Form(""),
):
    """Add one song to a playlist draft."""
    if raw_text and not title:
        parsed = parse_playlist_song_line(raw_text) or {}
        title = parsed.get("title", title)
        artist = parsed.get("artist", artist)
    db.add_playlist_item(playlist_id, raw_text=raw_text or f"{title} by {artist}".strip(), title=title, artist=artist)
    return RedirectResponse(url=playlist_redirect_url(playlist_id), status_code=303)


@router.post("/apps/music/playlists/items/{item_id}/update")
@router.post("/apps/playlists/items/{item_id}/update")
def update_playlist_item_form(
    item_id: int,
    playlist_id: int = Form(...),
    title: str = Form(""),
    artist: str = Form(""),
    position: int = Form(0),
):
    """Edit one playlist song row."""
    db.update_playlist_item(item_id, title=title, artist=artist, position=position, match_status="unmatched", spotify_track_id="", spotify_uri="", spotify_url="")
    return RedirectResponse(url=playlist_redirect_url(playlist_id), status_code=303)


@router.post("/apps/music/playlists/items/{item_id}/delete")
@router.post("/apps/playlists/items/{item_id}/delete")
def delete_playlist_item_form(item_id: int, playlist_id: int = Form(...)):
    """Delete one playlist song row."""
    db.delete_playlist_item(item_id)
    return RedirectResponse(url=playlist_redirect_url(playlist_id), status_code=303)


@router.get("/apps/music/playlists/spotify/connect")
@router.get("/apps/playlists/spotify/connect")
def playlists_spotify_connect(request: Request):
    """Send the active user to Spotify OAuth."""
    if not spotify_configured():
        raise HTTPException(status_code=400, detail="Spotify is not configured. Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.")
    user_id = get_current_user_id()
    params = {
        "client_id": SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": spotify_callback_url(request),
        "scope": SPOTIFY_SCOPES,
        "state": spotify_oauth_state(user_id),
        "show_dialog": "false",
    }
    return RedirectResponse(url=f"{SPOTIFY_ACCOUNT_BASE}/authorize?{urlencode(params)}", status_code=303)


@router.get("/apps/music/playlists/spotify/callback", name="music_spotify_callback")
@router.get("/apps/playlists/spotify/callback", name="playlists_spotify_callback")
def playlists_spotify_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Handle Spotify OAuth callback."""
    user_id = get_current_user_id()
    if error:
        raise HTTPException(status_code=400, detail=f"Spotify authorization failed: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing Spotify authorization code.")
    if not verify_spotify_oauth_state(state, user_id):
        raise HTTPException(status_code=400, detail="Spotify authorization state did not match.")
    payload = exchange_spotify_code(code, request)
    access_token = payload.get("access_token", "")
    spotify_user = spotify_current_user(access_token) if access_token else {}
    db.update_playlist_spotify_tokens(
        spotify_user_id=spotify_user.get("id", ""),
        display_name=spotify_user.get("display_name", "") or spotify_user.get("email", ""),
        access_token=access_token,
        refresh_token=payload.get("refresh_token", ""),
        expires_at=int(time.time()) + int(payload.get("expires_in", 3600)),
        scope=payload.get("scope", SPOTIFY_SCOPES),
    )
    return RedirectResponse(url="/apps/music/playlists?spotify_connected=1", status_code=303)


@router.post("/apps/music/playlists/spotify/disconnect")
@router.post("/apps/playlists/spotify/disconnect")
def playlists_spotify_disconnect():
    """Disconnect Spotify."""
    db.clear_playlist_spotify_tokens()
    return RedirectResponse(url="/apps/music/playlists", status_code=303)


@router.post("/apps/music/playlists/{playlist_id}/spotify/match")
@router.post("/apps/playlists/{playlist_id}/spotify/match")
def match_playlist_tracks(playlist_id: int):
    """Resolve playlist draft rows to Spotify track URIs."""
    profile = dict_from_row(db.get_playlist_profile())
    access_token = refresh_playlist_spotify_token(profile)
    items = dicts_from_rows(db.get_playlist_items(playlist_id))
    for item in items:
        candidates = spotify_search_track(access_token, item.get("title", ""), item.get("artist", ""))
        best = candidates[0] if candidates else {}
        db.update_playlist_item(
            item["id"],
            spotify_track_id=best.get("id", ""),
            spotify_uri=best.get("uri", ""),
            spotify_url=best.get("url", ""),
            match_status="matched" if best.get("uri") else "unmatched",
            candidates=candidates,
        )
    return RedirectResponse(url=playlist_redirect_url(playlist_id), status_code=303)


@router.post("/apps/music/playlists/{playlist_id}/spotify/submit")
@router.post("/apps/playlists/{playlist_id}/spotify/submit")
def submit_playlist_to_spotify(playlist_id: int):
    """Create the playlist in Spotify and add matched tracks."""
    profile = dict_from_row(db.get_playlist_profile())
    access_token = refresh_playlist_spotify_token(profile)
    playlist = dict_from_row(db.get_playlist_draft(playlist_id))
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found.")
    items = dicts_from_rows(db.get_playlist_items(playlist_id))
    unmatched = [item for item in items if not item.get("spotify_uri")]
    if unmatched:
        raise HTTPException(status_code=400, detail="Match all songs to Spotify before submitting.")
    created = spotify_create_playlist(access_token, playlist["title"], playlist.get("description", ""), bool(playlist.get("is_public")))
    spotify_id = created.get("id", "")
    snapshot_id = spotify_add_playlist_items(access_token, spotify_id, [item["spotify_uri"] for item in items]) if spotify_id and items else created.get("snapshot_id", "")
    spotify_url = ((created.get("external_urls") or {}).get("spotify") or "")
    db.update_playlist_draft(
        playlist_id,
        status="submitted",
        spotify_playlist_id=spotify_id,
        spotify_url=spotify_url,
        spotify_snapshot_id=snapshot_id,
    )
    return RedirectResponse(url=playlist_redirect_url(playlist_id, submitted=1), status_code=303)
