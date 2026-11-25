"""The animation lab (/media/lab): every clip lined up and checked, a viewer to watch clips and the joins between
them slowly, and (next) the player's states. Signed-in only, like the rest of Media."""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from core import ui
from core.media import connector as media

from . import lab

APP = {"id": "media", "title": "Media", "nav_label": "Media", "accent": "today"}
router = APIRouter()
_templates = ui.templates(Path(__file__).parent / "templates")


def _public(built):
    return {k: v for k, v in built.items() if k != "sheets"}


@router.get("/media/lab")
def lab_page(request: Request):
    built = lab.build(media.store())
    problems = sum(1 for c in built["clips"] if c["problems"]) + sum(1 for j in built["joins"] if j["verdict"] == "jump")
    page = _templates.get_template("mascot_lab.html").render(
        app=APP, page_url="/media/lab", lab=_public(built), problems=problems,
        done=request.query_params.get("done"))
    return HTMLResponse(page)


@router.get("/media/lab/clips.json")
def lab_clips():
    return JSONResponse(_public(lab.build(media.store())))


@router.get("/media/lab/sheet/{name}.webp")
def lab_sheet(name: str):
    sheet = lab.build(media.store())["sheets"].get(name)
    if sheet is None:
        return Response(status_code=404)
    return Response(sheet, media_type="image/webp", headers={"Cache-Control": "private, max-age=3600"})


@router.post("/media/lab/rebuild")
def lab_rebuild():
    lab.forget()
    return RedirectResponse("/media/lab?done=Rebuilt+from+your+kept+pictures.", status_code=303)
