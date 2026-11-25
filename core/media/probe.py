"""Temporary: can ChatGPT hand Dieter an image through the connector, on the phone and on the desktop?

media_test_image marks its `image` parameter as a file (`_meta["openai/fileParams"]`). ChatGPT should then send
{download_url, file_id, mime_type, file_name}; some reports say the phone app sends a bare string instead. The tool
reports exactly what arrived, downloads the image if it can, and says what it is. It keeps nothing but a short log
line (what arrived, the download host, the image's type and size), never the image or the download link, so the
results can be read back with media_test_results. Remove once Media is built.
"""

import io
import ipaddress
import json
import socket
import time
from urllib.parse import urlsplit

import httpx

from core.connector.tools import Tool, ToolSet
from database import get_current_user_id

MAX_BYTES = 25 * 1024 * 1024
TIMEOUT = 20
SCHEMA = """CREATE TABLE IF NOT EXISTS media_probe_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    created_at REAL,
    report_json TEXT
)"""

_db = None


def configure(db):
    global _db
    _db = db


def _public_https(url):
    """(ok, why): only https to a public address, so a crafted URL can't reach Dieter's own network."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return False, "not an https link"
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parts.hostname, parts.port or 443)}
    except OSError as exc:
        return False, f"couldn't look up the host ({exc.__class__.__name__})"
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global:
            return False, "the link points at a private address"
    return True, ""


def download(url, fetch=None):
    """(bytes, problem). fetch(url) -> bytes is for tests."""
    if fetch:
        return fetch(url), None
    ok, why = _public_https(url)
    if not ok:
        return None, why
    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            with client.stream("GET", url) as response:
                if response.status_code != 200:
                    return None, f"the download answered {response.status_code}"
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        return None, "the image is over 25 MB"
                return bytes(data), None
    except httpx.HTTPError as exc:
        return None, f"the download failed ({exc.__class__.__name__})"


def describe(data):
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
            return {"format": image.format, "width": image.width, "height": image.height, "mode": image.mode,
                    "transparent_background": bool(alpha), "bytes": len(data)}
    except Exception as exc:
        return {"error": f"Dieter got {len(data)} bytes but they aren't an image it can read ({exc.__class__.__name__})."}


def test_image(args, fetch=None):
    """Never fails without a trace: anything unexpected is logged and returned as a structured error."""
    try:
        return _test_image(args, fetch)
    except Exception as exc:  # report what broke, not a generic "Dieter hit an error"
        report = {"sent_by": str(args.get("device") or "")[:40] or "not said",
                  "received": type(args.get("image")).__name__, "worked": False,
                  "image": {"error": f"Dieter hit an internal error ({exc.__class__.__name__})."}}
        print(f"Warning: media_test_image failed: {exc!r}")
        try:
            _log(report)
        except Exception as log_exc:
            print(f"Warning: media_test_image couldn't log: {log_exc!r}")
        report["message"] = "It didn't work this time: " + report["image"]["error"] + " Tell Curtis exactly that."
        return report


def _test_image(args, fetch=None):
    raw = args.get("image")
    report = {"sent_by": str(args.get("device") or "").strip()[:40] or "not said", "received": type(raw).__name__}
    if isinstance(raw, dict):
        report["fields"] = sorted(raw)
        report["file_name"] = str(raw.get("file_name") or "")[:120]
        report["mime_type"] = str(raw.get("mime_type") or "")[:60]
        report["has_file_id"] = bool(raw.get("file_id"))
        url = str(raw.get("download_url") or "")
        report["download_host"] = urlsplit(url).hostname or "" if url else ""
        if url:
            data, problem = download(url, fetch)
            report["image"] = describe(data) if data is not None else {"error": problem}
        else:
            report["image"] = {"error": "no download_url came with it"}
    elif isinstance(raw, str):
        report["looks_like"] = ("a file id" if raw.startswith("file") else "a path" if raw.startswith("/")
                                else "a link" if raw.startswith("http") else "text")
        report["image"] = {"error": "ChatGPT sent text instead of the file, so Dieter can't download it."}
    else:
        report["image"] = {"error": "no image came with the call"}
    worked = "error" not in report["image"]
    report["worked"] = worked
    _log(report)
    report["message"] = (f"It worked: Dieter got a {report['image']['width']}x{report['image']['height']} "
                         f"{report['image']['format']}." if worked else
                         "It didn't work this time: " + report["image"]["error"] + " Tell Curtis exactly that.")
    return report


def _log(report):
    if _db is None:
        return
    conn = _db.connect()
    conn.execute(SCHEMA)
    conn.execute("INSERT INTO media_probe_log (user_id, created_at, report_json) VALUES (?, ?, ?)",
                 [get_current_user_id() or 0, time.time(), json.dumps(report)])
    _db._commit()


def results(args):
    if _db is None:
        return {"results": []}
    conn = _db.connect()
    conn.execute(SCHEMA)
    rows = conn.execute("SELECT created_at, report_json FROM media_probe_log WHERE user_id = ? "
                        "ORDER BY id DESC LIMIT 20", [get_current_user_id() or 0]).fetchall()
    return {"results": [dict(json.loads(r["report_json"]), when=time.strftime("%Y-%m-%d %H:%M UTC",
                                                                               time.gmtime(r["created_at"])))
                        for r in rows]}


def tools():
    return ToolSet("media", [
        Tool("media_test_image", "Send Dieter an image (test)",
             "A test: hands an image from this chat to Dieter and reports whether it arrived (type, size, "
             "transparency). Nothing is saved except a note of the result. Use it when Curtis asks to send an "
             "image to Dieter. Attach the image itself as `image`. device: say where this chat is running if you "
             "know (\"phone\", \"desktop\", \"web\").",
             test_image,
             {"image": {"type": "object", "description": "The image file to send.",
                        "properties": {"download_url": {"type": "string"}, "file_id": {"type": "string"},
                                       "mime_type": {"type": "string"}, "file_name": {"type": "string"}},
                        "required": ["download_url", "file_id"], "additionalProperties": False},
              "device": {"type": "string"}},
             ("image",), read_only=True,  # ChatGPT Plus/Pro only offers read-only tools; this saves just a result note
             meta={"openai/fileParams": ["image"]}),
        Tool("media_test_results", "Image test results",
             "The last 20 results of media_test_image: what ChatGPT sent and whether Dieter could read the image.",
             results, read_only=True),
    ])
