"""
Personal Project Agent - FastAPI Backend
Clean backend with separate frontend consuming /api endpoints.
"""

import os
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

from fastapi import FastAPI, Request, Form, HTTPException, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from database import Database
from pathlib import Path
from pydantic import BaseModel
import json
import shutil
import uuid
from jinja2 import Environment, FileSystemLoader
from llm_service import LLMService
from agent_service import AgentService
from priority_review_service import PriorityReviewService
from typing import Optional, List

# Initialize FastAPI
app = FastAPI(title="Project Agent API", version="1.0")

# Setup templates
templates_dir = Path(__file__).parent / "templates"
templates_dir.mkdir(exist_ok=True)
jinja_env = Environment(loader=FileSystemLoader(str(templates_dir)))
db_path = Path(os.getenv("DB_PATH", "projects.db"))
bundled_db_path = Path(__file__).parent / "projects.db"
if db_path != bundled_db_path and not db_path.exists() and bundled_db_path.exists():
    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(bundled_db_path, db_path)

uploads_dir = Path(os.getenv("UPLOADS_DIR", str(Path(__file__).parent / "uploads")))
recipe_uploads_dir = uploads_dir / "recipe_images"
recipe_uploads_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")

# Initialize database
db = Database(str(db_path))
db.init()
if db.get_project_count() == 0:
    db.populate_sample_data()

# Initialize LLM service
try:
    llm_service = LLMService()
except ValueError as e:
    llm_service = None
    print(f"Warning: LLM service not available - {e}")

agent_service = AgentService(db, llm_service)
priority_review_service = PriorityReviewService(db, agent_service)


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class ProjectIn(BaseModel):
    name: str
    description: str = ""
    priority_score: int = 3

class NoteIn(BaseModel):
    content: str

class ActionIn(BaseModel):
    title: str
    priority: str = "medium"

class BlockerIn(BaseModel):
    description: str
    severity: str = "medium"

class GoalIn(BaseModel):
    title: str
    target_completion: str = ""

class ChatMessage(BaseModel):
    content: str
    include_context: bool = True
    conversation_history: Optional[List[dict]] = None

class ApplyReviewIn(BaseModel):
    review_id: int


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def dict_from_row(row):
    """Convert sqlite3.Row to dict."""
    if row is None:
        return None
    return dict(row)

def dicts_from_rows(rows):
    """Convert list of sqlite3.Row to list of dicts."""
    return [dict(r) for r in rows]


# ============================================================================
# API ROUTES - DASHBOARD
# ============================================================================

@app.get("/api/dashboard")
def api_dashboard():
    """Get dashboard data: today's recommended project, next action, blockers, projects."""
    return agent_service.build_dashboard_context()


# ============================================================================
# API ROUTES - LLM CHAT
# ============================================================================

@app.post("/api/chat")
def api_chat(message: ChatMessage):
    """Chat with the project agent. The agent can update local project memory."""
    project_context = None
    if message.include_context:
        project_context = api_dashboard()

    db.add_chat_message("user", message.content)

    result = agent_service.chat(
        user_message=message.content,
        project_context=project_context,
        conversation_history=message.conversation_history,
    )

    result["model"] = (
        llm_service.provider.model
        if llm_service and hasattr(llm_service.provider, "model")
        else "local-planner"
    )
    db.add_chat_message("assistant", result["response"], result["model"])
    return result


@app.get("/api/chat/history")
def api_chat_history(limit: int = 50):
    """Get recent persisted chat messages."""
    return {"messages": dicts_from_rows(db.get_chat_messages(limit))}


@app.post("/api/priority-review")
def api_create_priority_review():
    """Ask the review model to produce a stored priority refactor plan."""
    return priority_review_service.create_review()


@app.get("/api/priority-review/latest")
def api_get_latest_priority_review():
    """Get the latest priority review plan."""
    review = priority_review_service.get_latest_review()
    if not review:
        raise HTTPException(status_code=404, detail="No priority review found")
    return review


@app.get("/api/priority-review/{review_id}")
def api_get_priority_review(review_id: int):
    """Get a stored priority review plan."""
    review = priority_review_service.get_review(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Priority review not found")
    return review


@app.post("/api/priority-review/{review_id}/apply")
def api_apply_priority_review(review_id: int):
    """Apply pending instructions from a priority review."""
    result = priority_review_service.apply_review(review_id)
    if not result:
        raise HTTPException(status_code=404, detail="Priority review not found")
    return result


@app.get("/api/codex-work-packet")
def api_codex_work_packet(review_id: Optional[int] = None):
    """Build a Markdown work packet for Codex from current priorities."""
    return priority_review_service.build_codex_work_packet(review_id)


@app.post("/api/codex-work-packet/save")
def api_save_codex_work_packet(review_id: Optional[int] = None):
    """Save the current Codex work packet to codex_work_packet.md."""
    return priority_review_service.save_codex_work_packet(review_id)


# ============================================================================
# API ROUTES - PROJECTS
# ============================================================================

@app.get("/api/projects")
def api_get_projects():
    """Get all projects."""
    return {"projects": dicts_from_rows(db.get_all_projects())}

@app.post("/api/projects")
def api_create_project(project: ProjectIn):
    """Create a new project."""
    project_id = db.add_project(project.name, project.description, project.priority_score)
    return {"status": "success", "project_id": project_id}

@app.get("/api/projects/{project_id}")
def api_get_project(project_id: int):
    """Get a specific project with all related data."""
    project = dict_from_row(db.get_project_by_id(project_id))
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    return {
        "project": project,
        "notes": dicts_from_rows(db.get_notes(project_id)),
        "actions": dicts_from_rows(db.get_recommended_actions(project_id)),
        "blockers": dicts_from_rows(db.get_blockers(project_id)),
        "goals": dicts_from_rows(db.get_weekly_goals(project_id)),
    }


# ============================================================================
# API ROUTES - NOTES
# ============================================================================

@app.get("/api/projects/{project_id}/notes")
def api_get_notes(project_id: int):
    """Get all notes for a project."""
    return {"notes": dicts_from_rows(db.get_notes(project_id))}

@app.post("/api/projects/{project_id}/notes")
def api_add_note(project_id: int, note: NoteIn):
    """Add a note to a project."""
    db.add_note(project_id, note.content)
    return {"status": "success"}


# ============================================================================
# API ROUTES - ACTIONS
# ============================================================================

@app.get("/api/projects/{project_id}/actions")
def api_get_actions(project_id: int):
    """Get all actions for a project."""
    return {"actions": dicts_from_rows(db.get_recommended_actions(project_id))}

@app.post("/api/projects/{project_id}/actions")
def api_add_action(project_id: int, action: ActionIn):
    """Add an action to a project."""
    db.add_recommended_action(project_id, action.title, action.priority)
    return {"status": "success"}

@app.post("/api/projects/{project_id}/actions/{action_id}/complete")
def api_complete_action(project_id: int, action_id: int):
    """Mark an action as complete."""
    db.mark_recommended_action_complete(action_id)
    return {"status": "success"}


# ============================================================================
# API ROUTES - BLOCKERS
# ============================================================================

@app.get("/api/projects/{project_id}/blockers")
def api_get_blockers(project_id: int):
    """Get all blockers for a project."""
    return {"blockers": dicts_from_rows(db.get_blockers(project_id))}

@app.post("/api/projects/{project_id}/blockers")
def api_add_blocker(project_id: int, blocker: BlockerIn):
    """Add a blocker to a project."""
    db.add_blocker(project_id, blocker.description, blocker.severity)
    return {"status": "success"}

@app.delete("/api/projects/{project_id}/blockers/{blocker_id}")
def api_delete_blocker(project_id: int, blocker_id: int):
    """Delete a blocker."""
    db.delete_blocker(blocker_id)
    return {"status": "success"}


# ============================================================================
# API ROUTES - GOALS
# ============================================================================

@app.get("/api/projects/{project_id}/goals")
def api_get_goals(project_id: int):
    """Get all goals for a project."""
    return {"goals": dicts_from_rows(db.get_weekly_goals(project_id))}

@app.post("/api/projects/{project_id}/goals")
def api_add_goal(project_id: int, goal: GoalIn):
    """Add a goal to a project."""
    db.add_weekly_goal(project_id, goal.title)
    return {"status": "success"}

@app.post("/api/projects/{project_id}/goals/{goal_id}/complete")
def api_complete_goal(project_id: int, goal_id: int):
    """Mark a goal as complete."""
    db.mark_goal_complete(goal_id)
    return {"status": "success"}


# ============================================================================
# HTML ROUTES (Frontend)
# ============================================================================

@app.get("/")
def dashboard(request: Request):
    """Main dashboard view - renders HTML."""
    data = api_dashboard()
    # Convert to JSON and back to ensure all dicts are pure Python dicts
    data_json = json.dumps(data, default=str)
    data_clean = json.loads(data_json)
    
    context = {
        "request": request,
        "projects": data_clean["projects"],
        "recommended_project": data_clean["recommended_project"],
        "next_action": data_clean["next_action"],
        "blockers": data_clean["blockers"],
        "actions": data_clean["actions"],
        "goals": data_clean["goals"],
        "stats": data_clean["stats"],
    }
    template = jinja_env.get_template("dashboard.html")
    html = template.render(context)
    return HTMLResponse(html)


@app.get("/projects")
def projects_page(request: Request):
    """Projects list page."""
    projects = dicts_from_rows(db.get_all_projects())
    context = {
        "request": request,
        "projects": projects,
    }
    template = jinja_env.get_template("projects.html")
    html = template.render(context)
    return HTMLResponse(html)


@app.get("/chat")
def chat_page(request: Request):
    """Project agent chat page."""
    context = {
        "request": request,
        "dashboard": api_dashboard(),
    }
    template = jinja_env.get_template("chat.html")
    html = template.render(context)
    return HTMLResponse(html)


@app.get("/projects/{project_id}")
def project_detail(request: Request, project_id: int):
    """Project detail page."""
    project = dict_from_row(db.get_project_by_id(project_id))
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    context = {
        "request": request,
        "project": project,
        "notes": dicts_from_rows(db.get_notes(project_id)),
        "actions": dicts_from_rows(db.get_recommended_actions(project_id)),
        "blockers": dicts_from_rows(db.get_blockers(project_id)),
        "goals": dicts_from_rows(db.get_weekly_goals(project_id)),
    }
    template = jinja_env.get_template("project_detail.html")
    html = template.render(context)
    return HTMLResponse(html)


@app.get("/projects/{project_id}/actions/{action_id}")
def action_detail(request: Request, project_id: int, action_id: int):
    """Task detail page with checklist and links to app-specific work surfaces."""
    project = dict_from_row(db.get_project_by_id(project_id))
    action = dict_from_row(db.get_recommended_action(action_id))
    if not project or not action or action["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Task not found")

    context = {
        "request": request,
        "project": project,
        "action": action,
        "steps": dicts_from_rows(db.get_task_steps(action_id)),
        "recipe_image_count": len(db.get_recipe_images(action_id)),
        "recipe_import_url": f"/apps/recipes/import?project_id={project_id}&action_id={action_id}",
    }
    template = jinja_env.get_template("action_detail.html")
    html = template.render(context)
    return HTMLResponse(html)


@app.get("/apps/recipes/import")
def recipe_import_page(request: Request, project_id: int, action_id: int):
    """Recipe app import surface for uploading recipe images."""
    project = dict_from_row(db.get_project_by_id(project_id))
    action = dict_from_row(db.get_recommended_action(action_id))
    if not project or not action or action["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Recipe import task not found")

    context = {
        "request": request,
        "project": project,
        "action": action,
        "recipe_images": dicts_from_rows(db.get_recipe_images(action_id)),
    }
    template = jinja_env.get_template("recipe_import.html")
    html = template.render(context)
    return HTMLResponse(html)


# ============================================================================
# FORM SUBMISSION ROUTES (Post-Redirect-Get Pattern)
# ============================================================================

@app.post("/projects/create")
def create_project_form(name: str = Form(...), description: str = Form(""), priority_score: int = Form(3)):
    """Create project via form."""
    db.add_project(name, description, priority_score)
    return RedirectResponse(url="/projects", status_code=303)

@app.post("/projects/{project_id}/notes/add")
@app.post("/projects/{project_id}/notes/create")
def add_note_form(project_id: int, content: str = Form(...)):
    """Add note via form."""
    db.add_note(project_id, content)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)

@app.post("/projects/{project_id}/actions/add")
@app.post("/projects/{project_id}/actions/create")
def add_action_form(project_id: int, title: str = Form(None), action: str = Form(None), priority: str = Form(...)):
    """Add action via form."""
    db.add_recommended_action(project_id, title or action, priority)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)

@app.post("/projects/{project_id}/actions/{action_id}/complete")
def complete_action_form(project_id: int, action_id: int):
    """Mark action complete via form."""
    db.mark_recommended_action_complete(action_id)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)

@app.post("/projects/{project_id}/actions/{action_id}/steps/create")
def add_task_step_form(project_id: int, action_id: int, step: str = Form(...)):
    """Add a checklist step to a task."""
    db.add_task_step(action_id, step)
    return RedirectResponse(url=f"/projects/{project_id}/actions/{action_id}", status_code=303)

@app.post("/projects/{project_id}/actions/{action_id}/steps/{step_id}/complete")
def complete_task_step_form(project_id: int, action_id: int, step_id: int):
    """Mark a task checklist step complete."""
    db.mark_task_step_complete(step_id)
    return RedirectResponse(url=f"/projects/{project_id}/actions/{action_id}", status_code=303)

@app.post("/apps/recipes/import/upload")
async def upload_recipe_images_form(
    project_id: int = Form(...),
    action_id: int = Form(...),
    files: List[UploadFile] = File(...),
):
    """Upload recipe image files to the recipe app import queue."""
    action = dict_from_row(db.get_recommended_action(action_id))
    if not action or action["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Task not found")

    for upload in files:
        if not upload.filename:
            continue
        if upload.content_type and not upload.content_type.startswith("image/"):
            continue

        original_name = Path(upload.filename).name
        extension = Path(original_name).suffix.lower()
        stored_name = f"{uuid.uuid4().hex}{extension}"
        destination = recipe_uploads_dir / stored_name
        with destination.open("wb") as handle:
            shutil.copyfileobj(upload.file, handle)

        db.add_recipe_image(
            project_id,
            action_id,
            f"recipe_images/{stored_name}",
            original_name,
            upload.content_type or "",
        )

    return RedirectResponse(
        url=f"/apps/recipes/import?project_id={project_id}&action_id={action_id}",
        status_code=303,
    )

@app.post("/projects/{project_id}/blockers/add")
@app.post("/projects/{project_id}/blockers/create")
def add_blocker_form(project_id: int, description: str = Form(...), severity: str = Form(...)):
    """Add blocker via form."""
    db.add_blocker(project_id, description, severity)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)

@app.post("/projects/{project_id}/goals/add")
@app.post("/projects/{project_id}/goals/create")
def add_goal_form(project_id: int, title: str = Form(None), goal: str = Form(None), target_completion: str = Form("")):
    """Add goal via form."""
    db.add_weekly_goal(project_id, title or goal)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)

@app.post("/projects/{project_id}/goals/{goal_id}/complete")
def complete_goal_form(project_id: int, goal_id: int):
    """Mark goal as complete via form."""
    db.mark_goal_complete(goal_id)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)

@app.post("/projects/{project_id}/blockers/{blocker_id}/delete")
def delete_blocker_form(project_id: int, blocker_id: int):
    """Delete blocker via form."""
    db.delete_blocker(blocker_id)
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)


# ============================================================================
# SERVER STARTUP
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
