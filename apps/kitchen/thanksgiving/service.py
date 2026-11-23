"""Thanksgiving as its own Cloud Run service (`uvicorn apps.kitchen.thanksgiving.service:app`).

It keeps its own SQLite database (backed up to Cloud Storage under GCS_PREFIX, e.g. "thanksgiving"), so the rest
of Dieter can be changed and redeployed without touching it. The main app signs the user in and forwards
/thanksgiving requests here with a signed X-Dieter-User header (core/internal.py); anything without a valid
header is refused. Calls back to the main app (the Kitchen recipe library, problem reports) go to
PLATFORM_URL, signed the same way.

Settings: DB_PATH, GCS_BUCKET, GCS_PREFIX, DIETER_INTERNAL_KEY, PLATFORM_URL, OPENAI_API_KEY (+ model names),
APP_TIMEZONE.
"""

import os
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from cloud_persistence import CloudStoragePersistence
from core import internal
from database import Database, reset_current_user_id, set_current_user_id

from . import router as tg_router

db_path = Path(os.getenv("DB_PATH", "thanksgiving.db"))
uploads_dir = Path(os.getenv("UPLOADS_DIR", "/tmp/thanksgiving_uploads"))
persistence = CloudStoragePersistence.from_env(db_path, uploads_dir)
db = Database(str(db_path))  # nothing opens it until ensure_ready()
if persistence.enabled:
    db.after_commit = persistence.schedule_backup
_ready = {"done": False}
_ready_lock = threading.Lock()


def ensure_ready():
    """Load the plan from Cloud Storage on the first real request, not at startup.

    During a deploy the new version starts minutes before it takes over; the old version keeps serving (and saving)
    until then. Loading at the first request means the new version starts from the old one's last save. If loading
    fails, raise rather than start from an empty database whose first save would overwrite the real plan."""
    if _ready["done"]:
        return
    with _ready_lock:
        if _ready["done"]:
            return
        restored = persistence.restore()
        if restored.get("enabled"):
            print(f"Thanksgiving restore: {restored}")
        db.init()
        _ready["done"] = True

platform_url = os.getenv("PLATFORM_URL", "")
if not platform_url:
    print("Warning: PLATFORM_URL isn't set, so Finish trip can't make Kitchen next-trip lists")
tg_router.configure(db, platform=tg_router.RemotePlatform(platform_url) if platform_url
                    else tg_router.NoKitchenLists(db))

app = FastAPI(title="Dieter Thanksgiving")
app.include_router(tg_router.router)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.middleware("http")
async def signed_user(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)
    try:
        await run_in_threadpool(ensure_ready)
    except Exception as exc:
        print(f"Warning: Thanksgiving restore failed - {exc}")
        return JSONResponse({"detail": "Thanksgiving is starting up. Try again in a moment."}, status_code=503)
    user = internal.verify(request.headers.get(internal.HEADER), allow_signed_out=True)
    signed_out_ok = request.method in ("GET", "HEAD") and tg_router.is_public_path(request.url.path)
    if not user or (not user.get("id") and not signed_out_ok):
        return JSONResponse({"detail": "Open Thanksgiving from askdieter.ai."}, status_code=401)
    request.state.current_user = user
    user_token = set_current_user_id(user["id"])
    ctx_token = internal.current_user.set(user)
    try:
        return await call_next(request)
    finally:
        internal.current_user.reset(ctx_token)
        reset_current_user_id(user_token)
        if persistence.enabled:
            await run_in_threadpool(persistence.save_pending)  # saved before the answer goes back


@app.on_event("shutdown")
def flush_backup():
    if persistence.enabled and _ready["done"]:  # never upload a database that was never loaded
        persistence.flush()
