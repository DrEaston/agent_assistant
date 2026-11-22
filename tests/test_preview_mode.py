"""Preview copies of Dieter: read a copy of the live data, never write it back, and say so on every page."""

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cloud_persistence import CloudStoragePersistence


class ReadOnlyPersistenceTests(unittest.TestCase):
    def test_read_only_never_touches_the_bucket_when_saving(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "projects.db"
            db.write_bytes(b"x")
            (Path(tmp) / "uploads").mkdir()
            (Path(tmp) / "uploads" / "a.jpg").write_bytes(b"x")
            store = CloudStoragePersistence("live-bucket", db, Path(tmp) / "uploads", read_only=True)
            with patch.object(store, "_get_bucket", side_effect=AssertionError("wrote to the bucket")):
                self.assertFalse(store.backup_database())
                self.assertFalse(store.schedule_backup())
                self.assertFalse(store.sync_upload_file(Path(tmp) / "uploads" / "a.jpg"))
                self.assertEqual(store.sync_uploads(), 0)
                self.assertFalse(store.seed_if_empty())

    def test_read_only_comes_from_the_environment(self):
        with patch.dict(os.environ, {"GCS_BUCKET": "b", "GCS_READ_ONLY": "1"}):
            self.assertTrue(CloudStoragePersistence.from_env("x.db", "up").read_only)
        with patch.dict(os.environ, {"GCS_BUCKET": "b", "GCS_READ_ONLY": ""}):
            self.assertFalse(CloudStoragePersistence.from_env("x.db", "up").read_only)


class PreviewBannerTests(unittest.TestCase):
    def render(self):
        from core import ui
        importlib.reload(ui)
        env = ui.templates()
        return env.from_string('{% extends "layout.html" %}{% block content %}hi{% endblock %}').render(app=None)

    def test_banner_only_on_previews(self):
        with patch.dict(os.environ, {"PREVIEW_LABEL": "PR #12"}):
            self.assertIn("Preview of PR #12", self.render())
        with patch.dict(os.environ, {"PREVIEW_LABEL": ""}):
            self.assertNotIn("Preview of", self.render())


if __name__ == "__main__":
    unittest.main()
