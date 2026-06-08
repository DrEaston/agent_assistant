"""
Personal Project Agent - FastAPI Backend
Clean backend with separate frontend consuming /api endpoints.
"""

import os
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

from fastapi import FastAPI, Request, Form, HTTPException, File, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
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
from recipe_ocr_service import RecipeOCRService
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
recipe_ocr_service = RecipeOCRService(uploads_dir)

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

def prepare_recipe_image_groups(groups):
    """Parse stored extraction sections for rendering."""
    prepared = []
    for group in groups:
        group_data = dict(group)
        try:
            group_data["sections"] = json.loads(group_data.get("sections_json") or "[]")
        except json.JSONDecodeError:
            group_data["sections"] = []
        prepared.append(group_data)
    return prepared

def prepare_recipe_complete_meals(meals):
    """Parse complete-meal quality notes for rendering."""
    prepared = []
    for meal in meals:
        meal_data = dict(meal)
        try:
            meal_data["quality_notes"] = json.loads(meal_data.get("quality_notes_json") or "[]")
        except json.JSONDecodeError:
            meal_data["quality_notes"] = ["Quality notes could not be parsed."]
        prepared.append(meal_data)
    return prepared

def build_recipe_extraction_stats(groups):
    """Summarize OCR progress for uploaded recipe card pairs."""
    total = len(groups)
    processed = sum(1 for group in groups if group.get("extraction_status") == "extracted")
    processing = sum(1 for group in groups if group.get("extraction_status") == "processing")
    errors = sum(1 for group in groups if group.get("extraction_status") == "error")
    pending = total - processed
    return {
        "total": total,
        "processed": processed,
        "pending": pending,
        "processing": processing,
        "errors": errors,
        "progress_percent": round((processed / total) * 100) if total else 0,
    }

def get_recipe_app_context():
    """Resolve planner-backed recipe app links and import status."""
    db.sync_recipe_complete_meals_from_extractions()
    project = db.get_project_by_name("Recipe display app")
    if not project:
        return {
            "project": None,
            "import_action": None,
            "import_url": "",
            "groups": [],
            "complete_meals": [],
            "components": [],
            "stats": {
                "total_pairs": 0,
                "scraped_pairs": 0,
                "pending_pairs": 0,
                "sections": 0,
                "complete_meals": 0,
                "complete_meals_ready": 0,
                "complete_meals_needing_review": 0,
                "components": 0,
            },
        }

    import_action = db.find_recommended_action(
        project["id"],
        "Import the first batch of recipe images",
    )
    if not import_action:
        return {
            "project": project,
            "import_action": None,
            "import_url": "",
            "groups": [],
            "complete_meals": [],
            "components": [],
            "stats": {
                "total_pairs": 0,
                "scraped_pairs": 0,
                "pending_pairs": 0,
                "sections": 0,
                "complete_meals": 0,
                "complete_meals_ready": 0,
                "complete_meals_needing_review": 0,
                "components": 0,
            },
        }

    groups = prepare_recipe_image_groups(db.get_recipe_image_groups(import_action["id"]))
    complete_meals = prepare_recipe_complete_meals(db.get_recipe_complete_meals())
    components = dicts_from_rows(db.get_recipe_components())
    scraped_pairs = sum(1 for group in groups if group.get("extraction_status") == "extracted")
    sections = sum(len(group.get("sections", [])) for group in groups)
    complete_meals_ready = sum(1 for meal in complete_meals if meal.get("status") == "ready")
    complete_meals_needing_review = len(complete_meals) - complete_meals_ready
    return {
        "project": project,
        "import_action": import_action,
        "import_url": f"/apps/recipes/import?project_id={project['id']}&action_id={import_action['id']}",
        "groups": groups,
        "complete_meals": complete_meals,
        "components": components,
        "stats": {
            "total_pairs": len(groups),
            "scraped_pairs": scraped_pairs,
            "pending_pairs": len(groups) - scraped_pairs,
            "sections": sections,
            "complete_meals": len(complete_meals),
            "complete_meals_ready": complete_meals_ready,
            "complete_meals_needing_review": complete_meals_needing_review,
            "components": len(components),
        },
    }

def is_auto_work_prompt(message):
    """Detect old synthetic chat prompts created by the chat page itself."""
    return (
        message.get("role") == "user"
        and message.get("content", "").strip().lower() == "what should i work on next?"
    )

def is_auto_work_response(message):
    """Detect old synthetic work-packet responses paired with auto prompts."""
    content = message.get("content", "").strip().lower()
    return (
        message.get("role") == "assistant"
        and "work on" in content
        and "recipe display app" in content
        and (
            "work packet" in content
            or "recommended next action" in content
            or "crisp work packet" in content
        )
    )

def filter_auto_chat_noise(messages):
    """Hide generated work-packet refresh chatter from chat history."""
    filtered = []
    skip_next_auto_response = False

    for message in messages:
        if is_auto_work_prompt(message):
            skip_next_auto_response = True
            continue

        if skip_next_auto_response and is_auto_work_response(message):
            skip_next_auto_response = False
            continue

        skip_next_auto_response = False
        filtered.append(message)

    return filtered

def normalize_step_text(text):
    """Normalize step text for lightweight duplicate detection."""
    return " ".join(text.lower().strip(" .,:;-").split())

def build_step_review(action, steps):
    """Build a conservative, previewable cleanup proposal for task steps."""
    open_steps = [step for step in steps if step["status"] == "open"]
    current_steps = [step["step"] for step in open_steps]
    action_text = action["action"].lower()

    if "recipe image" in action_text or "recipe images" in action_text:
        proposed_steps = [
            "Deploy the planner and recipe import app locally over Wi-Fi for phone access",
            "Keep local projects.db and uploads/ available for Wi-Fi-hosted phone uploads",
            "Open the local Wi-Fi Recipe Import page from a phone",
            "Upload the first batch of recipe images",
            "Confirm uploaded images appear in the import queue with metadata",
            "Mark uploaded images ready for OCR",
        ]
        reasons = [
            "Reflects the current local Wi-Fi hosting decision.",
            "Separates app deployment from the actual image upload workflow.",
            "Merges overlapping upload/storage/metadata/queue steps into clearer milestones.",
            "Keeps OCR readiness after images are uploaded and visible.",
        ]
    else:
        proposed_steps = []
        seen = set()
        for step in current_steps:
            normalized = normalize_step_text(step)
            if normalized and normalized not in seen:
                seen.add(normalized)
                proposed_steps.append(step.strip())
        reasons = ["Removed exact duplicate open steps."] if len(proposed_steps) != len(current_steps) else [
            "No obvious cleanup found; proposed order preserves the current open steps."
        ]

    return {
        "current_steps": current_steps,
        "proposed_steps": proposed_steps,
        "reasons": reasons,
    }

def build_action_codex_plan(project, action, selected_steps, blockers, notes, recipe_import_url):
    """Format a task-level work packet for Codex from selected checklist steps."""
    step_lines = "\n".join(
        f"{index}. {step['step']}"
        for index, step in enumerate(selected_steps, 1)
    ) or "No specific steps selected. Clarify the next implementable step before editing code."

    blocker_lines = "\n".join(f"- {blocker['description']} ({blocker['severity']})" for blocker in blockers) or "- None recorded"
    note_lines = "\n".join(f"- {note['content']}" for note in notes[:5]) or "- None recorded"

    recipe_context = ""
    if "recipe" in project["name"].lower() or "recipe" in action["action"].lower():
        recipe_context = f"""
Recipe app boundary:
- Planner task pages own task status, checklists, and Codex planning.
- Recipe app pages own image upload/import workflow.
- Recipe import page: {recipe_import_url}
"""

    return f"""# Codex Work Packet

## Project
{project['name']}

## Task
{action['action']}

## Objective
Implement or advance the selected task steps below. Keep the work scoped to this task unless a shared helper is clearly required.

## Selected Steps
{step_lines}

## Current Status
- Task priority: {action['priority']}
- Task status: {action['status']}

## Blockers
{blocker_lines}

## Recent Project Notes
{note_lines}
{recipe_context}
## Implementation Guidance
- Preserve the existing FastAPI + Jinja structure.
- Keep planner concerns separate from embedded app surfaces.
- Update the database layer through `database.py` rather than ad hoc SQL in route handlers.
- Run a focused verification after changes and report anything not tested.
"""


# ============================================================================
# API ROUTES - DASHBOARD
# ============================================================================

@app.get("/api/dashboard")
def api_dashboard():
    """Get dashboard data: today's recommended project, next action, blockers, projects."""
    return agent_service.build_dashboard_context()


@app.get("/api/work-packet")
def api_work_packet():
    """Get the current work packet without writing to chat history."""
    context = agent_service.build_dashboard_context()
    return {"work_packet": agent_service.build_work_packet(context)}


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
    try:
        rows = dicts_from_rows(db.get_chat_messages(limit))
        return {"messages": filter_auto_chat_noise(rows)}
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"messages": [], "error": f"Could not load chat history: {exc}"},
        )


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
    
    recipe_app = get_recipe_app_context()

    context = {
        "request": request,
        "projects": data_clean["projects"],
        "recommended_project": data_clean["recommended_project"],
        "next_action": data_clean["next_action"],
        "blockers": data_clean["blockers"],
        "actions": data_clean["actions"],
        "goals": data_clean["goals"],
        "stats": data_clean["stats"],
        "recipe_app_url": "/apps/recipes" if recipe_app["project"] else "",
        "recipe_app": recipe_app,
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


@app.get("/projects/{project_id}/actions/{action_id}/steps/reviews/{review_id}")
def step_review_detail(request: Request, project_id: int, action_id: int, review_id: int):
    """Preview a proposed step cleanup before applying it."""
    project = dict_from_row(db.get_project_by_id(project_id))
    action = dict_from_row(db.get_recommended_action(action_id))
    review = dict_from_row(db.get_task_step_review(review_id))
    if not project or not action or not review:
        raise HTTPException(status_code=404, detail="Step review not found")
    if action["project_id"] != project_id or review["action_id"] != action_id:
        raise HTTPException(status_code=404, detail="Step review not found")

    payload = json.loads(review["payload"])
    context = {
        "request": request,
        "project": project,
        "action": action,
        "review": review,
        "current_steps": payload.get("current_steps", []),
        "proposed_steps": payload.get("proposed_steps", []),
        "reasons": payload.get("reasons", []),
    }
    template = jinja_env.get_template("step_review.html")
    html = template.render(context)
    return HTMLResponse(html)


@app.post("/projects/{project_id}/actions/{action_id}/codex-plan")
def codex_plan_preview(
    request: Request,
    project_id: int,
    action_id: int,
    step_ids: Optional[List[int]] = Form(None),
):
    """Preview a Codex work packet for selected task steps."""
    project = dict_from_row(db.get_project_by_id(project_id))
    action = dict_from_row(db.get_recommended_action(action_id))
    if not project or not action or action["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Task not found")

    all_steps = dicts_from_rows(db.get_task_steps(action_id))
    selected_ids = set(step_ids or [])
    selected_steps = [
        step for step in all_steps
        if step["status"] == "open" and (not selected_ids or step["id"] in selected_ids)
    ]
    recipe_import_url = f"/apps/recipes/import?project_id={project_id}&action_id={action_id}"
    markdown = build_action_codex_plan(
        project,
        action,
        selected_steps,
        dicts_from_rows(db.get_blockers(project_id)),
        dicts_from_rows(db.get_notes(project_id)),
        recipe_import_url,
    )
    context = {
        "request": request,
        "project": project,
        "action": action,
        "selected_steps": selected_steps,
        "selected_step_ids": [step["id"] for step in selected_steps],
        "markdown": markdown,
        "saved_path": "",
    }
    template = jinja_env.get_template("codex_plan.html")
    html = template.render(context)
    return HTMLResponse(html)


@app.get("/apps/recipes")
def recipe_home_page(request: Request):
    """Recipe app home page."""
    recipe_app = get_recipe_app_context()
    if not recipe_app["project"]:
        raise HTTPException(status_code=404, detail="Recipe app project not found")

    context = {
        "request": request,
        "recipe_app": recipe_app,
        "project": recipe_app["project"],
        "import_action": recipe_app["import_action"],
        "groups": recipe_app["groups"],
        "complete_meals": recipe_app["complete_meals"],
        "components": recipe_app["components"],
        "stats": recipe_app["stats"],
    }
    template = jinja_env.get_template("recipe_home.html")
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
        "recipe_image_groups": prepare_recipe_image_groups(db.get_recipe_image_groups(action_id)),
        "recipe_image_roles": [
            {"value": "front", "label": "Front"},
            {"value": "back", "label": "Back"},
            {"value": "extra", "label": "Extra page"},
        ],
    }
    context["extraction_stats"] = build_recipe_extraction_stats(context["recipe_image_groups"])
    template = jinja_env.get_template("recipe_import.html")
    html = template.render(context)
    return HTMLResponse(html)


@app.post("/apps/recipes/components/analyze")
def analyze_recipe_components_form():
    """Analyze ready complete meals into reusable components."""
    db.sync_recipe_complete_meals_from_extractions()
    meals = prepare_recipe_complete_meals(db.get_recipe_complete_meals())
    for meal in meals:
        if meal.get("status") != "ready":
            continue
        result = recipe_ocr_service.analyze_components(meal)
        if result["status"] == "analyzed":
            db.replace_recipe_components_for_meal(meal["id"], result["components"])

    return RedirectResponse(url="/apps/recipes", status_code=303)


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

@app.post("/projects/{project_id}/actions/{action_id}/steps/{step_id}/reopen")
def reopen_task_step_form(project_id: int, action_id: int, step_id: int):
    """Reopen a completed task checklist step."""
    db.reopen_task_step(step_id)
    return RedirectResponse(url=f"/projects/{project_id}/actions/{action_id}", status_code=303)

@app.post("/projects/{project_id}/actions/{action_id}/steps/review")
def create_step_review_form(project_id: int, action_id: int):
    """Create a previewable cleanup proposal for a task's open steps."""
    action = dict_from_row(db.get_recommended_action(action_id))
    if not action or action["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Task not found")

    steps = dicts_from_rows(db.get_task_steps(action_id))
    payload = build_step_review(action, steps)
    summary = "Suggested cleanup for task steps"
    review_id = db.create_task_step_review(action_id, summary, payload)
    return RedirectResponse(
        url=f"/projects/{project_id}/actions/{action_id}/steps/reviews/{review_id}",
        status_code=303,
    )

@app.post("/projects/{project_id}/actions/{action_id}/steps/reviews/{review_id}/apply")
def apply_step_review_form(project_id: int, action_id: int, review_id: int):
    """Apply a pending task step cleanup proposal."""
    action = dict_from_row(db.get_recommended_action(action_id))
    review = dict_from_row(db.get_task_step_review(review_id))
    if not action or not review or action["project_id"] != project_id or review["action_id"] != action_id:
        raise HTTPException(status_code=404, detail="Step review not found")

    applied = db.apply_task_step_review(review_id)
    if not applied:
        raise HTTPException(status_code=400, detail="Step review has already been applied or is unavailable")
    return RedirectResponse(url=f"/projects/{project_id}/actions/{action_id}", status_code=303)

@app.post("/projects/{project_id}/actions/{action_id}/codex-plan/save")
def save_codex_plan_form(
    request: Request,
    project_id: int,
    action_id: int,
    step_ids: Optional[List[int]] = Form(None),
):
    """Save a task-level Codex work packet to codex_work_packet.md."""
    project = dict_from_row(db.get_project_by_id(project_id))
    action = dict_from_row(db.get_recommended_action(action_id))
    if not project or not action or action["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Task not found")

    all_steps = dicts_from_rows(db.get_task_steps(action_id))
    selected_ids = set(step_ids or [])
    selected_steps = [
        step for step in all_steps
        if step["status"] == "open" and (not selected_ids or step["id"] in selected_ids)
    ]
    recipe_import_url = f"/apps/recipes/import?project_id={project_id}&action_id={action_id}"
    markdown = build_action_codex_plan(
        project,
        action,
        selected_steps,
        dicts_from_rows(db.get_blockers(project_id)),
        dicts_from_rows(db.get_notes(project_id)),
        recipe_import_url,
    )
    output_path = Path("codex_work_packet.md").resolve()
    output_path.write_text(markdown + "\n", encoding="utf-8")
    context = {
        "request": request,
        "project": project,
        "action": action,
        "selected_steps": selected_steps,
        "selected_step_ids": [step["id"] for step in selected_steps],
        "markdown": markdown,
        "saved_path": str(output_path),
    }
    template = jinja_env.get_template("codex_plan.html")
    html = template.render(context)
    return HTMLResponse(html)

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

    groups = db.get_recipe_image_groups(action_id)
    group_count = len(groups)
    open_group_id = None

    for group in groups:
        sides = {image["side"] for image in group["images"]}
        if "front" in sides and "back" not in sides:
            open_group_id = group["id"]
            break

    for upload in files:
        if not upload.filename:
            continue
        if upload.content_type and not upload.content_type.startswith("image/"):
            continue

        if open_group_id:
            image_group_id = open_group_id
            side = "back"
            open_group_id = None
        else:
            group_count += 1
            image_group_id = db.create_recipe_image_group(
                project_id,
                action_id,
                f"Recipe pair {group_count}",
            )
            side = "front"

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
            image_group_id,
            side,
        )

        if side == "front":
            open_group_id = image_group_id

    return RedirectResponse(
        url=f"/apps/recipes/import?project_id={project_id}&action_id={action_id}",
        status_code=303,
    )

@app.post("/apps/recipes/import/extract")
def extract_recipe_images_form(
    project_id: int = Form(...),
    action_id: int = Form(...),
):
    """Run OCR/structured extraction over uploaded recipe image groups."""
    action = dict_from_row(db.get_recommended_action(action_id))
    if not action or action["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Task not found")

    groups = db.get_recipe_image_groups(action_id)
    for group in groups:
        if group.get("extraction_status") == "extracted":
            continue
        db.upsert_recipe_extraction(
            group["id"],
            "processing",
            group.get("ingredients_text", ""),
            group.get("instructions_text", ""),
            group.get("sections_json", "[]"),
            "",
            "",
        )
        result = recipe_ocr_service.extract_group(group)
        db.upsert_recipe_extraction(
            group["id"],
            result["status"],
            result.get("ingredients_text", ""),
            result.get("instructions_text", ""),
            result.get("sections_json", "[]"),
            result.get("raw_response", ""),
            result.get("error", ""),
        )

    return RedirectResponse(
        url=f"/apps/recipes/import?project_id={project_id}&action_id={action_id}",
        status_code=303,
    )

@app.post("/apps/recipes/import/images/{image_id}/assign")
def assign_recipe_image_form(
    image_id: int,
    project_id: int = Form(...),
    action_id: int = Form(...),
    group_id: int = Form(...),
    side: str = Form(...),
):
    """Update an uploaded recipe image's group and role."""
    action = dict_from_row(db.get_recommended_action(action_id))
    image = dict_from_row(db.get_recipe_image(image_id))
    groups = db.get_recipe_image_groups(action_id)
    group_ids = {group["id"] for group in groups}
    allowed_roles = {"front", "back", "extra"}

    if not action or action["project_id"] != project_id:
        raise HTTPException(status_code=404, detail="Task not found")
    if not image or image["project_id"] != project_id or image["action_id"] != action_id:
        raise HTTPException(status_code=404, detail="Image not found")
    if group_id not in group_ids:
        raise HTTPException(status_code=400, detail="Recipe pair not found")
    if side not in allowed_roles:
        raise HTTPException(status_code=400, detail="Unsupported image role")

    db.update_recipe_image_assignment(image_id, group_id, side)
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
