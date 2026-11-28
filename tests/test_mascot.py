"""The animation lab (core/mascot): clips lined up on one canvas, seams measured, jumps reported."""

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

from core.mascot import frames as fr, lab
from core.media.store import Blobs, MediaStore
from database import Database, reset_current_user_id, set_current_user_id


def dragon(n=6, size=(362, 362), scale=1.0, jitter=0, head_drop=0, fmt="GIF"):
    """A blobby stand-in for Dieter: a body and a head. jitter moves everything sideways on odd frames."""
    out = []
    for i in range(n):
        im = Image.new("RGBA", size, (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        x = (i % 2) * jitter
        drop = head_drop * i / max(1, n - 1)
        s = scale
        d.ellipse((60 * s + x, 120 * s, 300 * s + x, 330 * s), fill=(230, 110, 30, 255))
        d.ellipse((150 * s + x, (50 + drop) * s, 250 * s + x, (160 + drop) * s), fill=(240, 130, 40, 255))
        d.point((10, 10), fill=(i, 0, 0, 255 if i % 2 else 254))  # keep frames distinct so the GIF keeps them all
        out.append(im)
    buf = io.BytesIO()
    extra = {"disposal": 2} if fmt == "GIF" else {"lossless": True}
    out[0].save(buf, fmt, save_all=True, append_images=out[1:], duration=100, loop=0, **extra)
    return buf.getvalue()


class FrameTests(unittest.TestCase):
    def test_a_clip_at_another_size_lines_up_with_the_reference(self):
        big = fr.decode(dragon())
        small = fr.decode(dragon(scale=0.6, size=(640, 360), fmt="WEBP"))
        placed = fr.place(big, *fr.first_fit(fr.box(big[0][0])))
        fitted = fr.place(small, *fr.fit(fr.box(small[0][0]), fr.box(placed[0][0])))
        self.assertLess(fr.seam(placed[0][0], fitted[0][0]), fr.OK)
        self.assertEqual(placed[0][0].size, (fr.CANVAS, fr.CANVAS))

    def test_a_sideways_jump_scores_as_a_jump(self):
        clip = fr.decode(dragon(jitter=30))
        placed = fr.place(clip, *fr.first_fit(fr.box(clip[0][0])))
        self.assertEqual(fr.verdict(fr.seam(placed[0][0], placed[1][0])), "jump")
        self.assertEqual(fr.verdict(fr.seam(placed[0][0], placed[0][0])), "ok")


class LabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(str(Path(self.tmp.name) / "lab.db"))
        self.db.init()
        self.store = MediaStore(self.db, Blobs(Path(self.tmp.name) / "media"))
        self.token = set_current_user_id(1)
        lab.forget()

    def tearDown(self):
        lab.forget()
        reset_current_user_id(self.token)
        if self.db.conn:
            self.db.conn.close()
        self.tmp.cleanup()

    def add(self, data, title, keep=True):
        item = self.store.add(data, title, "chatgpt")
        if keep:
            self.store.keep(item["id"])
        return item["id"]

    def test_clips_are_checked_and_bad_joins_listed_first(self):
        spec = {"reference": "breathing", "clips": [
            {"name": "breathing", "title": "Breathing", "media_id": self.add(dragon(), "b"), "play": "loop",
             "starts": "alert", "ends": "alert", "role": "idle"},
            {"name": "jittery", "title": "Jittery", "media_id": self.add(dragon(jitter=30, scale=0.7), "j"),
             "play": "loop", "starts": "alert", "ends": "alert", "role": "idle"},
            {"name": "nod_off", "title": "Nod off", "media_id": self.add(dragon(head_drop=120), "n"),
             "play": "once", "starts": "alert", "ends": "asleep", "role": "sleep()"},
            {"name": "waiting", "title": "Waiting", "media_id": self.add(dragon(n=5), "w", keep=False),
             "play": "once", "starts": "alert", "ends": "alert", "role": ""},
        ]}
        with mock.patch.object(lab, "manifest", return_value=spec):
            built = lab.build(self.store)
        names = [c["name"] for c in built["clips"]]
        self.assertEqual(names, ["breathing", "jittery", "nod_off"])
        self.assertEqual(built["missing"][0]["name"], "waiting")
        jittery = next(c for c in built["clips"] if c["name"] == "jittery")
        self.assertTrue(any("paws move" in p for p in jittery["problems"]))
        self.assertEqual(next(c for c in built["clips"] if c["name"] == "breathing")["problems"], [])
        self.assertEqual(built["joins"][0]["verdict"], "jump")  # worst first
        self.assertTrue(all(j["pose"] == "alert" for j in built["joins"]))  # nothing starts asleep, so no join there
        self.assertEqual(built["homes"], {"alert": "breathing"})
        # Each clip is checked against the home loop only (not every pairing): jittery and nod_off, to and from.
        self.assertEqual(sorted((j["from"], j["to"]) for j in built["joins"]),
                         [("breathing", "jittery"), ("breathing", "nod_off"), ("jittery", "breathing")])
        self.assertTrue(built["sheets"]["breathing"][0].startswith(b"RIFF"))  # WebP sprite sheet pages

        from core import ui
        page = ui.templates(Path(lab.__file__).parent / "templates").get_template("mascot_lab.html").render(
            app={"id": "media", "title": "Media", "nav_label": "Media", "accent": "today"}, page_url="/media/lab",
            lab={k: v for k, v in built.items() if k != "sheets"}, problems=2, done=None)
        self.assertIn("Jumps", page)
        self.assertIn("Waiting: not kept yet", page.replace("waiting: not", "Waiting: not"))

    def test_a_clip_that_runs_off_the_canvas_is_a_problem(self):
        # A long, low pose sized to the same area as an upright one comes out wider than the canvas.
        def flat():
            im = Image.new("RGBA", (900, 200), (0, 0, 0, 0))
            ImageDraw.Draw(im).rectangle((0, 120, 899, 199), fill=(230, 110, 30, 255))
            buf = io.BytesIO()
            im.save(buf, "WEBP", lossless=True)
            return buf.getvalue()
        spec = {"reference": "breathing", "clips": [
            {"name": "breathing", "title": "Breathing", "media_id": self.add(dragon(), "b"), "play": "loop",
             "starts": "alert", "ends": "alert", "role": "idle"},
            {"name": "flat", "title": "Flat", "media_id": self.add(flat(), "f"), "play": "once",
             "starts": "alert", "ends": "alert", "role": ""},
        ]}
        with mock.patch.object(lab, "manifest", return_value=spec):
            built = lab.build(self.store)
        flat_clip = next(c for c in built["clips"] if c["name"] == "flat")
        self.assertTrue(any("runs off the canvas" in p for p in flat_clip["problems"]), flat_clip["problems"])
        self.assertEqual(next(c for c in built["clips"] if c["name"] == "breathing")["problems"], [])

    def test_the_real_manifest_names_only_kept_style_ids(self):
        spec = lab.manifest()
        self.assertIn(spec["reference"], [c["name"] for c in spec["clips"]])
        poses = {c["starts"] for c in spec["clips"]} | {c["ends"] for c in spec["clips"]}
        self.assertLessEqual(poses, {"alert", "asleep", "thinking", "listening", "speaking"})
        for c in spec["clips"]:
            self.assertTrue(c["media_id"].startswith("med-"), c["name"])
            self.assertIn(c["play"], ("once", "loop", "pingpong"))


if __name__ == "__main__":
    unittest.main()
