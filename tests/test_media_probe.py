"""The temporary ChatGPT image test (core/media/probe.py)."""

import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from core.media import probe
from database import Database, reset_current_user_id, set_current_user_id


def png(alpha=True):
    out = io.BytesIO()
    Image.new("RGBA" if alpha else "RGB", (64, 48)).save(out, "PNG")
    return out.getvalue()


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(str(Path(tempfile.mkdtemp()) / "probe.db"))
        probe.configure(self.db)
        self.token = set_current_user_id(7)

    def tearDown(self):
        reset_current_user_id(self.token)
        probe.configure(None)
        if self.db.conn:
            self.db.conn.close()

    def test_a_file_from_chatgpt_is_read_and_logged_without_the_link(self):
        sent = {"download_url": "https://files.oaiusercontent.com/abc?sig=secret", "file_id": "file_1",
                "mime_type": "image/png", "file_name": "ragnar.png"}
        report = probe.test_image({"image": sent, "device": "phone"}, fetch=lambda url: png())
        self.assertTrue(report["worked"])
        self.assertEqual((report["image"]["width"], report["image"]["height"]), (64, 48))
        self.assertTrue(report["image"]["transparent_background"])
        self.assertEqual(report["download_host"], "files.oaiusercontent.com")
        logged = probe.results({})["results"]
        self.assertEqual(len(logged), 1)
        self.assertNotIn("secret", str(logged))
        self.assertEqual(logged[0]["sent_by"], "phone")

    def test_a_bare_string_is_reported(self):
        report = probe.test_image({"image": "file_abc123"})
        self.assertFalse(report["worked"])
        self.assertEqual(report["looks_like"], "a file id")
        self.assertIn("text instead of the file", report["message"])

    def test_not_an_image(self):
        report = probe.test_image({"image": {"download_url": "https://x.example/f"}}, fetch=lambda url: b"hello")
        self.assertFalse(report["worked"])

    def test_only_public_https_links_are_downloaded(self):
        for url in ("http://example.com/a.png", "https://127.0.0.1/a.png", "https://169.254.169.254/x"):
            data, problem = probe.download(url)
            self.assertIsNone(data, url)
            self.assertTrue(problem)

    def test_the_tool_tells_chatgpt_image_is_a_file(self):
        schema = probe.tools().tools["media_test_image"].schema()
        self.assertEqual(schema["_meta"], {"openai/fileParams": ["image"]})
        self.assertNotIn("_meta", probe.tools().tools["media_test_results"].schema())

    def test_other_people_dont_see_my_results(self):
        probe.test_image({"image": "x"})
        other = set_current_user_id(8)
        try:
            self.assertEqual(probe.results({})["results"], [])
        finally:
            reset_current_user_id(other)


if __name__ == "__main__":
    unittest.main()
