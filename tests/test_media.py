"""Media (core/media): pictures sent to Dieter wait for Curtis's OK; only he keeps or discards them."""

import io
import json
import secrets
import tempfile
import time
import unittest
import uuid
import warnings
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from core.connector import oauth
from core.connector.tools import ToolError
from core.media import connector, fetch
from core.media.store import Blobs, Invalid, MediaStore, inspect
from database import Database, reset_current_user_id, set_current_user_id


def picture(alpha=True, size=(64, 48), fmt="PNG", color=(255, 128, 0, 0)):
    out = io.BytesIO()
    Image.new("RGBA" if alpha else "RGB", size, color if alpha else color[:3]).save(out, fmt)
    return out.getvalue()


class FakeCloud:
    """Stands in for Cloud Storage: remembers what was put, can hand it back, records deletes."""

    def __init__(self):
        self.files, self.deleted = {}, []

    def put(self, key, path):
        self.files[key] = Path(path).read_bytes()

    def get(self, key, path):
        if key in self.files:
            Path(path).write_bytes(self.files[key])

    def delete(self, key):
        self.deleted.append(key)
        self.files.pop(key, None)


class MediaStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(str(Path(self.tmp.name) / "media.db"))
        self.db.init()
        self.cloud = FakeCloud()
        self.folder = Path(self.tmp.name) / "media"
        self.store = MediaStore(self.db, Blobs(self.folder, self.cloud))
        self.token = set_current_user_id(1)

    def tearDown(self):
        reset_current_user_id(self.token)
        if self.db.conn:
            self.db.conn.close()
        self.tmp.cleanup()

    def test_a_picture_waits_then_is_kept_with_its_transparency(self):
        data = picture()
        item = self.store.add(data, "Dieter, eyes half closed", "chatgpt", source="sitting pose", prompt="close eyes")
        self.assertEqual((item["status"], item["width"], item["height"], item["has_alpha"]), ("waiting", 64, 48, True))
        self.assertEqual(self.store.items("kept"), [])
        self.assertEqual(self.store.today()["waiting"], 1)
        self.store.keep(item["id"])
        self.assertEqual([i["id"] for i in self.store.items("kept")], [item["id"]])
        self.assertIsNone(self.store.today())
        stored, mime = self.store.read(item["id"])
        self.assertEqual((stored, mime), (data, "image/png"))  # byte for byte, alpha and all
        with Image.open(io.BytesIO(stored)) as image:
            self.assertEqual(image.mode, "RGBA")
        with self.assertRaises(Invalid):
            self.store.keep(item["id"])

    def test_after_a_restart_the_file_comes_back_from_cloud_storage(self):
        item = self.store.add(picture(), "Dragon", "chatgpt")
        for f in self.folder.rglob("*"):
            if f.is_file():
                f.unlink()  # a new Cloud Run instance starts with an empty folder
        fresh = MediaStore(self.db, Blobs(self.folder, self.cloud))
        self.assertEqual(fresh.read(item["id"])[0], picture())

    def test_discard_deletes_the_file_everywhere(self):
        item = self.store.add(picture(), "Dragon", "chatgpt")
        self.store.discard(item["id"])
        self.assertEqual(len(self.cloud.deleted), 1)
        self.assertFalse(any(f.is_file() for f in self.folder.rglob("*")))
        self.assertEqual(self.store.item(item["id"])["status"], "discarded")
        with self.assertRaises(Invalid):
            self.store.read(item["id"])

    def test_unkept_pictures_go_after_a_week(self):
        old = self.store.add(picture(color=(1, 2, 3, 4)), "Old", "chatgpt")
        new = self.store.add(picture(color=(5, 6, 7, 8)), "New", "chatgpt")
        conn = self.db.connect()
        conn.execute("UPDATE media_items SET created_at = ? WHERE id = ?", [time.time() - 8 * 86400, old["id"]])
        conn.commit()
        self.assertEqual([i["id"] for i in self.store.items("waiting")], [new["id"]])
        self.assertEqual(self.store.item(old["id"])["status"], "discarded")

    def test_the_same_picture_twice_is_saved_once(self):
        first = self.store.add(picture(), "Dragon", "chatgpt")
        again = self.store.add(picture(), "Dragon again", "chatgpt")
        self.assertEqual(again["id"], first["id"])
        self.assertTrue(again["duplicate"])

    def test_uploads_by_him_are_kept_straight_away(self):
        self.assertEqual(self.store.add(picture(), "Mine", "you")["status"], "kept")

    def test_only_real_pictures_of_known_kinds(self):
        for bad in (b"", b"hello", picture()[:40], b"<svg xmlns='http://www.w3.org/2000/svg'/>"):
            with self.assertRaises(Invalid):
                inspect(bad)
        self.assertEqual(inspect(picture(alpha=False, fmt="JPEG"))["format"], "JPEG")
        self.assertFalse(inspect(picture(alpha=False))["has_alpha"])
        with self.assertRaises(Invalid):
            self.store.add(picture(), "", "chatgpt")
        with self.assertRaises(Invalid):
            self.store.add(picture(), "x", "someone")

    def test_other_people_dont_see_my_pictures(self):
        item = self.store.add(picture(), "Dragon", "chatgpt")
        other = set_current_user_id(2)
        try:
            self.assertEqual(self.store.items("all"), [])
            with self.assertRaises(Invalid):
                self.store.read(item["id"])
            with self.assertRaises(Invalid):
                self.store.keep(item["id"])
        finally:
            reset_current_user_id(other)


class SaveToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(str(Path(self.tmp.name) / "media.db"))
        self.db.init()
        self.saved = dict(connector._state)
        connector.configure(self.db, Blobs(Path(self.tmp.name) / "media"))
        self.token = set_current_user_id(1)

    def tearDown(self):
        reset_current_user_id(self.token)
        connector._state.update(self.saved)
        if self.db.conn:
            self.db.conn.close()
        self.tmp.cleanup()

    def test_chatgpt_saves_a_file_and_never_the_link(self):
        sent = {"download_url": "https://files.oaiusercontent.com/abc?sig=secret", "file_id": "file_1",
                "mime_type": "image/png", "file_name": "dragon.png"}
        out = connector.save_image({"image": sent, "title": "Dragon", "assistant": "chatgpt",
                                    "source": "Logbook: Ragnar legs, v1"}, download=lambda url: (picture(), None))
        self.assertEqual((out["status"], out["size"], out["transparent"]), ("waiting", "64x48 PNG", True))
        self.assertIn("waiting for Curtis's OK", out["message"])
        row = dict(self.db.connect().execute("SELECT * FROM media_items").fetchone())
        self.assertNotIn("secret", json.dumps(row))
        self.assertEqual(row["file_name"], "dragon.png")
        self.assertEqual(connector.list_items({})["pictures"], [])  # not his until he keeps it
        self.assertEqual(len(connector.list_items({"show": "waiting"})["pictures"]), 1)

    def test_clear_errors(self):
        for args, bit in (({"image": "file_abc", "title": "x", "assistant": "chatgpt"}, "as text"),
                          ({"image": {"file_id": "f"}, "title": "x", "assistant": "chatgpt"}, "No image file"),
                          ({"image": {"download_url": "https://x"}, "title": "x", "assistant": "bard"}, "assistant")):
            with self.assertRaises(ToolError) as caught:
                connector.save_image(args, download=lambda url: (picture(), None))
            self.assertIn(bit, str(caught.exception))
        with self.assertRaises(ToolError) as caught:
            connector.save_image({"image": {"download_url": "https://x"}, "title": "x", "assistant": "chatgpt"},
                                 download=lambda url: (None, "the download answered 403"))
        self.assertIn("403", str(caught.exception))

    def test_save_is_a_file_tool_that_asks_first_and_list_is_read_only(self):
        tools = connector.tools().tools
        schema = tools["media_save_image"].schema()
        self.assertEqual(schema["_meta"], {"openai/fileParams": ["image"]})
        self.assertFalse(schema["annotations"]["readOnlyHint"])  # ChatGPT asks him before it runs
        self.assertTrue(tools["media_list"].schema()["annotations"]["readOnlyHint"])
        self.assertFalse([n for n in tools if "keep" in n or "discard" in n or "approve" in n])


class DownloadTests(unittest.TestCase):
    def test_only_public_https_links(self):
        for url in ("http://example.com/a.png", "https://127.0.0.1/a.png", "https://169.254.169.254/x", "ftp://x"):
            data, problem = fetch.download(url)
            self.assertIsNone(data, url)
            self.assertTrue(problem)


class MediaPageTests(unittest.TestCase):
    """The pages through the real app."""

    @classmethod
    def setUpClass(cls):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import api
        cls.api, cls.db = api, api.db
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.saved = dict(connector._state)
        connector.configure(cls.db, Blobs(Path(cls.tmp.name) / "media"))
        cls.user_id = cls.db.create_user(f"media-{uuid.uuid4().hex[:8]}@example.com", "Media Keeper", "x", "member")
        conn = cls.db.connect()
        conn.execute("UPDATE users SET status = 'active' WHERE id = ?", (cls.user_id,))
        conn.commit()
        cls.token = oauth.OAuth(cls.db, lambda uid: dict(cls.db.get_user_by_id(uid) or {}) or None) \
            .issue("chatgpt-test", cls.user_id, "dieter")["access_token"]

    @classmethod
    def tearDownClass(cls):
        connector._state.update(cls.saved)
        cls.tmp.cleanup()

    def setUp(self):
        self.client = TestClient(self.api.app)
        session = secrets.token_urlsafe(16)
        self.db.create_session(self.user_id, self.api.hash_session_token(session),
                               (datetime.utcnow() + timedelta(days=1)).isoformat())
        self.client.cookies.set(self.api.SESSION_COOKIE_NAME, session)

    def send_from_chatgpt(self, title, data):
        token = set_current_user_id(self.user_id)
        try:
            return connector.save_image({"image": {"download_url": "https://x.example/f", "file_id": "f1"},
                                         "title": title, "assistant": "chatgpt"}, download=lambda url: (data, None))
        finally:
            reset_current_user_id(token)

    def test_he_keeps_or_discards_on_the_page(self):
        sent = self.send_from_chatgpt("Dragon, eyes half closed", picture(color=(9, 9, 9, 0)))
        home = self.client.get("/media").text
        self.assertIn("Waiting for your OK", home)
        self.assertIn("Dragon, eyes half closed", home)
        self.assertIn("Waiting for your OK", self.client.get("/").text)  # on Today
        page = self.client.get(f"/media/{sent['id']}").text
        self.assertIn("Keep", page)
        file = self.client.get(f"/media/{sent['id']}/file")
        self.assertEqual((file.status_code, file.headers["content-type"]), (200, "image/png"))
        self.assertEqual(self.client.post(f"/media/{sent['id']}/keep", follow_redirects=False).status_code, 303)
        self.assertIn("Kept", self.client.get(f"/media/{sent['id']}").text)

        other = self.send_from_chatgpt("Not this one", picture(color=(1, 1, 1, 0)))
        self.client.post(f"/media/{other['id']}/discard")
        self.assertEqual(self.client.get(f"/media/{other['id']}/file").status_code, 404)

    def test_signed_out_and_other_people_get_nothing(self):
        sent = self.send_from_chatgpt("Private", picture(color=(2, 2, 2, 0)))
        anonymous = TestClient(self.api.app)
        self.assertNotEqual(anonymous.get(f"/media/{sent['id']}/file", follow_redirects=False).status_code, 200)
        self.assertEqual(self.client.get("/media/med-000000000000/file").status_code, 404)

    def test_upload_on_the_page(self):
        r = self.client.post("/media", files={"file": ("x.png", picture(), "image/png")})
        self.assertEqual(r.status_code, 405)
        r = self.client.post("/media/upload", data={"title": "My sketch"},
                             files={"file": ("sketch.png", picture(color=(3, 3, 3, 0)), "image/png")},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("My sketch", self.client.get(r.headers["location"]).text)
        bad = self.client.post("/media/upload", files={"file": ("x.txt", b"hello", "text/plain")},
                               follow_redirects=False)
        self.assertIn("error=", bad.headers["location"])

    def test_chatgpt_sees_it_through_the_connector(self):
        sent = self.send_from_chatgpt("Through MCP", picture(color=(4, 4, 4, 0)))
        answer = TestClient(self.api.app).post(
            "/mcp", headers={"Authorization": f"Bearer {self.token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "media_get", "arguments": {"media_id": sent["id"]}}}).json()["result"]
        self.assertEqual(json.loads(answer["content"][0]["text"])["status"], "waiting")


if __name__ == "__main__":
    unittest.main()
