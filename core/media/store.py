"""The Media store: pictures handed to Dieter, waiting for Curtis's OK until he keeps or discards them.

A picture is "waiting" (an assistant sent it), "kept" (he said yes; assistants and apps can use it) or "discarded"
(the bytes are deleted; the row stays as a record). Waiting pictures he never decides on are discarded after
WAIT_DAYS. Only his signed-in pages call keep() and discard(); the connector has no tool for either.

Bytes never go in SQLite (the whole database is downloaded at every start). They're files in a private folder, copied
to Cloud Storage under their own prefix and fetched back on demand, so a start doesn't download every picture. The
folder is not the public /uploads one: pictures are only served through /media/<id>/file to their owner.
Everything is scoped to the signed-in user (get_current_user_id).
"""

import hashlib
import io
import secrets
import time
from pathlib import Path

from database import get_current_user_id

MAX_BYTES = 25 * 1024 * 1024
MAX_VIDEO_BYTES = 30 * 1024 * 1024  # Cloud Run takes uploads up to 32 MB; an 8-second Firefly clip is ~5-15 MB
MAX_PIXELS = 40_000_000
WAIT_DAYS = 7
FORMATS = {"PNG": ("png", "image/png"), "WEBP": ("webp", "image/webp"), "JPEG": ("jpg", "image/jpeg"),
           "GIF": ("gif", "image/gif"), "MP4": ("mp4", "video/mp4"), "MOV": ("mov", "video/quicktime")}
VIDEOS = ("MP4", "MOV")
WHO = {"you": "you", "chatgpt": "ChatGPT", "claude": "Claude"}
STATUSES = ("waiting", "kept", "discarded")

SCHEMA = """
CREATE TABLE IF NOT EXISTS media_items (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'waiting',
    title TEXT DEFAULT '',
    note TEXT DEFAULT '',
    prompt TEXT DEFAULT '',
    source TEXT DEFAULT '',
    made_by TEXT DEFAULT '',
    file_name TEXT DEFAULT '',
    format TEXT DEFAULT '',
    width INTEGER DEFAULT 0,
    height INTEGER DEFAULT 0,
    has_alpha INTEGER DEFAULT 0,
    bytes INTEGER DEFAULT 0,
    sha256 TEXT DEFAULT '',
    created_at REAL,
    decided_at REAL
);
CREATE INDEX IF NOT EXISTS media_items_user ON media_items (user_id, status);
"""


class Invalid(Exception):
    """Something he or an assistant can fix; the message says what."""


def who(made_by):
    return WHO.get(made_by, made_by or "someone")


def _video_size(data):
    """(width, height) from the first video track header (tkhd) of an MP4/MOV, or (0, 0)."""
    at = data.find(b"tkhd")
    while at != -1:
        size = int.from_bytes(data[at - 4:at], "big")
        end = at - 4 + size
        if 84 <= size <= 200 and end <= len(data):
            width = int.from_bytes(data[end - 8:end - 4], "big") >> 16
            height = int.from_bytes(data[end - 4:end], "big") >> 16
            if width and height:
                return width, height
        at = data.find(b"tkhd", at + 4)
    return 0, 0


def inspect_video(data):
    """An MP4 or MOV video (Firefly's clips), or Invalid. Checked by its header: Dieter only stores and plays it."""
    if len(data) > MAX_VIDEO_BYTES:
        raise Invalid("That video is over 30 MB. Firefly's 8-second clips are well under that; try a shorter one.")
    if data[4:8] != b"ftyp" or b"moov" not in data:
        raise Invalid("That video isn't an MP4 Dieter can read.")
    fmt = "MOV" if data[8:12] == b"qt  " else "MP4"
    width, height = _video_size(data)
    ext, mime = FORMATS[fmt]
    return {"format": fmt, "ext": ext, "mime": mime, "width": width, "height": height, "has_alpha": False}


def inspect(data):
    """What the picture (or video) is, or Invalid. Only PNG, WebP, JPEG and GIF pictures, the pixels checked, not
    just the header; and MP4/MOV videos."""
    from PIL import Image

    if not data:
        raise Invalid("No image came with it.")
    if data[4:8] == b"ftyp":
        return inspect_video(data)
    if len(data) > MAX_BYTES:
        raise Invalid("That image is over 25 MB.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            fmt, width, height = image.format, image.width, image.height
            alpha = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
            if fmt not in FORMATS:
                raise Invalid(f"Dieter keeps PNG, WebP, JPEG or GIF pictures, not {fmt or 'this kind of file'}.")
            if width * height > MAX_PIXELS:
                raise Invalid("That image is too big (over 40 megapixels).")
            image.verify()
    except Invalid:
        raise
    except Exception as exc:
        raise Invalid(f"Those {len(data)} bytes aren't a picture Dieter can read ({exc.__class__.__name__}).")
    ext, mime = FORMATS[fmt]
    return {"format": fmt, "ext": ext, "mime": mime, "width": width, "height": height, "has_alpha": bool(alpha)}


class Blobs:
    """Picture files: a local folder, plus an optional remote copy (Cloud Storage) with put/get/delete(key[, path])."""

    def __init__(self, folder, remote=None):
        self.folder, self.remote = Path(folder), remote

    def _path(self, key):
        return self.folder / key

    def put(self, key, data):
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if self.remote:
            self.remote.put(key, path)

    def get(self, key):
        path = self._path(key)
        if not path.exists() and self.remote:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.remote.get(key, path)
        return path.read_bytes() if path.exists() else None

    def delete(self, key):
        self._path(key).unlink(missing_ok=True)
        if self.remote:
            self.remote.delete(key)


class MediaStore:
    def __init__(self, db, blobs):
        self.db, self.blobs, self._ready = db, blobs, False

    def _conn(self):
        conn = self.db.connect()
        if not self._ready:
            conn.executescript(SCHEMA)
            conn.commit()
            self._ready = True
        return conn

    def _user(self):
        user = get_current_user_id()
        if not user:
            raise Invalid("Sign in to Dieter first.")
        return user

    @staticmethod
    def _key(row):
        return f"{row['user_id']}/{row['id']}.{FORMATS.get(row['format'], ('bin',))[0]}"

    def _out(self, row):
        item = dict(row)
        item["has_alpha"] = bool(item["has_alpha"])
        item["who"] = who(item["made_by"])
        item["kind"] = "video" if item["format"] in VIDEOS else "picture"
        item["page"] = f"/media/{item['id']}"
        item["file"] = f"/media/{item['id']}/file" if item["status"] != "discarded" else ""
        return item

    # ---------- adding ----------

    def add(self, data, title, made_by, note="", prompt="", source="", file_name=""):
        """A new waiting picture (or the one already here with the same bytes, marked duplicate)."""
        user = self._user()
        title = " ".join(str(title or "").split())[:120]
        if not title:
            raise Invalid("The picture needs a short title, e.g. \"Dieter, eyes half closed\".")
        if made_by not in WHO:
            raise Invalid("made_by is you, chatgpt or claude.")
        info = inspect(data)
        digest = hashlib.sha256(data).hexdigest()
        conn = self._conn()
        same = conn.execute("SELECT * FROM media_items WHERE user_id = ? AND sha256 = ? AND status != 'discarded'",
                            [user, digest]).fetchone()
        if same:
            return dict(self._out(same), duplicate=True)
        row = {"id": f"med-{secrets.token_hex(6)}", "user_id": user, "status": "kept" if made_by == "you" else "waiting",
               "title": title, "note": str(note or "").strip()[:2000], "prompt": str(prompt or "").strip()[:4000],
               "source": " ".join(str(source or "").split())[:300], "made_by": made_by,
               "file_name": " ".join(str(file_name or "").split())[:200], "format": info["format"],
               "width": info["width"], "height": info["height"], "has_alpha": int(info["has_alpha"]),
               "bytes": len(data), "sha256": digest, "created_at": time.time(), "decided_at": None}
        self.blobs.put(self._key(row), data)
        conn.execute(f"INSERT INTO media_items ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
        self.db._commit()
        return self._out(row)

    # ---------- reading ----------

    def items(self, status="kept", limit=200):
        if status not in STATUSES + ("all",):
            raise Invalid("show is kept, waiting or all.")
        self.purge()
        sql, args = "SELECT * FROM media_items WHERE user_id = ?", [self._user()]
        if status == "all":
            sql += " AND status != 'discarded'"
        else:
            sql, args = sql + " AND status = ?", args + [status]
        rows = self._conn().execute(sql + " ORDER BY created_at DESC LIMIT ?", args + [limit]).fetchall()
        return [self._out(r) for r in rows]

    def _row(self, item_id):
        row = self._conn().execute("SELECT * FROM media_items WHERE id = ? AND user_id = ?",
                                   [str(item_id or ""), self._user()]).fetchone()
        if not row:
            raise Invalid(f"There's no picture {item_id}. media_list shows them.")
        return row

    def item(self, item_id):
        return self._out(self._row(item_id))

    def read(self, item_id):
        """(bytes, mime) for a waiting or kept picture."""
        row = self._row(item_id)
        if row["status"] == "discarded":
            raise Invalid("That picture was discarded.")
        data = self.blobs.get(self._key(row))
        if data is None:
            raise Invalid("Dieter has the record of that picture but can't find the file.")
        return data, FORMATS.get(row["format"], ("", "application/octet-stream"))[1]

    def made_from(self, item_id):
        """Pictures made from this one (their source names it): what came out of a Firefly video."""
        rows = self._conn().execute("SELECT * FROM media_items WHERE user_id = ? AND status != 'discarded' AND id != ? "
                                    "AND source LIKE ? ORDER BY created_at",
                                    [self._user(), item_id, f"%{item_id}%"]).fetchall()
        return [self._out(r) for r in rows]

    def videos(self):
        """Kept videos, each with whether anything has been made from it yet."""
        return [dict(v, taken_in=bool(self.made_from(v["id"]))) for v in self.items("kept") if v["kind"] == "video"]

    def today(self):
        """For the Today page: pictures waiting for his OK (None when there are none)."""
        waiting = self.items("waiting", limit=50)
        return {"waiting": len(waiting), "first": waiting[0]} if waiting else None

    # ---------- deciding (his pages only) ----------

    def keep(self, item_id):
        row = self._row(item_id)
        if row["status"] != "waiting":
            raise Invalid(f"That picture is already {row['status']}.")
        self._set(row, "kept")

    def discard(self, item_id):
        row = self._row(item_id)
        if row["status"] == "discarded":
            raise Invalid("That picture was already discarded.")
        self._drop(row)

    def _set(self, row, status):
        self._conn().execute("UPDATE media_items SET status = ?, decided_at = ? WHERE id = ?",
                             [status, time.time(), row["id"]])
        self.db._commit()

    def _drop(self, row):
        try:
            self.blobs.delete(self._key(row))
        except Exception as exc:  # the record still says discarded; a stray file is harmless
            print(f"Warning: couldn't delete media file {row['id']}: {exc!r}")
        self._set(row, "discarded")

    def purge(self):
        """Discard this user's waiting pictures older than WAIT_DAYS."""
        cutoff = time.time() - WAIT_DAYS * 86400
        rows = self._conn().execute("SELECT * FROM media_items WHERE user_id = ? AND status = 'waiting' "
                                    "AND created_at < ?", [self._user(), cutoff]).fetchall()
        for row in rows:
            self._drop(row)
        return len(rows)
