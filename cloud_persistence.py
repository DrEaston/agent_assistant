"""Optional Google Cloud Storage persistence for the local SQLite app.

This keeps the current SQLite/file-based app shape while making Cloud Run
runtime changes durable enough for personal use.
"""

import os
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path


class CloudStoragePersistence:
    def __init__(self, bucket_name, db_path, uploads_dir, prefix="dieter"):
        self.bucket_name = (bucket_name or "").strip()
        self.db_path = Path(db_path)
        self.uploads_dir = Path(uploads_dir)
        self.prefix = (prefix or "dieter").strip().strip("/")
        self._lock = threading.RLock()
        self._client = None
        self._bucket = None
        self.enabled = bool(self.bucket_name)

    @classmethod
    def from_env(cls, db_path, uploads_dir):
        bucket_name = os.getenv("GCS_BUCKET") or os.getenv("DIETER_GCS_BUCKET")
        prefix = os.getenv("GCS_PREFIX") or os.getenv("DIETER_GCS_PREFIX") or "dieter"
        return cls(bucket_name, db_path, uploads_dir, prefix)

    @property
    def db_blob_name(self):
        return f"{self.prefix}/projects.db"

    @property
    def uploads_prefix(self):
        return f"{self.prefix}/uploads/"

    def _get_bucket(self):
        if not self.enabled:
            return None
        if self._bucket is None:
            try:
                from google.cloud import storage
            except ImportError as exc:
                raise RuntimeError("google-cloud-storage is not installed") from exc
            self._client = storage.Client()
            self._bucket = self._client.bucket(self.bucket_name)
        return self._bucket

    def restore(self):
        """Restore the database and uploaded files from Cloud Storage if present."""
        if not self.enabled:
            return {"enabled": False, "database_restored": False, "uploads_restored": 0}
        with self._lock:
            bucket = self._get_bucket()
            database_restored = self._restore_database(bucket)
            uploads_restored = self._restore_uploads(bucket)
            return {
                "enabled": True,
                "database_restored": database_restored,
                "uploads_restored": uploads_restored,
            }

    def _restore_database(self, bucket):
        blob = bucket.blob(self.db_blob_name)
        if not blob.exists():
            return False
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(self.db_path))
        return True

    def _restore_uploads(self, bucket):
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for blob in bucket.list_blobs(prefix=self.uploads_prefix):
            if blob.name.endswith("/"):
                continue
            relative_name = blob.name.removeprefix(self.uploads_prefix)
            if not relative_name:
                continue
            destination = self.uploads_dir / relative_name
            destination.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(destination))
            count += 1
        return count

    def backup_database(self):
        """Upload a consistent SQLite backup snapshot to Cloud Storage."""
        if not self.enabled or not self.db_path.exists():
            return False
        with self._lock:
            bucket = self._get_bucket()
            with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                source = sqlite3.connect(str(self.db_path))
                target = sqlite3.connect(str(tmp_path))
                try:
                    source.backup(target)
                finally:
                    target.close()
                    source.close()
                bucket.blob(self.db_blob_name).upload_from_filename(str(tmp_path))
                return True
            finally:
                tmp_path.unlink(missing_ok=True)

    def sync_upload_file(self, path):
        """Upload one file under uploads_dir to Cloud Storage."""
        if not self.enabled:
            return False
        path = Path(path)
        if not path.exists() or not path.is_file():
            return False
        try:
            relative_path = path.resolve().relative_to(self.uploads_dir.resolve())
        except ValueError:
            return False
        with self._lock:
            bucket = self._get_bucket()
            blob_name = f"{self.uploads_prefix}{relative_path.as_posix()}"
            bucket.blob(blob_name).upload_from_filename(str(path))
            return True

    def sync_uploads(self):
        """Upload all local files under uploads_dir to Cloud Storage."""
        if not self.enabled or not self.uploads_dir.exists():
            return 0
        count = 0
        for path in self.uploads_dir.rglob("*"):
            if path.is_file() and self.sync_upload_file(path):
                count += 1
        return count

    def backup_all(self):
        """Upload the database and all local uploads."""
        return {
            "database_uploaded": self.backup_database(),
            "uploads_uploaded": self.sync_uploads(),
        }

    def seed_if_empty(self):
        """Create the first durable snapshot if no database object exists yet."""
        if not self.enabled or not self.db_path.exists():
            return False
        with self._lock:
            bucket = self._get_bucket()
            if bucket.blob(self.db_blob_name).exists():
                return False
        self.backup_database()
        self.sync_uploads()
        return True

    def copy_bundled_uploads_if_needed(self, bundled_uploads_dir):
        """Copy image assets bundled into the image when no cloud uploads exist."""
        if not bundled_uploads_dir or not Path(bundled_uploads_dir).exists():
            return 0
        copied = 0
        for source in Path(bundled_uploads_dir).rglob("*"):
            if not source.is_file():
                continue
            destination = self.uploads_dir / source.relative_to(bundled_uploads_dir)
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            copied += 1
        return copied
