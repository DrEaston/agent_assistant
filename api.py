"""
Personal Project Agent - FastAPI Backend
Clean backend with separate frontend consuming /api endpoints.
"""

import os
import secrets
import hmac
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

from fastapi import FastAPI, Request, Form, HTTPException, File, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from database import Database, set_current_user_id, reset_current_user_id, get_current_user_id
from pathlib import Path
from pydantic import BaseModel
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json
import shutil
import uuid
import hashlib
import base64
import re
import threading
import mimetypes
import time
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
from jinja2 import Environment, FileSystemLoader
from llm_service import LLMService
from agent_service import AgentService
from priority_review_service import PriorityReviewService
from recipe_ocr_service import RecipeOCRService
from cloud_persistence import CloudStoragePersistence
from typing import Optional, List

# Initialize FastAPI
app = FastAPI(title="Project Agent API", version="1.0")
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Setup templates
templates_dir = Path(__file__).parent / "templates"
templates_dir.mkdir(exist_ok=True)
jinja_env = Environment(loader=FileSystemLoader(str(templates_dir)))
db_path = Path(os.getenv("DB_PATH", "projects.db"))
bundled_db_path = Path(__file__).parent / "projects.db"
uploads_dir = Path(os.getenv("UPLOADS_DIR", str(Path(__file__).parent / "uploads")))
bundled_uploads_dir = Path(__file__).parent / "uploads"
recipe_uploads_dir = uploads_dir / "recipe_images"
recipe_thumbnails_dir = uploads_dir / "recipe_thumbnails"
cloud_persistence = CloudStoragePersistence.from_env(db_path, uploads_dir)

try:
    restore_result = cloud_persistence.restore()
    if restore_result.get("enabled"):
        print(f"Cloud persistence restore: {restore_result}")
except Exception as exc:
    print(f"Warning: Cloud persistence restore failed - {exc}")

if db_path != bundled_db_path and not db_path.exists() and bundled_db_path.exists():
    db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(bundled_db_path, db_path)

if uploads_dir != bundled_uploads_dir and bundled_uploads_dir.exists() and not any(uploads_dir.rglob("*")):
    cloud_persistence.copy_bundled_uploads_if_needed(bundled_uploads_dir)

recipe_uploads_dir.mkdir(parents=True, exist_ok=True)
recipe_thumbnails_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(uploads_dir)), name="uploads")
recipe_ocr_service = RecipeOCRService(uploads_dir)

# Initialize database
db = Database(str(db_path))
db.init()
if db.get_project_count() == 0:
    db.populate_sample_data()
if cloud_persistence.enabled:
    db.after_commit = cloud_persistence.backup_database
    try:
        cloud_persistence.seed_if_empty()
    except Exception as exc:
        print(f"Warning: Cloud persistence seed failed - {exc}")

# Initialize LLM service
try:
    llm_service = LLMService()
except ValueError as e:
    llm_service = None
    print(f"Warning: LLM service not available - {e}")

agent_service = AgentService(db, llm_service)
priority_review_service = PriorityReviewService(db, agent_service)
recipe_context_lock = threading.RLock()
recipe_maintenance_last_run = None
RECIPE_MAINTENANCE_INTERVAL_SECONDS = 60

SESSION_COOKIE_NAME = "dieter_session"
SESSION_DAYS = 30
PBKDF2_ITERATIONS = 210_000
SESSION_COOKIE_SECURE = bool(os.getenv("K_SERVICE")) or os.getenv("SESSION_COOKIE_SECURE", "").lower() == "true"
REGISTRATION_CODE = os.getenv("DIETER_REGISTRATION_CODE", "")
GUEST_EMAIL = os.getenv("DIETER_GUEST_EMAIL", "guest@askdieter.local")
READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}
STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET", "")
STRAVA_API_BASE = "https://www.strava.com/api/v3"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
STRAVA_REDIRECT_URI = os.getenv("STRAVA_REDIRECT_URI", "")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "")
SPOTIFY_ACCOUNT_BASE = "https://accounts.spotify.com"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"
SPOTIFY_SCOPES = "playlist-modify-private playlist-modify-public playlist-read-private user-read-private user-read-email"


def hash_password(password):
    """Hash a password with stdlib PBKDF2."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PBKDF2_ITERATIONS,
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password, stored_hash):
    """Verify a PBKDF2 password hash."""
    try:
        algorithm, iterations, salt_hex, digest_hex = (stored_hash or "").split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def hash_session_token(token):
    """Store only a hash of browser session tokens."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_login_response(user_id, redirect_to="/"):
    """Create a session and redirect the user."""
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.utcnow() + timedelta(days=SESSION_DAYS)).isoformat()
    db.create_session(user_id, hash_session_token(token), expires_at)
    response = RedirectResponse(url=redirect_to, status_code=303)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        httponly=True,
        secure=SESSION_COOKIE_SECURE,
        samesite="lax",
        max_age=SESSION_DAYS * 24 * 60 * 60,
    )
    return response


def safe_internal_redirect_target(value, fallback="/"):
    """Keep auth redirects on this app."""
    return value if value and value.startswith("/") and not value.startswith("//") else fallback


def clear_login_response(redirect_to="/login"):
    """Clear the login cookie and redirect."""
    response = RedirectResponse(url=redirect_to, status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


def render_auth_page(request, mode="login", error=""):
    """Render the shared login/register page."""
    template = jinja_env.get_template("auth.html")
    return HTMLResponse(template.render({
        "request": request,
        "mode": mode,
        "error": error,
        "has_users": db.get_user_count() > 0,
        "requires_registration_code": bool(REGISTRATION_CODE),
    }))


def redirect_or_unauthorized(request):
    """Return an auth challenge appropriate to browser or API requests."""
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Login required."}, status_code=401)
    return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)


def is_guest_user(user):
    """Return True for the shared read-only guest account."""
    return bool(user and user["role"] == "guest")


def guest_read_only_response(request):
    """Reject writes from guest sessions."""
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Guest access is read-only."}, status_code=403)
    return HTMLResponse(
        """
        <section style="font-family: system-ui, sans-serif; max-width: 36rem; margin: 4rem auto; line-height: 1.5;">
            <h1>Guest access is read-only</h1>
            <p>You can browse the recipe catalogue as a guest, but saving, editing, planning, and deleting need a full account.</p>
            <p><a href="/apps/recipes">Back to recipes</a></p>
        </section>
        """,
        status_code=403,
    )


def get_or_create_guest_user_id():
    """Create the shared guest user on demand and expose the recipe catalogue to it."""
    user = db.get_user_by_email(GUEST_EMAIL)
    if user:
        return user["id"]
    password = secrets.token_urlsafe(32)
    guest_id = db.create_user(GUEST_EMAIL, "Guest", hash_password(password), "guest")
    db.share_recipe_library_with_all_users()
    return guest_id


@app.middleware("http")
async def load_authenticated_user(request: Request, call_next):
    """Load the logged-in user and require authentication for app routes."""
    path = request.url.path
    public_prefixes = ("/login", "/guest-login", "/register", "/logout", "/favicon.ico", "/apps/planner", "/dashboard")
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    user = None
    if token:
        user = db.get_session_user(hash_session_token(token), datetime.utcnow().isoformat())

    request.state.current_user = dict(user) if user else None
    user_token = set_current_user_id(user["id"] if user else None)
    try:
        if not user and not path.startswith(public_prefixes):
            if db.get_user_count() == 0:
                return RedirectResponse(url="/register", status_code=303)
            return redirect_or_unauthorized(request)
        if (
            is_guest_user(user)
            and request.method not in READ_ONLY_METHODS
            and not path.startswith(("/login", "/register", "/logout", "/guest-login"))
        ):
            return guest_read_only_response(request)
        response = await call_next(request)
        return response
    finally:
        reset_current_user_id(user_token)


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

class SchedulerItemIn(BaseModel):
    title: str
    context_label: str = ""
    scheduled_for: str = ""
    notes: str = ""

class ShareIn(BaseModel):
    email: str
    permission: str = "view"

class ChatMessage(BaseModel):
    content: str
    include_context: bool = True
    conversation_history: Optional[List[dict]] = None

class ApplyReviewIn(BaseModel):
    review_id: int

class RecipeEditMessage(BaseModel):
    content: str
    page_url: str = ""
    conversation_history: Optional[List[dict]] = None

class DieterActionMessage(BaseModel):
    content: str
    page_url: str = ""
    page_title: str = ""
    confirmation_token: str = ""
    conversation_history: Optional[List[dict]] = None


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

def _recipe_thumbnail_score(image):
    """Score whether an image has a prominent food/photo region."""
    sample = image.convert("RGB").resize((96, 96))
    width, height = sample.size
    pixels = sample.load()
    nonwhite = 0
    saturation_sum = 0
    variance_sum = 0
    count = 0
    for y in range(0, int(height * 0.65)):
        for x in range(width):
            red, green, blue = pixels[x, y]
            max_channel = max(red, green, blue)
            min_channel = min(red, green, blue)
            saturation = max_channel - min_channel
            is_white = red > 225 and green > 220 and blue > 205
            if not is_white:
                nonwhite += 1
            saturation_sum += saturation
            variance_sum += abs(red - green) + abs(green - blue) + abs(blue - red)
            count += 1
    if not count:
        return 0
    return (nonwhite / count) * 3 + (saturation_sum / count / 255) + (variance_sum / count / 255)

def _crop_food_photo_region(image):
    """Crop toward the food-photo area above the ingredient list."""
    width, height = image.size
    left = int(width * 0.06)
    top = int(height * 0.10)
    right = int(width * 0.94)
    bottom = int(height * 0.49)
    if right <= left or bottom <= top:
        return image
    return image.crop((left, top, right, bottom))

def build_recipe_thumbnail_url(meal):
    """Generate or retrieve a cropped recipe thumbnail from candidate card images."""
    candidates = [
        candidate for candidate in (meal.get("thumbnail_candidates") or "").split("||")
        if candidate
    ]
    primary_thumbnail = meal.get("thumbnail_filename")
    if primary_thumbnail:
        candidates = [primary_thumbnail] + [candidate for candidate in candidates if candidate != primary_thumbnail]
    if not candidates:
        return ""

    cache_key = hashlib.sha1(("v4|" + "|".join(candidates)).encode("utf-8")).hexdigest()[:16]
    thumbnail_name = f"recipe_thumb_{meal.get('id', 'meal')}_{cache_key}.jpg"
    thumbnail_path = recipe_thumbnails_dir / thumbnail_name
    if thumbnail_path.exists():
        return f"/uploads/recipe_thumbnails/{thumbnail_name}"

    try:
        from PIL import Image, ImageOps
    except ImportError:
        return f"/uploads/{candidates[0]}"

    best = None
    for filename in candidates:
        image_path = uploads_dir / filename
        if not image_path.exists():
            continue
        try:
            with Image.open(image_path) as opened:
                image = ImageOps.exif_transpose(opened).convert("RGB")
                score = _recipe_thumbnail_score(image)
                if filename == primary_thumbnail:
                    score += 10
                if not best or score > best[0]:
                    best = (score, filename, image.copy())
        except Exception:
            continue
    if not best:
        return f"/uploads/{candidates[0]}"

    crop = _crop_food_photo_region(best[2])
    crop.thumbnail((1000, 650))
    crop.save(thumbnail_path, "JPEG", quality=88, optimize=True)
    try:
        cloud_persistence.sync_upload_file(thumbnail_path)
    except Exception as exc:
        print(f"Warning: thumbnail cloud sync failed - {exc}")
    return f"/uploads/recipe_thumbnails/{thumbnail_name}"

def extract_recipe_pdf_preview(pdf_path, output_dir):
    """Extract a useful recipe photo from a PDF, falling back to a page preview."""
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PDF import requires PyMuPDF. Install requirements and try again.") from exc

    document = fitz.open(str(pdf_path))
    try:
        best = None
        for page_index in range(len(document)):
            page = document[page_index]
            for image_index, image_ref in enumerate(page.get_images(full=True)):
                xref = image_ref[0]
                try:
                    extracted = document.extract_image(xref)
                except Exception:
                    continue
                image_bytes = extracted.get("image")
                extension = (extracted.get("ext") or "png").lower()
                width = int(extracted.get("width") or 0)
                height = int(extracted.get("height") or 0)
                if not image_bytes or width < 80 or height < 80:
                    continue
                score = width * height
                if not best or score > best["score"]:
                    best = {
                        "score": score,
                        "bytes": image_bytes,
                        "extension": "jpg" if extension == "jpeg" else extension,
                        "page": page_index + 1,
                        "image_index": image_index + 1,
                    }
        if best:
            output_name = f"{uuid.uuid4().hex}.{best['extension']}"
            output_path = output_dir / output_name
            output_path.write_bytes(best["bytes"])
            return output_name, f"PDF photo p{best['page']}"

        if len(document) == 0:
            return "", ""
        page = document[0]
        pixmap = page.get_pixmap(matrix=fitz.Matrix(1.7, 1.7), alpha=False)
        output_name = f"{uuid.uuid4().hex}.png"
        output_path = output_dir / output_name
        pixmap.save(str(output_path))
        return output_name, "PDF page preview"
    finally:
        document.close()

def prepare_recipe_complete_meals(meals):
    """Parse complete-meal quality notes for rendering."""
    prepared = []
    new_cutoff = datetime.utcnow() - timedelta(days=14)
    for meal in meals:
        meal_data = dict(meal)
        try:
            meal_data["quality_notes"] = json.loads(meal_data.get("quality_notes_json") or "[]")
        except json.JSONDecodeError:
            meal_data["quality_notes"] = ["Quality notes could not be parsed."]
        meal_data["thumbnail_url"] = build_recipe_thumbnail_url(meal_data)
        title = (meal_data.get("title") or "").strip().lower()
        meal_data["is_placeholder"] = (
            meal_data.get("source_kind", "card") == "card"
            and title.startswith("recipe pair ")
            and not (meal_data.get("ingredients_text") or "").strip()
            and not (meal_data.get("instructions_text") or "").strip()
        )
        meal_data["display_title"] = meal_data.get("edited_title") or meal_data.get("title") or ""
        meal_data["display_ingredients_text"] = meal_data.get("edited_ingredients_text") or meal_data.get("ingredients_text") or ""
        meal_data["display_instructions_text"] = meal_data.get("edited_instructions_text") or meal_data.get("instructions_text") or ""
        meal_data["has_dieter_edits"] = bool(
            meal_data.get("edited_title")
            or meal_data.get("edited_ingredients_text")
            or meal_data.get("edited_instructions_text")
        )
        meal_data["visibility"] = meal_data.get("visibility") or "shared"
        meal_data["is_owner"] = bool(meal_data.get("is_owner"))
        meal_data["is_favorite"] = bool(meal_data.get("is_favorite"))
        meal_data["catalogue_label"] = "Private" if meal_data["visibility"] == "private" else "Shared"
        try:
            created_at = datetime.fromisoformat((meal_data.get("created_at") or "").replace("Z", "+00:00"))
            meal_data["is_new"] = created_at.replace(tzinfo=None) >= new_cutoff
        except ValueError:
            meal_data["is_new"] = False
        prepared.append(meal_data)
    return prepared

def filter_public_complete_meals(meals):
    """Show only properly imported meals in the user-facing recipe library."""
    return [
        meal for meal in meals
        if not meal.get("is_placeholder") and meal.get("status") == "ready"
    ]

def prepare_recipe_components(components):
    """Parse structured component ingredients for rendering."""
    prepared = []
    for component in components:
        component_data = dict(component)
        try:
            structured_ingredients = json.loads(component_data.get("structured_ingredients_json") or "[]")
        except json.JSONDecodeError:
            structured_ingredients = []
        component_data["structured_ingredients"] = structured_ingredients
        component_data["structured_ingredients_json_attr"] = json.dumps(structured_ingredients)
        component_data["display_title"] = component_data.get("edited_title") or component_data.get("title") or ""
        component_data["display_ingredients_text"] = component_data.get("edited_ingredients_text") or component_data.get("ingredients_text") or ""
        component_data["display_instructions_text"] = component_data.get("edited_instructions_text") or component_data.get("instructions_text") or ""
        component_data["has_dieter_edits"] = bool(
            component_data.get("edited_title")
            or component_data.get("edited_ingredients_text")
            or component_data.get("edited_instructions_text")
        )
        prepared.append(component_data)
    return prepared

def prepare_recipe_change_log(changes):
    """Parse stored recipe change-log JSON for rendering/API responses."""
    prepared = []
    for change in changes:
        change_data = dict(change)
        for source_key, fallback in [
            ("changed_fields_json", []),
            ("before_json", {}),
            ("after_json", {}),
        ]:
            target_key = source_key.replace("_json", "")
            try:
                change_data[target_key] = json.loads(change_data.get(source_key) or json.dumps(fallback))
            except json.JSONDecodeError:
                change_data[target_key] = fallback
        prepared.append(change_data)
    return prepared

def prepare_meal_plan_items(items):
    """Parse stored pending/cooked meal-plan entries."""
    prepared = []
    for item in items:
        item_data = dict(item)
        try:
            item_data["component_ids"] = json.loads(item_data.get("component_ids_json") or "[]")
        except json.JSONDecodeError:
            item_data["component_ids"] = []
        item_data["source_url"] = ""
        if item_data.get("source_kind") == "complete_meal" and item_data.get("source_id"):
            item_data["source_url"] = f"/apps/recipes/meals/{item_data['source_id']}"
        prepared.append(item_data)
    return prepared

def prepare_grocery_lists(lists):
    """Parse stored grocery-list records for rendering."""
    prepared = []
    for grocery_list in lists:
        list_data = dict(grocery_list)
        try:
            list_data["meal_plan_item_ids"] = json.loads(list_data.get("meal_plan_item_ids_json") or "[]")
        except json.JSONDecodeError:
            list_data["meal_plan_item_ids"] = []
        try:
            list_data["items"] = json.loads(list_data.get("items_json") or "[]")
        except json.JSONDecodeError:
            list_data["items"] = []
        for index, item in enumerate(list_data["items"]):
            if isinstance(item, dict):
                item["item_index"] = index
                item["status"] = item.get("status") or "need"
        prepared.append(list_data)
    return prepared

def cookable_meal_plan_items_for_grocery_list(grocery_list, linked_items_by_id=None):
    """Return linked meal-plan items that represent recipes/meals needing cooking."""
    if linked_items_by_id is None:
        linked_items = prepare_meal_plan_items(db.get_recipe_meal_plan_items(None, 250))
        linked_items_by_id = {item["id"]: item for item in linked_items}
    cookable = []
    for item_id in grocery_list.get("meal_plan_item_ids", []):
        item = linked_items_by_id.get(item_id)
        if item and item.get("source_kind") != "manual_item":
            cookable.append(item)
    return cookable

def annotate_grocery_list_cook_counts(grocery_lists):
    """Attach recipe-to-cook counts for list summaries."""
    linked_items = prepare_meal_plan_items(db.get_recipe_meal_plan_items(None, 250))
    linked_items_by_id = {item["id"]: item for item in linked_items}
    for grocery_list in grocery_lists:
        cookable = cookable_meal_plan_items_for_grocery_list(grocery_list, linked_items_by_id)
        grocery_list["cookable_meal_plan_items"] = cookable
        grocery_list["recipes_to_cook_count"] = len([item for item in cookable if item.get("status") != "cooked"])
        grocery_list["recipes_total_count"] = len(cookable)
    return grocery_lists

def refresh_grocery_list_completion(list_id):
    """Mark a grocery list done when all linked cookable recipes are cooked."""
    row = dict_from_row(db.get_recipe_grocery_list(list_id))
    if not row:
        return None
    grocery_list = prepare_grocery_lists([row])[0]
    cookable = cookable_meal_plan_items_for_grocery_list(grocery_list)
    if cookable and all(item.get("status") == "cooked" for item in cookable):
        if grocery_list.get("status") != "done":
            db.update_recipe_grocery_list_status(list_id, "done")
            grocery_list["status"] = "done"
    elif grocery_list.get("status") == "done" and any(item.get("status") != "cooked" for item in cookable):
        db.update_recipe_grocery_list_status(list_id, "active")
        grocery_list["status"] = "active"
    grocery_list["cookable_meal_plan_items"] = cookable
    grocery_list["recipes_to_cook_count"] = len([item for item in cookable if item.get("status") != "cooked"])
    grocery_list["recipes_total_count"] = len(cookable)
    return grocery_list

def split_ingredient_lines(ingredients_text):
    """Split recipe ingredient text into useful grocery-line candidates."""
    return [
        re.sub(r"^[-*]\s*", "", line.strip())
        for line in (ingredients_text or "").splitlines()
        if line.strip()
    ]

BAKING_SECTION_LABELS = {
    "dough": "Dough",
    "filling": "Filling",
    "icing": "Icing",
    "crust": "Crust",
    "sauce": "Sauce",
    "pesto": "Pesto",
    "chicken": "Chicken",
    "toppings": "Toppings",
}

BAKING_SECTION_ALIASES = {
    "dough": "dough",
    "filling": "filling",
    "fillings": "filling",
    "cinnamon filling": "filling",
    "icing": "icing",
    "frosting": "icing",
    "glaze": "icing",
    "crust": "crust",
    "pizza crust": "crust",
    "sauce": "sauce",
    "pizza sauce": "sauce",
    "pesto": "pesto",
    "pesto sauce": "pesto",
    "chicken": "chicken",
    "seared chicken": "chicken",
    "topping": "toppings",
    "toppings": "toppings",
}

BAKING_INGREDIENT_SECTION_TERMS = {
    "dough": [
        "starter", "levain", "flour", "bread flour", "all-purpose", "water",
        "milk", "yeast", "salt", "egg", "eggs", "dough", "tangzhong",
    ],
    "filling": [
        "brown sugar", "cinnamon", "nutmeg", "cardamom", "cocoa", "jam",
        "preserves", "raisins", "nuts", "pecans", "walnuts", "filling",
    ],
    "icing": [
        "powdered sugar", "confectioners", "cream cheese", "vanilla",
        "butter", "heavy cream", "icing", "frosting", "glaze", "lemon juice",
        "maple syrup", "milk",
    ],
    "crust": [
        "00 flour", "bread flour", "cornmeal", "crust", "dough", "flour",
        "olive oil", "pizza dough", "semolina", "yeast",
    ],
    "sauce": [
        "marinara", "pizza sauce", "sauce", "tomato", "tomatoes",
    ],
    "pesto": [
        "basil", "garlic", "parmesan", "pine nut", "pine nuts", "pesto",
    ],
    "chicken": [
        "chicken", "chicken breast", "chicken thighs",
    ],
    "toppings": [
        "cheese", "mozzarella", "onion", "pepper", "topping", "toppings",
    ],
}

BAKING_INSTRUCTION_SECTION_TERMS = {
    "dough": [
        "autolyse", "bulk", "dough", "ferment", "flour", "fold", "knead",
        "levain", "proof", "rise", "starter", "stretch", "water",
    ],
    "filling": [
        "brown sugar", "cardamom", "cinnamon", "filling", "nutmeg", "roll up",
        "spread", "sprinkle", "swirl",
    ],
    "icing": [
        "beat", "combine", "cream cheese", "drizzle", "frost", "frosting",
        "glaze", "icing", "powdered sugar", "smooth", "thin", "vanilla",
        "whisk",
    ],
    "crust": [
        "bake", "crust", "dough", "flour", "knead", "parbake", "pizza stone",
        "proof", "rise", "shape", "stretch",
    ],
    "sauce": [
        "sauce", "simmer", "tomato",
    ],
    "pesto": [
        "basil", "blend", "garlic", "parmesan", "pesto", "pine nut", "process",
    ],
    "chicken": [
        "chicken", "cook", "saute", "sear", "slice",
    ],
    "toppings": [
        "assemble", "cheese", "mozzarella", "scatter", "sprinkle", "top",
        "topping", "toppings",
    ],
}

def slugify_baking_section_key(label):
    """Create a stable key for a recipe-provided Bake Mode section."""
    key = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")
    return key or "section"

def baking_section_label_for_key(key):
    """Return a human label for a known or recipe-provided Bake Mode section."""
    return BAKING_SECTION_LABELS.get(key) or (key or "Section").replace("-", " ").title()

def parse_baking_section_heading(line):
    """Return a Bake Mode section when a line is likely a section heading."""
    raw_heading = re.sub(r"^#+\s*", "", line or "").strip()
    if not raw_heading:
        return None
    has_heading_marker = bool(re.match(r"^\s*#+", line or "") or re.search(r"[:\-]\s*$", raw_heading))
    heading = re.sub(r"[:\-]+$", "", raw_heading).strip()
    heading = re.sub(r"^(for|make|the)\s+", "", heading, flags=re.IGNORECASE).strip()
    normalized = heading.lower()
    alias_key = BAKING_SECTION_ALIASES.get(normalized)
    if alias_key:
        return {"key": alias_key, "label": baking_section_label_for_key(alias_key)}
    if not has_heading_marker and not re.match(r"^(for|make|the)\s+", raw_heading, flags=re.IGNORECASE):
        return None
    if len(heading.split()) > 5:
        return None
    key = slugify_baking_section_key(heading)
    return {"key": key, "label": baking_section_label_for_key(key)}

def normalize_baking_section_heading(line):
    """Return a Bake Mode section key when a line looks like a heading."""
    heading = parse_baking_section_heading(line)
    return heading["key"] if heading else None

def parse_baking_ingredient_sections(ingredients_text):
    """Split recipe ingredients into touch-friendly baking sections."""
    sections_by_key = {}
    section_order = []
    other_items = []
    current_key = None

    def ensure_section(key, label=None):
        if key not in sections_by_key:
            sections_by_key[key] = {
                "key": key,
                "label": label or baking_section_label_for_key(key),
                "items": [],
            }
            section_order.append(key)
        return sections_by_key[key]

    source_lines = []
    for raw_line in (ingredients_text or "").splitlines():
        if "|" in raw_line and not normalize_baking_section_heading(raw_line):
            source_lines.extend(part.strip() for part in raw_line.split("|"))
        else:
            source_lines.append(raw_line)

    for raw_line in source_lines:
        line = raw_line.strip()
        if not line:
            continue
        heading = parse_baking_section_heading(line)
        if heading:
            current_key = heading["key"]
            ensure_section(current_key, heading["label"])
            continue
        item = re.sub(r"^[-*]\s*", "", line).strip()
        if not item:
            continue
        if current_key:
            ensure_section(current_key)["items"].append(item)
        else:
            other_items.append(item)

    sections = [sections_by_key[key] for key in section_order if sections_by_key[key]["items"]]
    if not sections and other_items:
        categorized = {}
        categorized_order = []

        def ensure_categorized(key):
            if key not in categorized:
                categorized[key] = {
                    "key": key,
                    "label": baking_section_label_for_key(key),
                    "items": [],
                }
                categorized_order.append(key)
            return categorized[key]

        uncategorized = []
        for item in other_items:
            lowered = item.lower()
            target_key = ""
            scored_keys = []
            for key, terms in BAKING_INGREDIENT_SECTION_TERMS.items():
                score = sum(len(term) for term in terms if term in lowered)
                if score:
                    scored_keys.append((score, key))
            if scored_keys:
                scored_keys.sort(key=lambda item: item[0], reverse=True)
                target_key = scored_keys[0][1]
            if target_key:
                ensure_categorized(target_key)["items"].append(item)
            else:
                uncategorized.append(item)
        if any(section["items"] for section in categorized.values()):
            sections = [categorized[key] for key in categorized_order if categorized[key]["items"]]
            other_items = uncategorized
    if other_items:
        sections.append({"key": "general", "label": "Ingredients", "items": other_items})
    return sections

def parse_baking_instruction_sections(instructions_text, ingredient_sections=None):
    """Split recipe instructions into the baking sections they most likely support."""
    section_sources = ingredient_sections or [
        {"key": key, "label": label, "items": []}
        for key, label in BAKING_SECTION_LABELS.items()
    ]
    sections_by_key = {section["key"]: [] for section in section_sources}
    section_order = [section["key"] for section in section_sources]
    term_map = {}
    for section in section_sources:
        key = section["key"]
        label = section.get("label") or baking_section_label_for_key(key)
        terms = set(BAKING_INSTRUCTION_SECTION_TERMS.get(key, []))
        terms.add(label.lower())
        terms.update(word for word in re.findall(r"[a-z]{4,}", label.lower()))
        for ingredient in section.get("items", [])[:8]:
            ingredient_text = re.sub(
                r"\b\d+(?:[./]\d+)?|\b(cup|cups|tbsp|tablespoons?|tsp|teaspoons?|oz|ounces?|lb|lbs|g|grams?)\b",
                " ",
                ingredient.lower(),
            )
            terms.update(word for word in re.findall(r"[a-z]{4,}", ingredient_text)[:3])
        term_map[key] = terms
    current_key = None
    last_target_key = None
    continuation_steps_remaining = 0
    source_lines = []
    for raw_line in (instructions_text or "").splitlines():
        pieces = re.split(r"\s+(?=\d+[.)]\s+)", raw_line.strip())
        source_lines.extend(piece for piece in pieces if piece.strip())
    for raw_line in source_lines:
        line = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", raw_line).strip()
        if not line:
            continue
        heading_key = normalize_baking_section_heading(line)
        if heading_key:
            current_key = heading_key if heading_key in sections_by_key else None
            continue
        lowered = line.lower()
        target_key = current_key
        scored_keys = []
        for key, terms in term_map.items():
            score = sum(1 for term in terms if term in lowered)
            if score:
                scored_keys.append((score, key))
        if scored_keys:
            best_score = max(score for score, _ in scored_keys)
            best_keys = [key for score, key in scored_keys if score == best_score]
            if current_key in best_keys:
                target_key = current_key
            else:
                target_key = min(best_keys, key=lambda key: section_order.index(key))
        elif last_target_key and continuation_steps_remaining > 0 and re.match(
            r"^(add|beat|combine|drizzle|mix|pour|spread|stir|thin|whisk)\b",
            lowered,
        ):
            target_key = last_target_key
        if target_key and target_key in sections_by_key:
            sections_by_key[target_key].append(line)
            last_target_key = target_key
            continuation_steps_remaining = 2 if target_key in {"icing", "glaze", "frosting"} else 1
        elif continuation_steps_remaining > 0:
            continuation_steps_remaining -= 1
    return sections_by_key

def attach_baking_instructions_to_sections(ingredient_sections, instructions_text):
    """Add section-specific instruction snippets to ingredient sections for Bake Mode."""
    instructions_by_key = parse_baking_instruction_sections(instructions_text, ingredient_sections)
    return [
        {
            **section,
            "instructions": instructions_by_key.get(section["key"], []),
        }
        for section in ingredient_sections
    ]

def format_baking_ingredient_sections(section_values):
    """Render saved ingredient sections in a predictable edited-recipe format."""
    blocks = []
    for key, label in BAKING_SECTION_LABELS.items():
        lines = [
            re.sub(r"^[-*]\s*", "", line.strip())
            for line in (section_values.get(key) or "").splitlines()
            if line.strip()
        ]
        if lines:
            blocks.append(f"{label}:\n" + "\n".join(f"- {line}" for line in lines))
    return "\n\n".join(blocks)

def normalize_grocery_name(ingredient):
    """Normalize an ingredient line enough to combine repeats."""
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        re.sub(
            r"\b(cup|cups|tbsp|tablespoons?|tsp|teaspoons?|oz|ounces?|lb|lbs|pounds?|g|grams?|cloves?|small|medium|large|pinch|packet|packets|can|cans)\b",
            "",
            re.sub(
                r"^[\d\s./]+",
                "",
                re.sub(
                    r"\([^)]*\)",
                    "",
                    (ingredient or "").lower(),
                ),
            ),
        ),
    ).strip()

def extract_grocery_quantity(ingredient):
    """Pull a compact quantity phrase from the start of an ingredient line."""
    match = re.match(
        r"^([\d\s./]+(?:cup|cups|tbsp|tablespoons?|tsp|teaspoons?|oz|ounces?|lb|lbs|pounds?|g|grams?|cloves?|pinch|packet|packets|can|cans)?)",
        ingredient or "",
        flags=re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""

def title_case_grocery_name(ingredient):
    """Format normalized grocery names for display."""
    return " ".join(word.capitalize() for word in (ingredient or "").split())

BAD_GROCERY_OPTION_PHRASES = [
    "calories",
    "contains",
    "custom plate option",
    "if modified meal",
    "optional modification",
    "optional modifications",
    "optional protein",
    "optional swap",
    "pantry needed",
    "what we send",
    "what you ll need",
    "what you'll need",
    "you ll also need",
    "you'll also need",
    "pasta cooking water",
    "reserved pasta cooking water",
]

GROCERY_OPTION_NAME_FIXES = {
    "bunch thyme": "thyme",
    "cr me fra che": "creme fraiche",
    "frank s hot sauce": "franks hot sauce",
    "frank s seasoning": "franks seasoning",
}

def clean_available_grocery_candidate(candidate):
    """Return a dropdown-safe ingredient name, or blank for OCR/instruction noise."""
    text = re.sub(r"\([^)]*\)", " ", candidate or "")
    text = re.sub(r"[\n\r]+", " ", text)
    text = re.sub(r"\b\d+\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    lowered = text.lower()
    if not lowered:
        return ""
    if any(phrase in lowered for phrase in BAD_GROCERY_OPTION_PHRASES):
        return ""
    if re.search(r"\bor\b", lowered):
        return ""

    normalized = normalize_grocery_name(text)
    if not normalized:
        return ""
    normalized = GROCERY_OPTION_NAME_FIXES.get(normalized, normalized)
    words = normalized.split()
    if len(words) > 5:
        return ""
    return title_case_grocery_name(normalized)

GROCERY_CATEGORY_RULES = [
    ("meat_seafood", "Meat & Seafood", [
        "chicken", "pork", "beef", "steak", "sausage", "bacon", "shrimp",
        "turkey", "salmon", "fish", "tilapia", "cod", "meat", "prosciutto",
    ]),
    ("produce", "Produce", [
        "apple", "avocado", "broccoli", "brussels", "carrot", "celery",
        "cucumber", "garlic", "ginger", "green bean", "kale", "lemon",
        "lime", "mushroom", "onion", "pepper", "potato", "scallion",
        "shallot", "spinach", "sweet potato", "tomato", "zucchini",
    ]),
    ("dairy", "Dairy", [
        "butter", "cheddar", "cheese", "cream", "creme", "feta", "milk",
        "mozzarella", "parmesan", "sour cream", "yogurt",
    ]),
    ("canned_goods", "Canned & Jarred", [
        "beans", "cannellini", "chickpea", "tomato paste", "diced tomato",
        "crushed tomato", "jam", "stock concentrate", "concentrate",
    ]),
    ("sauces_condiments", "Sauces & Condiments", [
        "balsamic", "bbq", "dijon", "honey", "hot sauce", "ketchup",
        "mayo", "mayonnaise", "mustard", "soy sauce", "vinegar", "worcestershire",
    ]),
    ("seasonings", "Seasonings", [
        "black pepper", "chili powder", "cumin", "curry", "garlic powder",
        "italian seasoning", "oregano", "paprika", "pepper", "salt",
        "seasoning", "spice", "thyme", "turmeric",
    ]),
    ("grains_pasta", "Grains, Pasta & Bread", [
        "bread", "breadcrumbs", "bun", "ciabatta", "couscous", "flour", "noodle",
        "panko", "pasta", "rice", "tortilla",
    ]),
    ("pantry", "Pantry", [
        "cornstarch", "oil", "olive oil", "sugar",
    ]),
]

def grocery_category_for_item(item):
    """Assign a grocery item to a store-friendly shopping category."""
    if item.get("category"):
        category = item.get("category")
        for category_key, category_label, _ in GROCERY_CATEGORY_RULES:
            if category == category_key:
                return category_key, category_label
    name = (item.get("name") or "").lower()
    for category_key, category_label, terms in GROCERY_CATEGORY_RULES:
        if any(term in name for term in terms):
            return category_key, category_label
    return "other", "Other"

def group_grocery_items_by_category(items):
    """Group grocery items by shopping category while preserving category order."""
    grouped = {
        category_key: {
            "key": category_key,
            "label": category_label,
            "items": [],
        }
        for category_key, category_label, _ in GROCERY_CATEGORY_RULES
    }
    grouped["other"] = {"key": "other", "label": "Other", "items": []}

    for item in items:
        category_key, category_label = grocery_category_for_item(item)
        grouped.setdefault(category_key, {"key": category_key, "label": category_label, "items": []})
        grouped[category_key]["items"].append(item)

    return [
        section for section in grouped.values()
        if section["items"]
    ]

def build_available_grocery_items(complete_meals, components):
    """Build a searchable ingredient list from imported recipes and components."""
    available_items = set()

    def add_candidate(candidate):
        cleaned = clean_available_grocery_candidate(candidate)
        if cleaned:
            available_items.add(cleaned)

    for component in components:
        for ingredient in component.get("structured_ingredients") or []:
            add_candidate(ingredient.get("name") or ingredient.get("source_text") or "")
        for line in split_ingredient_lines(component.get("display_ingredients_text") or ""):
            add_candidate(line)

    for meal in complete_meals:
        if meal.get("status") != "ready" or meal.get("is_placeholder"):
            continue
        for line in split_ingredient_lines(meal.get("display_ingredients_text") or ""):
            add_candidate(line)

    return sorted(available_items)

def build_available_grocery_options(available_items):
    """Attach grocery categories to available ingredients for UI filtering."""
    options = []
    for item in available_items:
        category_key, category_label = grocery_category_for_item({"name": item})
        options.append({
            "name": item,
            "category": category_key,
            "category_label": category_label,
        })
    return options

def add_grocery_item(items_by_key, name, quantity="", note="", source="", category=""):
    """Accumulate grocery item quantities and recipe sources."""
    key = normalize_grocery_name(name) or (name or "").strip().lower()
    if not key:
        return
    if key not in items_by_key:
        items_by_key[key] = {
            "name": title_case_grocery_name(key),
            "quantities": [],
            "notes": [],
            "sources": [],
        }
    item = items_by_key[key]
    if category and not item.get("category"):
        item["category"] = category
    if quantity and quantity not in item["quantities"]:
        item["quantities"].append(quantity)
    if note and note not in item["notes"]:
        item["notes"].append(note)
    if source and source not in item["sources"]:
        item["sources"].append(source)

def grocery_items_from_component(component, source_title):
    """Build grocery entries from a component using structured amounts when available."""
    entries = []
    structured = component.get("structured_ingredients") or []
    if structured:
        for ingredient in structured:
            quantity = " ".join(
                value for value in [ingredient.get("amount", ""), ingredient.get("unit", "")]
                if value
            ).strip()
            entries.append({
                "name": ingredient.get("name") or ingredient.get("source_text") or "",
                "quantity": quantity,
                "note": ingredient.get("preparation") or "",
                "source": source_title,
            })
        return entries

    for line in split_ingredient_lines(component.get("display_ingredients_text") or component.get("ingredients_text") or ""):
        entries.append({
            "name": normalize_grocery_name(line) or line,
            "quantity": extract_grocery_quantity(line),
            "note": "",
            "source": source_title,
        })
    return entries

def build_grocery_items_for_plan(meal_plan_items):
    """Create a consolidated grocery list from pending meal-plan items."""
    items_by_key = {}
    for plan_item in meal_plan_items:
        if plan_item.get("source_kind") == "manual_item":
            for manual_item in plan_item.get("component_ids") or []:
                if not isinstance(manual_item, dict):
                    continue
                add_grocery_item(
                    items_by_key,
                    manual_item.get("name", ""),
                    manual_item.get("quantity", ""),
                    manual_item.get("note", ""),
                    plan_item.get("title") or "Manual item",
                    manual_item.get("category", ""),
                )
        elif plan_item.get("source_kind") == "complete_meal" and plan_item.get("source_id"):
            meal = dict_from_row(db.get_recipe_complete_meal(plan_item["source_id"]))
            if not meal:
                continue
            meal = prepare_recipe_complete_meals([meal])[0]
            source_title = meal.get("display_title") or plan_item.get("title") or "Meal"
            for line in split_ingredient_lines(meal.get("display_ingredients_text") or ""):
                add_grocery_item(
                    items_by_key,
                    normalize_grocery_name(line) or line,
                    extract_grocery_quantity(line),
                    "",
                    source_title,
                )
        else:
            for component_id in plan_item.get("component_ids") or []:
                component = dict_from_row(db.get_recipe_component(component_id))
                if not component:
                    continue
                component = prepare_recipe_components([component])[0]
                source_title = component.get("display_title") or plan_item.get("title") or "Meal part"
                for entry in grocery_items_from_component(component, source_title):
                    add_grocery_item(
                        items_by_key,
                        entry["name"],
                        entry["quantity"],
                        entry["note"],
                        entry["source"],
                    )
    return sorted(items_by_key.values(), key=lambda item: item["name"])

def parse_recipe_app_chat_content(content):
    """Extract page and user message from the recipe app drawer payload."""
    if "Recipe app chat request." not in (content or ""):
        return None
    page_match = re.search(r"URL:\s*(.+)", content or "")
    message_match = re.search(r"User message:\s*(.*)", content or "", flags=re.DOTALL)
    return {
        "page_url": page_match.group(1).strip() if page_match else "",
        "user_message": message_match.group(1).strip() if message_match else content.strip(),
    }

def meaningful_recipe_terms(text):
    """Pick useful query words for recipe lookup."""
    stopwords = {
        "about", "again", "could", "dieter", "does", "have", "make", "recipe",
        "recipes", "should", "suggest", "there", "use", "what", "which", "with",
        "your", "mine", "my", "the", "and", "for", "that", "this",
    }
    terms = []
    for term in re.findall(r"[a-zA-Z][a-zA-Z0-9]+", text.lower()):
        if len(term) < 3 or term in stopwords:
            continue
        if term not in terms:
            terms.append(term)
    return terms[:8]

def score_recipe_record(record, terms):
    """Score a meal/component against recipe chat query terms."""
    searchable = " ".join(
        str(record.get(key) or "")
        for key in [
            "display_title",
            "title",
            "component_type",
            "display_ingredients_text",
            "display_instructions_text",
            "ingredients_text",
            "instructions_text",
        ]
    ).lower()
    return sum(2 if term in (record.get("display_title") or record.get("title") or "").lower() else 1 for term in terms if term in searchable)

def build_recipe_chat_context(user_message, page_url=""):
    """Build compact recipe-library context for Dieter chat."""
    recipe_app = get_recipe_app_context()
    meals = filter_public_complete_meals(recipe_app.get("complete_meals", []))
    components = recipe_app.get("components", [])
    terms = meaningful_recipe_terms(user_message)

    scored_meals = [
        (score_recipe_record(meal, terms), meal)
        for meal in meals
    ]
    scored_components = [
        (score_recipe_record(component, terms), component)
        for component in components
    ]
    matched_meals = [meal for score, meal in sorted(scored_meals, key=lambda entry: entry[0], reverse=True) if score > 0]
    matched_components = [component for score, component in sorted(scored_components, key=lambda entry: entry[0], reverse=True) if score > 0]

    if not terms:
        matched_meals = meals[:12]
        matched_components = components[:12]

    lines = [
        f"Current recipe app page: {page_url or 'unknown'}",
        f"User recipe-library query terms: {', '.join(terms) if terms else 'none'}",
        "",
        "Use these records as the user's actual recipe library. If asked 'my recipes', answer from these records only.",
    ]

    lines.append("\nMatching complete meals:")
    for meal in matched_meals[:16]:
        ingredients = " ".join(split_ingredient_lines(meal.get("display_ingredients_text") or ""))[:450]
        lines.append(f"- id {meal.get('id')}: {meal.get('display_title') or meal.get('title')} | ingredients: {ingredients}")
    if not matched_meals:
        lines.append("- No complete meal matches found.")

    lines.append("\nMatching meal components:")
    for component in matched_components[:20]:
        ingredients = " ".join(split_ingredient_lines(component.get("display_ingredients_text") or ""))[:350]
        lines.append(
            f"- id {component.get('id')}: {component.get('display_title') or component.get('title')} "
            f"({component.get('component_type')}) | ingredients: {ingredients}"
        )
    if not matched_components:
        lines.append("- No component matches found.")

    lines.append("\nAvailable complete meal titles:")
    for meal in meals[:40]:
        lines.append(f"- {meal.get('display_title') or meal.get('title')}")

    return "\n".join(lines)

RECIPE_CHAT_SYSTEM_PROMPT = """You are Dieter, the user's recipe app assistant.

Answer using the provided recipe-library context. When the user asks about "my recipes",
recommend actual recipes from the context by title. Do not invent unavailable recipes.
If the context has no match, say that clearly and suggest the closest available match.
Keep answers brief and practical.
Use plain text only. Do not use Markdown emphasis such as **bold** or *italics*.
Simple dash bullets are okay.
"""

def recipe_chat_response(user_message, page_url="", conversation_history=None):
    """Answer recipe app questions with recipe-library awareness."""
    recent_user_context = " ".join(
        entry.get("content", "")
        for entry in list(conversation_history or [])[-6:]
        if entry.get("role") == "user"
    )
    lookup_text = f"{recent_user_context} {user_message}".strip()
    context = build_recipe_chat_context(lookup_text, page_url)
    prompt = f"[Recipe Library Context]\n{context}\n\nUser message:\n{user_message}"
    if llm_service:
        messages = list(conversation_history or [])[-8:]
        messages.append({"role": "user", "content": prompt})
        return llm_service.provider.chat(messages, RECIPE_CHAT_SYSTEM_PROMPT)

    return "I can see the recipe app context, but the model is not configured, so I cannot make a recommendation right now."

def group_recipe_components(components):
    """Group recipe components into stable library sections."""
    section_labels = [
        ("meat", "Protein"),
        ("carb", "Carb"),
        ("vegetable", "Vegetables"),
        ("soup", "Soups"),
        ("sauce", "Sauces"),
        ("other", "Other"),
    ]
    sections = []
    for component_type, label in section_labels:
        items = [component for component in components if component.get("component_type") == component_type]
        if items:
            sections.append({
                "type": component_type,
                "label": label,
                "components": items,
            })
    uncategorized = [
        component for component in components
        if component.get("component_type") not in {section_type for section_type, _ in section_labels}
    ]
    if uncategorized:
        sections.append({
            "type": "other",
            "label": "Other",
            "components": uncategorized,
        })
    return sections

def build_saved_meal_text_from_components(components):
    """Create readable complete-meal text from selected components."""
    ingredients_sections = []
    instructions_sections = []
    for component in components:
        component_type = component.get("component_type", "other").replace("_", " ").title()
        title = component.get("title") or "Meal part"
        structured = component.get("structured_ingredients") or []
        if structured:
            ingredient_lines = []
            for ingredient in structured:
                quantity = " ".join(
                    value for value in [ingredient.get("amount", ""), ingredient.get("unit", "")]
                    if value
                ).strip()
                prep = f" - {ingredient['preparation']}" if ingredient.get("preparation") else ""
                ingredient_lines.append(f"{quantity + ' ' if quantity else ''}{ingredient.get('name', '')}{prep}".strip())
            ingredients_body = "\n".join(line for line in ingredient_lines if line)
        else:
            ingredients_body = component.get("ingredients_text") or ""
        if ingredients_body:
            ingredients_sections.append(f"{component_type}: {title}\n{ingredients_body}")
        if component.get("instructions_text"):
            instructions_sections.append(f"{component_type}: {title}\n{component['instructions_text']}")
    return "\n\n".join(ingredients_sections), "\n\n".join(instructions_sections)

def parse_recipe_target_from_url(page_url):
    """Resolve a recipe chat URL into a target kind and id."""
    match = re.search(r"/apps/recipes/meals/(\d+)", page_url or "")
    if match:
        return "meal", int(match.group(1))
    match = re.search(r"/apps/recipes/components/(\d+)", page_url or "")
    if match:
        return "component", int(match.group(1))
    return None, None

def recipe_record_for_edit(recipe_kind, recipe_id):
    """Load the current editable recipe record."""
    if recipe_kind == "meal":
        record = dict_from_row(db.get_recipe_complete_meal(recipe_id))
        if not record:
            return None
        return prepare_recipe_complete_meals([record])[0]
    if recipe_kind == "component":
        record = dict_from_row(db.get_recipe_component(recipe_id))
        if not record:
            return None
        return prepare_recipe_components([record])[0]
    return None

def extract_json_object(text):
    """Parse a model response that should be JSON, tolerating extra text."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text or "", flags=re.DOTALL)
    if not match:
        raise ValueError("No JSON object found in model response")
    return json.loads(match.group(0))

def build_recipe_edit_prompt(recipe_kind, recipe, user_message):
    """Create the user prompt for a structured Dieter recipe edit."""
    return f"""Recipe kind: {recipe_kind}
Title:
{recipe.get('display_title') or recipe.get('title') or ''}

Ingredients:
{recipe.get('display_ingredients_text') or ''}

Instructions:
{recipe.get('display_instructions_text') or ''}

User feedback/change request:
{user_message}

Return only JSON with this exact shape:
{{
  "apply_change": true,
  "summary": "",
  "title": null,
  "ingredients_text": null,
  "instructions_text": null,
  "changed_fields": [],
  "assistant_message": ""
}}
"""

RECIPE_EDIT_SYSTEM_PROMPT = """You are Dieter, a recipe editing assistant.

You may edit a recipe based on cooking feedback or a requested change.
Preserve useful existing recipe content. Apply small, practical edits.

Good edits include:
- Adding or clarifying oven order and timing for complete meals.
- Adding component-specific timing notes.
- Adjusting seasoning amounts or adding notes from cooking feedback.
- Clarifying sequence, doneness cues, rests, sauce reduction, or prep order.

Rules:
- Return only valid JSON.
- If the request is only a question and no recipe text should change, set
  apply_change to false and answer in assistant_message.
- For fields that should not change, use null.
- If a field changes, return the full replacement text for that field, not a diff.
- Keep changed_fields limited to title, ingredients_text, instructions_text.
- Do not invent unrelated ingredients or steps.
- Make concise, user-friendly recipe prose.
"""

def propose_recipe_edit(recipe_kind, recipe, user_message, conversation_history=None):
    """Use the configured model to propose a structured recipe edit."""
    if not llm_service:
        return {
            "apply_change": False,
            "summary": "Feedback recorded; Dieter model is not configured.",
            "title": None,
            "ingredients_text": None,
            "instructions_text": None,
            "changed_fields": [],
            "assistant_message": "I recorded that feedback, but the model is not configured so I did not change the recipe text.",
            "model": "",
        }

    prompt = build_recipe_edit_prompt(recipe_kind, recipe, user_message)
    messages = list(conversation_history or [])[-6:]
    messages.append({"role": "user", "content": prompt})
    raw_response = llm_service.provider.chat(messages, RECIPE_EDIT_SYSTEM_PROMPT)
    parsed = extract_json_object(raw_response)
    parsed["model"] = getattr(llm_service.provider, "model", "")
    return parsed

def apply_recipe_edit(recipe_kind, recipe_id, user_message, proposal):
    """Apply an allowed recipe edit and record its structured change log."""
    recipe = recipe_record_for_edit(recipe_kind, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")

    allowed_fields = ["title", "ingredients_text", "instructions_text"]
    before = {
        "title": recipe.get("display_title") or recipe.get("title") or "",
        "ingredients_text": recipe.get("display_ingredients_text") or "",
        "instructions_text": recipe.get("display_instructions_text") or "",
    }
    after = dict(before)
    changed_fields = []

    if proposal.get("apply_change"):
        for field in allowed_fields:
            value = proposal.get(field)
            if isinstance(value, str) and value.strip() and value != before[field]:
                after[field] = value.strip()
                changed_fields.append(field)

    if recipe_kind == "meal" and changed_fields:
        db.update_recipe_complete_meal_edits(
            recipe_id,
            title=after["title"] if "title" in changed_fields else None,
            ingredients_text=after["ingredients_text"] if "ingredients_text" in changed_fields else None,
            instructions_text=after["instructions_text"] if "instructions_text" in changed_fields else None,
        )
    elif recipe_kind == "component" and changed_fields:
        db.update_recipe_component_edits(
            recipe_id,
            title=after["title"] if "title" in changed_fields else None,
            ingredients_text=after["ingredients_text"] if "ingredients_text" in changed_fields else None,
            instructions_text=after["instructions_text"] if "instructions_text" in changed_fields else None,
        )

    summary = proposal.get("summary") or ("Updated recipe." if changed_fields else "Recorded feedback.")
    change_id = db.add_recipe_change_log(
        recipe_kind,
        recipe_id,
        user_message,
        summary,
        changed_fields,
        before,
        after,
        proposal.get("model", ""),
    )
    variation_id = None
    if changed_fields:
        variation_id = db.add_recipe_variation(
            recipe_kind,
            recipe_id,
            after["title"],
            after["ingredients_text"],
            after["instructions_text"],
            summary,
            threshold=2,
        )
        db.upvote_recipe_variation(variation_id)
    return {
        "change_id": change_id,
        "variation_id": variation_id,
        "changed_fields": changed_fields,
        "summary": summary,
        "assistant_message": proposal.get("assistant_message") or summary,
        "after": after,
    }

def parse_planner_target_from_url(page_url):
    """Resolve a planner URL into the strongest available project/task target."""
    action_match = re.search(r"/projects/(\d+)/actions/(\d+)", page_url or "")
    if action_match:
        return {
            "target_kind": "task",
            "project_id": int(action_match.group(1)),
            "action_id": int(action_match.group(2)),
        }
    project_match = re.search(r"/projects/(\d+)", page_url or "")
    if project_match:
        return {
            "target_kind": "project",
            "project_id": int(project_match.group(1)),
            "action_id": None,
        }
    if page_url == "/" or re.search(r"/apps/assistant|/apps/planner|/dashboard|/apps\b", page_url or ""):
        dashboard = agent_service.build_dashboard_context()
        project = dashboard.get("recommended_project")
        action = dashboard.get("next_action")
        return {
            "target_kind": "planner",
            "project_id": project.get("id") if project else None,
            "action_id": action.get("id") if action else None,
        }
    return {"target_kind": "unknown", "project_id": None, "action_id": None}

def build_planner_edit_context(page_url):
    """Build compact planner context with IDs for structured edits."""
    target = parse_planner_target_from_url(page_url)
    projects = [dict(row) for row in db.get_all_projects()]
    lines = [
        f"Current URL: {page_url}",
        f"Resolved target: {target}",
        "",
        "Open scheduler/agenda items:",
    ]
    for item in db.get_scheduler_items(status="open", limit=20):
        item = dict(item)
        timing = item.get("scheduled_for") or "unscheduled"
        context_label = item.get("context_label") or "general"
        notes = f" notes={item.get('notes', '')}" if item.get("notes") else ""
        lines.append(
            f"- Scheduler id={item['id']} context={context_label} when={timing}: {item.get('title', '')}{notes}"
        )
    lines.extend([
        "",
        "Projects and editable planner records:",
    ])
    for project in projects:
        lines.append(
            f"- Project id={project['id']} name={project['name']} "
            f"priority={project.get('priority_score', 3)} status={project.get('status', 'active')}"
        )
        if project.get("description"):
            lines.append(f"  description: {project['description']}")
        actions = [dict(row) for row in db.get_recommended_actions(project["id"])]
        for action in actions[:12]:
            lines.append(
                f"  - Task id={action['id']} priority={action.get('priority', 'medium')} "
                f"status={action.get('status', 'open')}: {action.get('action', '')}"
            )
            steps = [dict(row) for row in db.get_task_steps(action["id"])]
            for step in steps[:12]:
                lines.append(
                    f"    - Step id={step['id']} status={step.get('status', 'open')}: {step.get('step', '')}"
                )
        blockers = [dict(row) for row in db.get_blockers(project["id"])]
        for blocker in blockers[:8]:
            lines.append(
                f"  - Blocker id={blocker['id']} severity={blocker.get('severity', 'medium')}: "
                f"{blocker.get('description', '')}"
            )
        goals = [dict(row) for row in db.get_weekly_goals(project["id"])]
        for goal in goals[:8]:
            status = "done" if goal.get("completed") else "open"
            lines.append(f"  - Goal id={goal['id']} status={status}: {goal.get('goal', '')}")
    return target, "\n".join(lines)

PLANNER_EDIT_SYSTEM_PROMPT = """You are Dieter, a structured planner editing assistant.

You may update the user's local planner only when the request is clear enough.
Use the current URL target when the user says "this task", "this project", or "mark it done".
If the target or requested edit is unclear, do not apply changes; ask one concise clarification question.

Return only valid JSON with this shape:
{
  "apply_change": true,
  "summary": "",
  "operations": [],
  "assistant_message": ""
}

Allowed operation shapes:
- {"op":"add_project","name":"","description":"","priority_score":3}
- {"op":"update_project","project_id":1,"name":null,"description":null,"priority_score":null,"focus_reason":null,"status":null}
- {"op":"add_note","project_id":1,"content":""}
- {"op":"add_task","project_id":1,"action":"","priority":"medium"}
- {"op":"update_task","action_id":1,"action":null,"priority":null}
- {"op":"complete_task","action_id":1}
- {"op":"reopen_task","action_id":1}
- {"op":"add_step","action_id":1,"step":""}
- {"op":"update_step","step_id":1,"step":""}
- {"op":"complete_step","step_id":1}
- {"op":"reopen_step","step_id":1}
- {"op":"add_blocker","project_id":1,"description":"","severity":"medium"}
- {"op":"delete_blocker","blocker_id":1}
- {"op":"add_goal","project_id":1,"goal":""}
- {"op":"complete_goal","goal_id":1}
- {"op":"add_scheduler_item","title":"","context_label":"","scheduled_for":"","notes":"","project_id":null,"action_id":null}
- {"op":"update_scheduler_item","scheduler_item_id":1,"title":null,"context_label":null,"scheduled_for":null,"notes":null,"status":null}
- {"op":"complete_scheduler_item","scheduler_item_id":1}
- {"op":"reopen_scheduler_item","scheduler_item_id":1}

Rules:
- Return only JSON.
- Do not invent IDs; use IDs from context.
- If adding records, use the current project/task target when appropriate.
- Use scheduler items for contextual reminders, agenda questions, appointments, errands, and "next time I talk to/go to/call..." requests.
- If the user asks to add bullets/details to an existing scheduler context, use update_scheduler_item for that existing item. Do not create a second item with the same title/context.
- For action reminders like "call to have the AC serviced", use the topic/domain as the title and context_label ("AC"), and put the actual action as a short checkbox note ("- [ ] call to have AC serviced").
- For scheduler scheduled_for, use YYYY-MM-DD when the user gives a clear date; otherwise use an empty string.
- For scheduler context_label, use short labels like AC, Pilates, Mechanic, Doctor, Grocery, Insurance, Home, or Call.
- For scheduler additions, do not invent or expand notes. Save only details the user explicitly gave.
- For scheduler additions, keep notes as short bullets or an empty string. Never save "Original request:" text.
- For scheduler note updates, never repeat the user's whole instruction. Keep or merge concise bullets only.
- For scheduler note updates with one topic plus details, use one parent bullet and indented child bullets, for example "- Good stereo guy\n  - Name: Sam\n  - Phone: 555-1234"; do not flatten those details into separate top-level bullets.
- For scheduler additions, make assistant_message explicit: say what was saved, the date/context if known, where it was saved, and whether the user should add missing time/location details.
- If an appointment/reminder lacks an exact time or location, still save it, but mention missing details in assistant_message only, not in scheduler notes.
- For non-scheduler changes, keep assistant_message short and say what changed.
- Use priority values only: high, medium, low.
- Use severity values only: high, medium, low.
- Use project status values only: active, paused, done, archived.
- Use scheduler status values only: open, done, archived.
"""

def infer_scheduler_context_label(text):
    """Infer a short agenda context from common reminder wording."""
    text_lower = (text or "").lower()
    context_map = [
        ("air conditioner", "AC"),
        ("air conditioning", "AC"),
        ("hvac", "AC"),
        ("a/c", "AC"),
        (" ac ", "AC"),
        ("ac ", "AC"),
        ("pilates", "Pilates"),
        ("home", "Home"),
        ("house", "Home"),
        ("chore", "Home"),
        ("chores", "Home"),
        ("before bed", "Home"),
        ("bedtime", "Home"),
        ("mechanic", "Mechanic"),
        ("doctor", "Doctor"),
        ("dentist", "Dentist"),
        ("grocery", "Grocery"),
        ("store", "Store"),
        ("insurance", "Insurance"),
        ("call", "Call"),
        ("appointment", "Appointment"),
    ]
    for needle, label in context_map:
        if needle in text_lower:
            return label
    return "General"

def refine_scheduler_context_label(text, proposed_context=""):
    """Prefer a specific inferred scheduler context over a generic model label."""
    proposed = (proposed_context or "").strip()
    inferred = infer_scheduler_context_label(text)
    generic_contexts = {"", "General", "Call", "Class", "Task", "Reminder", "Scheduler"}
    if proposed in generic_contexts and inferred and inferred != "General":
        return inferred
    return proposed or inferred or "General"

def app_now():
    """Return the app-local time used for casual scheduling language."""
    timezone_name = os.getenv("APP_TIMEZONE", "America/Phoenix")
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except Exception:
        return datetime.now()

def app_today():
    """Return the app-local date used for scheduler priority."""
    return app_now().date()

def local_scheduler_proposal(user_message, target):
    """Build a conservative scheduler edit when no model is configured."""
    text = (user_message or "").strip()
    if not text:
        return None
    scheduler_cues = [
        "remind me",
        "remember",
        "next time",
        "ask my",
        "ask the",
        "appointment",
        "class",
        "pilates",
        "agenda",
        "schedule",
        "scheduler",
        "tomorrow",
        "chore",
        "chores",
        "card",
        "task",
        "to do",
        "todo",
    ]
    if not any(cue in text.lower() for cue in scheduler_cues):
        return None
    return {
        "apply_change": True,
        "summary": "Added scheduler item.",
        "operations": [{
            "op": "add_scheduler_item",
            "title": text,
            "context_label": infer_scheduler_context_label(text),
            "scheduled_for": "",
            "notes": "",
            "project_id": target.get("project_id"),
            "action_id": target.get("action_id"),
        }],
        "assistant_message": "I added that to your Scheduler.",
        "model": "local-scheduler",
        "target": target,
    }

def extract_scheduler_agenda_items(text):
    """Extract agenda bullets from ask-about scheduler wording."""
    original = (text or "").strip()
    if not original:
        return []

    agenda_text = ""
    patterns = [
        r"\bask(?:\s+(?:my|the|them|him|her|mechanic|doctor|dentist|shop|garage))?\s+about\s*:?\s*(.+)",
        r"\bthings\s+to\s+ask(?:\s+(?:my|the|mechanic|doctor|dentist|shop|garage))?\s*:?\s*(.+)",
        r"\bagenda\s*:?\s*(.+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, original, flags=re.IGNORECASE | re.DOTALL)
        if match:
            agenda_text = match.group(1)
            break

    if not agenda_text:
        return []

    agenda_text = re.sub(r"\b(today|tomorrow|next week)\b", "", agenda_text, flags=re.IGNORECASE)
    agenda_text = re.sub(r"\bon\s+\d{4}-\d{2}-\d{2}\b", "", agenda_text, flags=re.IGNORECASE)
    agenda_text = re.sub(r"\s+", " ", agenda_text).strip(" .")
    if not agenda_text:
        return []

    raw_items = re.split(r"\s*(?:,|;|\n|\band\b)\s*", agenda_text)
    items = []
    for raw_item in raw_items:
        item = re.sub(r"^[-*]\s*", "", raw_item).strip(" .")
        item = re.sub(r"^(ask\s+about|about)\s+", "", item, flags=re.IGNORECASE).strip(" .")
        if item and item.lower() not in {"it", "that", "this"}:
            items.append(item)
    return items

def extract_scheduler_detail_items(text, context_label=""):
    """Extract explicit non-agenda details without preserving the raw prompt."""
    original = (text or "").strip()
    if not original:
        return []

    detail_text = original
    detail_markers = [
        r"\bnotes?\s*:?\s*(.+)",
        r"\bdetails?\s*:?\s*(.+)",
        r"\bremember\s+to\s*:?\s*(.+)",
    ]
    for pattern in detail_markers:
        match = re.search(pattern, original, flags=re.IGNORECASE | re.DOTALL)
        if match:
            detail_text = match.group(1)
            break

    detail_text = re.sub(
        r"^\s*(please\s+)?(remind me|remember|add|put|schedule)\s+(to|about|that)?\s*",
        "",
        detail_text,
        flags=re.IGNORECASE,
    )
    detail_text = re.sub(r"\b(today|tomorrow|next week)\b", "", detail_text, flags=re.IGNORECASE)
    detail_text = re.sub(r"\bon\s+\d{4}-\d{2}-\d{2}\b", "", detail_text, flags=re.IGNORECASE)
    if context_label:
        detail_text = re.sub(rf"\b(my|the)?\s*{re.escape(context_label)}('?s)?\b", "", detail_text, flags=re.IGNORECASE)
    detail_text = re.sub(r"\b(appointment|appt|reminder|agenda|note)\b", "", detail_text, flags=re.IGNORECASE)
    detail_text = re.sub(r"\s+", " ", detail_text).strip(" .:-")
    if not detail_text or len(detail_text) < 4:
        return []

    raw_items = re.split(r"\s*(?:,|;|\n|\band\b)\s*", detail_text)
    items = []
    for raw_item in raw_items:
        item = raw_item.strip(" .:-")
        if item and item.lower() not in {"it", "that", "this"}:
            items.append(item)
    return items[:5]

def scheduler_list_request(text):
    """Detect voice-style requests that should become checklist bullets."""
    return bool(re.search(r"\b(chore|chores|to do|todo|checklist|list|tasks?)\b", text or "", flags=re.IGNORECASE))

def scheduler_request_targets_existing_notes(text):
    """Return true when the user explicitly wants to add details to an existing card."""
    return bool(re.search(
        r"\b(add|put|save)\b.+\b(to|under|in)\b.+\b(bullet|bullets|card|item|note|notes)\b",
        text or "",
        flags=re.IGNORECASE | re.DOTALL,
    ))

def extract_scheduler_list_items(text, context_label=""):
    """Extract checklist items from dictated list requests."""
    original = (text or "").strip()
    if not original or not scheduler_list_request(original):
        return []

    list_text = ""
    patterns = [
        r"\b(?:make|create|build|add|put)(?:\s+me)?(?:\s+a|\s+an|\s+the)?\s+(?:(?:chore|chores|to do|todo|task|tasks|checklist)\s+)?list(?:\s+for\s+(?:today|tomorrow|tonight|this evening))?(?:\s+(?:of|with|that includes|including|for))?\s*:?\s*(.+)$",
        r"\b(?:chores|tasks|to do|todo|checklist)(?:\s+for\s+(?:today|tomorrow|tonight|this evening))?\s*:?\s*(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, original, flags=re.IGNORECASE | re.DOTALL)
        if match:
            list_text = match.group(1)
            break
    if not list_text:
        list_text = original

    list_text = re.sub(
        r"^\s*(please\s+)?(?:make|create|build|add|put|remember|remind me)(?:\s+me)?(?:\s+a|\s+an|\s+the)?\s+",
        "",
        list_text,
        flags=re.IGNORECASE,
    )
    list_text = re.sub(r"\b(?:chore|chores|to do|todo|task|tasks|checklist)\s+list\b", "", list_text, flags=re.IGNORECASE)
    list_text = re.sub(r"\blist\s+(?:for|of|with|that includes|including)\b", "", list_text, flags=re.IGNORECASE)
    list_text = re.sub(r"\bfor\s+(?:today|tomorrow|tonight|this evening)\b", "", list_text, flags=re.IGNORECASE)
    list_text = re.sub(r"\b(?:today|tomorrow|tonight|this evening)\b", "", list_text, flags=re.IGNORECASE)
    if context_label:
        list_text = re.sub(rf"\b(my|the)?\s*{re.escape(context_label)}('?s)?\b", "", list_text, flags=re.IGNORECASE)
    list_text = re.sub(r"\s+", " ", list_text).strip(" .:-")
    if not list_text:
        return []

    raw_items = re.split(
        r"\s*(?:,|;|\n|\band\s+then\b|\bthen\b|\balso\b|\bplus\b|\band\b)\s*",
        list_text,
        flags=re.IGNORECASE,
    )
    items = []
    seen = set()
    for raw_item in raw_items:
        item = raw_item.strip(" .:-")
        item = re.sub(r"^(that\s+)?(i\s+)?(?:also\s+)?(?:need to|have to|should|must|to)\s+", "", item, flags=re.IGNORECASE)
        item = re.sub(r"^(a|an|the)\s+", "", item, flags=re.IGNORECASE).strip(" .:-")
        key = re.sub(r"[^a-z0-9]+", " ", item.lower()).strip()
        if key and key not in {"it", "that", "this", "list"} and key not in seen:
            items.append(item[0].upper() + item[1:] if item else item)
            seen.add(key)
    return items[:12]

def extract_scheduler_action_note(text, context_label=""):
    """Extract the concrete action from a short action reminder."""
    original = re.sub(r"\s+", " ", (text or "").strip())
    if not original:
        return ""
    cleaned = re.sub(
        r"^\s*(please\s+)?(?:remind me|remember|add|put|schedule)\s+(?:me\s+)?(?:to|about|that)?\s*",
        "",
        original,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\b(?:today|tomorrow|tonight|before bed|bedtime|this evening|next week)\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bon\s+\d{4}-\d{2}-\d{2}\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(for|on|by)\s*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .:-")
    if not re.match(r"^(call|email|text|message|schedule|book|buy|order|pay|pick up|drop off)\b", cleaned, flags=re.IGNORECASE):
        return ""
    cleaned = re.sub(r"\bthe\s+(AC|A/C|HVAC)\b", "AC", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bair\s+condition(?:er|ing)\b", "AC", cleaned, flags=re.IGNORECASE)
    if context_label and context_label.upper() != "AC":
        cleaned = re.sub(rf"\b(my|the)?\s*{re.escape(context_label)}('?s)?\b", context_label, cleaned, flags=re.IGNORECASE)
    return cleaned[:180]

def clean_scheduler_note_line(line):
    """Normalize a scheduler note line and drop obvious prompt echoes."""
    cleaned = re.sub(r"^[-*]\s*", "", (line or "").strip())
    checkbox_prefix = ""
    checkbox_match = re.match(r"^\[( |x|X)\]\s*", cleaned)
    if checkbox_match:
        checkbox_prefix = f"[{checkbox_match.group(1).lower()}] "
        cleaned = cleaned[checkbox_match.end():]
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        return ""
    if re.search(r"\b(add|put)\b.*\b(to|under|in)\b.*\b(bullet|bullets|card|item|scheduler)\b", cleaned, re.IGNORECASE):
        return ""
    parts = [
        part.strip(" .")
        for part in re.split(r"\s*(?:[.;]\s+|\n+)\s*", cleaned)
        if part.strip(" .")
    ]
    if len(parts) > 1:
        deduped_parts = []
        seen_parts = set()
        for part in parts:
            key = part.lower()
            if key not in seen_parts:
                deduped_parts.append(part)
                seen_parts.add(key)
        cleaned = ". ".join(deduped_parts)
    return f"{checkbox_prefix}{cleaned[:180]}"

def is_scheduler_child_note_line(line):
    """Detect indented markdown-ish child bullets."""
    return bool(re.match(r"^\s{2,}[-*]\s+", line or ""))

def format_scheduler_note_lines(lines):
    """Format note lines while preserving explicit child bullets."""
    formatted = []
    for line in lines:
        if not line:
            continue
        if is_scheduler_child_note_line(line):
            child = clean_scheduler_note_line(line)
            if child:
                formatted.append(f"  - {child}")
        else:
            parent = clean_scheduler_note_line(line)
            if parent:
                formatted.append(f"- {parent}")
    return "\n".join(formatted)

def normalize_scheduler_notes(notes):
    """Return clean, deduplicated bullet-form notes, preserving one nested level."""
    lines = []
    seen = set()
    for raw_line in (notes or "").splitlines():
        line = clean_scheduler_note_line(raw_line)
        is_child = is_scheduler_child_note_line(raw_line)
        key = f"{'child' if is_child else 'parent'}:{line.lower()}"
        if line and key not in seen:
            lines.append(f"  - {line}" if is_child and lines else f"- {line}")
            seen.add(key)
    return "\n".join(lines[:20])

def merge_scheduler_notes(existing_notes, new_notes):
    """Merge note bullets without duplicating existing content, preserving child bullets."""
    merged_lines = []
    seen = set()
    for notes in [existing_notes or "", new_notes or ""]:
        for raw_line in notes.splitlines():
            line = clean_scheduler_note_line(raw_line)
            is_child = is_scheduler_child_note_line(raw_line)
            key = f"{'child' if is_child else 'parent'}:{line.lower()}"
            if line and key not in seen:
                merged_lines.append(f"  - {line}" if is_child and merged_lines else f"- {line}")
                seen.add(key)
    return "\n".join(merged_lines[:20])

def scheduler_note_tree(notes):
    """Build display-ready scheduler note parents with optional child bullets."""
    items = []
    for line_index, raw_line in enumerate((notes or "").splitlines()):
        text = clean_scheduler_note_line(raw_line)
        if not text:
            continue
        checkbox_match = re.match(r"^\[( |x)\]\s+", text, flags=re.IGNORECASE)
        note = {
            "text": re.sub(r"^\[( |x)\]\s+", "", text, flags=re.IGNORECASE),
            "children": [],
            "line_index": line_index,
            "checkable": bool(checkbox_match),
            "checked": bool(checkbox_match and checkbox_match.group(1).lower() == "x"),
        }
        if is_scheduler_child_note_line(raw_line) and items:
            items[-1]["children"].append(note)
        else:
            items.append(note)
    return items

jinja_env.globals["scheduler_note_tree"] = scheduler_note_tree

def toggle_scheduler_note_checkbox(notes, line_index):
    """Toggle one markdown checkbox note line."""
    lines = (notes or "").splitlines()
    if line_index < 0 or line_index >= len(lines):
        return notes or ""
    line = lines[line_index]
    match = re.match(r"^(\s*[-*]\s*)\[\s*([xX]?)\s*\](\s+.*)$", line)
    if not match:
        return notes or ""
    next_marker = " " if match.group(2).lower() == "x" else "x"
    lines[line_index] = f"{match.group(1)}[{next_marker}]{match.group(3)}"
    return "\n".join(lines)

def scheduler_notes_need_checklist(notes):
    """Return true when a scheduler item has plain note lines."""
    for raw_line in (notes or "").splitlines():
        line = clean_scheduler_note_line(raw_line)
        if line and not re.match(r"^\[( |x)\]\s+", line, flags=re.IGNORECASE):
            return True
    return False

jinja_env.globals["scheduler_notes_need_checklist"] = scheduler_notes_need_checklist

def make_scheduler_notes_checklist(notes):
    """Convert existing scheduler notes into unchecked checklist bullets."""
    converted = []
    for raw_line in (notes or "").splitlines():
        line = clean_scheduler_note_line(raw_line)
        if not line:
            continue
        if not re.match(r"^\[( |x)\]\s+", line, flags=re.IGNORECASE):
            line = f"[ ] {line}"
        converted.append(f"  - {line}" if is_scheduler_child_note_line(raw_line) and converted else f"- {line}")
    return "\n".join(converted[:20])

def find_open_scheduler_item_for_context(context_label, title="", scheduled_for=""):
    """Find an existing open scheduler item by short context/title."""
    context_key = (context_label or "").strip().lower()
    title_key = (title or "").strip().lower()
    scheduled_key = (scheduled_for or "").strip()
    if not context_key and not title_key:
        return None
    for item in dicts_from_rows(db.get_scheduler_items(status="open", limit=100)):
        item_scheduled = (item.get("scheduled_for") or "").strip()
        if (scheduled_key or item_scheduled) and scheduled_key != item_scheduled:
            continue
        item_context = (item.get("context_label") or "").strip().lower()
        item_title = (item.get("title") or "").strip().lower()
        if title_key and title_key in {item_context, item_title}:
            return item
        generic_titles = {"", "scheduler reminder", "reminder", "scheduler item", context_key}
        if title_key not in generic_titles:
            continue
        if context_key and context_key != "general" and context_key in {item_context, item_title}:
            return item
    return None

def extract_scheduler_bullet_additions(text, context_label=""):
    """Extract bullet additions from requests like 'add X to mechanic bullets'."""
    original = (text or "").strip()
    if not original:
        return []
    context = (context_label or infer_scheduler_context_label(original) or "").lower()
    context_pattern = re.escape(context) if context and context != "general" else r"[a-z]+"
    patterns = [
        rf"\badd\s+(?:this\s+)?(?:to\s+)?(?:my\s+|the\s+)?{context_pattern}\s+(?:bullet|bullets|card|item|note|notes)\s*:?\s*(.+)",
        rf"\badd\s+(.+?)\s+(?:to|under|in)\s+(?:my\s+|the\s+)?{context_pattern}\s+(?:bullet|bullets|card|item|note|notes)\b",
        rf"\b(?:also\s+)?ask\s+about\s+(.+)",
    ]
    addition_text = ""
    for pattern in patterns:
        match = re.search(pattern, original, flags=re.IGNORECASE | re.DOTALL)
        if match:
            addition_text = match.group(1)
            break
    if not addition_text:
        return []
    addition_text = re.sub(r"\b(today|tomorrow|next week)\b", "", addition_text, flags=re.IGNORECASE)
    addition_text = re.sub(r"\s+", " ", addition_text).strip(" .:-")

    stereo_match = re.search(
        r"\b(?:a\s+)?good\s+stereo\s+guy\b(?P<details>.*)",
        addition_text,
        flags=re.IGNORECASE,
    )
    if stereo_match:
        details_text = stereo_match.group("details").strip(" .:-")
        detail_items = []
        name_match = re.search(
            r"\b(?:named|name(?:\s+is)?|called)\s+(.+?)(?=\s+(?:and\s+)?(?:phone|number|cell|mobile)\b|[,;.]|$)",
            details_text,
            flags=re.IGNORECASE,
        )
        if name_match:
            detail_items.append(f"Name: {name_match.group(1).strip(' .')}")
        phone_match = re.search(
            r"\b(?:phone|number|cell|mobile)(?:\s+is)?\s*[:\-]?\s*([+()\d][+()\d\s.-]{5,})",
            details_text,
            flags=re.IGNORECASE,
        )
        if phone_match:
            detail_items.append(f"Phone: {phone_match.group(1).strip(' .')}")
        if not detail_items and details_text:
            for part in re.split(r"\s*(?:,|;|\band\b)\s*", details_text):
                cleaned = clean_scheduler_note_line(part)
                if cleaned:
                    detail_items.append(cleaned)
        return ["Good stereo guy", *[f"  - {item}" for item in detail_items[:4]]]

    raw_items = re.split(r"\s*(?:,|;|\n|\band\b)\s*", addition_text)
    items = []
    for raw_item in raw_items:
        item = clean_scheduler_note_line(raw_item)
        item = re.sub(r"^(ask\s+about|about)\s+", "", item, flags=re.IGNORECASE).strip(" .")
        if item:
            items.append(f"Ask about {item}")
    return items[:8]

def format_scheduler_confirmation(items):
    """Explain scheduler changes in a useful, human-readable way."""
    if not items:
        return ""
    lines = []
    for item in items:
        title = item.get("title") or "Untitled reminder"
        context_label = item.get("context_label") or "General"
        scheduled_for = item.get("scheduled_for") or ""
        notes = item.get("notes") or ""
        when = scheduled_for if scheduled_for else "no exact date saved"
        lines.append(f"Saved to Scheduler: {title}")
        lines.append(f"Context: {context_label}")
        lines.append(f"When: {when}")
        if notes:
            lines.append(f"Notes: {notes}")
        missing = []
        if not scheduled_for:
            missing.append("date")
        if not re.search(r"\b\d{1,2}(:\d{2})?\s*(am|pm)\b|\b\d{1,2}:\d{2}\b", notes, flags=re.IGNORECASE):
            missing.append("time")
        if not re.search(r"\bat\b|\baddress\b|\blocation\b|\bshop\b|\bgarage\b|\bclinic\b", notes, flags=re.IGNORECASE):
            missing.append("location")
        if missing:
            lines.append(f"Still useful to add later: {', '.join(missing)}.")
        lines.append("You can review it at /apps/assistant/scheduler.")
    return "\n".join(lines)

def format_planner_write_destinations(operations):
    """Make it explicit where Dieter wrote structured planner results."""
    destinations = []
    for operation in operations or []:
        op = operation.get("op", "")
        if "scheduler_item" in op:
            label = "Scheduler"
            path = "/apps/assistant/scheduler"
        elif op in {"add_task", "update_task", "complete_task", "reopen_task"}:
            label = "Planner task list"
            path = "/apps/assistant/planner"
        elif op in {"add_step", "update_step", "complete_step", "reopen_step"}:
            label = "Task checklist"
            path = "/apps/assistant/planner"
        elif op in {"add_project", "update_project"}:
            label = "Planner projects"
            path = "/apps/assistant/planner"
        elif op in {"add_note"}:
            label = "Project notes"
            path = "/apps/assistant/planner"
        elif op in {"add_blocker", "delete_blocker"}:
            label = "Project blockers"
            path = "/apps/assistant/planner"
        elif op in {"add_goal", "complete_goal"}:
            label = "Weekly goals"
            path = "/apps/assistant/planner"
        else:
            continue
        destination = f"Wrote result to: {label} ({path})."
        if destination not in destinations:
            destinations.append(destination)
    return "\n".join(destinations)

def scheduler_text_implies_today(text):
    """Detect casual same-day scheduler phrasing."""
    return bool(re.search(r"\b(today|tonight|before bed|bedtime|this evening|in an hour|in \d+ hours?)\b", text or "", flags=re.IGNORECASE))

def scheduler_date_from_text(text):
    """Resolve simple relative scheduler dates from user text."""
    text = text or ""
    today = app_today()
    if re.search(r"\btomorrow\b", text, flags=re.IGNORECASE):
        return (today + timedelta(days=1)).isoformat()
    if scheduler_text_implies_today(text):
        return today.isoformat()
    return ""

def title_from_scheduler_note_line(line, fallback="Scheduler item"):
    """Create a short card title from a standalone scheduler note."""
    cleaned = clean_scheduler_note_line(line)
    cleaned = re.sub(r"^\[( |x)\]\s+", "", cleaned, flags=re.IGNORECASE)
    for _ in range(2):
        cleaned = re.sub(r"^\s*(up\s+)?that\s+", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"^\s*in (an|\d+) hours?\s+", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"^\s*(i\s+)?(need to|have to|should|must)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" .")
    if not cleaned:
        return fallback
    return cleaned[0].upper() + cleaned[1:80]

def split_mixed_priority_scheduler_notes():
    """Move obvious today-note lines out of future scheduler cards."""
    today = app_today()
    today_iso = today.isoformat()
    for item in dicts_from_rows(db.get_scheduler_items(status="open", limit=250)):
        scheduled_for = (item.get("scheduled_for") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", scheduled_for or ""):
            continue
        try:
            scheduled_date = datetime.strptime(scheduled_for, "%Y-%m-%d").date()
        except ValueError:
            continue
        if scheduled_date <= today or not (item.get("notes") or "").strip():
            continue

        keep_lines = []
        move_lines = []
        for raw_line in (item.get("notes") or "").splitlines():
            line_date = scheduler_date_from_text(raw_line)
            if line_date == today_iso:
                move_lines.append(raw_line)
            else:
                keep_lines.append(raw_line)
        if not move_lines:
            continue

        db.update_scheduler_item(item["id"], notes="\n".join(keep_lines))
        title = (
            title_from_scheduler_note_line(move_lines[0])
            if len(move_lines) == 1
            else item.get("context_label") or item.get("title") or "Today"
        )
        context_label = item.get("context_label") or "General"
        notes = "" if len(move_lines) == 1 else normalize_scheduler_notes("\n".join(move_lines))
        existing = find_open_scheduler_item_for_context(context_label, title, today_iso)
        if existing:
            merged_notes = merge_scheduler_notes(existing.get("notes", ""), notes or normalize_scheduler_notes("\n".join(move_lines)))
            db.update_scheduler_item(existing["id"], notes=merged_notes)
        else:
            db.add_scheduler_item(
                title,
                context_label=context_label,
                scheduled_for=today_iso,
                notes=notes,
                source="dieter-cleanup",
                project_id=item.get("project_id"),
                action_id=item.get("action_id"),
            )

def synthesize_scheduler_operation(user_message, operation):
    """Turn raw reminder text into a concise scheduler record."""
    original = (user_message or "").strip()
    title = (operation.get("title") or original).strip()
    context_label = refine_scheduler_context_label(original, operation.get("context_label")).strip()
    scheduled_for = (operation.get("scheduled_for") or "").strip()
    bullet_additions = extract_scheduler_bullet_additions(original, context_label)
    agenda_items = bullet_additions or extract_scheduler_agenda_items(original)
    list_items = extract_scheduler_list_items(original, context_label)
    detail_items = [] if list_items else extract_scheduler_detail_items(original, context_label)

    text = original.lower()
    if not scheduled_for:
        scheduled_for = scheduler_date_from_text(text)

    rawish_title = title.lower() == original.lower() or len(title) > 80
    is_list_request = scheduler_list_request(original)
    chore_list_match = re.search(r"\b(chore|chores)\b", original, flags=re.IGNORECASE)
    if context_label and context_label != "General" and not (context_label == "Home" and rawish_title and not list_items):
        if list_items:
            title = "Chore list" if chore_list_match else f"{context_label} checklist"
        else:
            title = context_label
    elif rawish_title:
        cleaned = re.sub(
            r"^\s*(please\s+)?(remind me|remember|add|put|schedule|make|create)\s+(me\s+)?(a\s+|an\s+|the\s+)?(to|about|that)?\s*",
            "",
            original,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\b(today|tomorrow|tonight|before bed|bedtime|this evening)\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\b(for|on|by)\s*$", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\b(to|in|on)\s+(the\s+)?scheduler\b", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
        if list_items and chore_list_match:
            title = "Chore list"
        elif list_items:
            title = "Checklist"
        elif cleaned:
            title = cleaned[0].upper() + cleaned[1:]
        else:
            title = "Scheduler reminder"

    note_lines = []
    if list_items:
        note_lines = list_items
    elif agenda_items:
        note_lines = [
            item if re.match(r"^ask\s+about\b", item, flags=re.IGNORECASE) else f"Ask about {item}"
            for item in agenda_items
        ]
    elif is_list_request:
        note_lines = []
    elif context_label and context_label != "General":
        action_note = extract_scheduler_action_note(original, context_label)
        if action_note and action_note.lower() != title.lower():
            note_lines = [f"[ ] {action_note}"]
    elif detail_items and not (len(detail_items) == 1 and detail_items[0].lower() == title.lower()):
        normalized_title = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
        normalized_detail = re.sub(r"[^a-z0-9]+", " ", detail_items[0].lower()).strip() if len(detail_items) == 1 else ""
        if not (normalized_title and normalized_title in normalized_detail and scheduler_text_implies_today(normalized_detail)):
            note_lines = detail_items
    if note_lines and (
        len(note_lines) > 1
        or is_list_request
    ):
        note_lines = [
            line if re.match(r"^\[( |x|X)\]\s+", line) else f"[ ] {line}"
            for line in note_lines
        ]

    operation["title"] = title
    operation["context_label"] = context_label or "General"
    operation["scheduled_for"] = scheduled_for
    operation["notes"] = normalize_scheduler_notes(format_scheduler_note_lines(note_lines))
    return operation

def enrich_scheduler_item_priority(item, today=None):
    """Add date priority fields used by scheduler cards."""
    today = today or app_today()
    scheduled_for = (item.get("scheduled_for") or "").strip()
    if not scheduled_for:
        combined_text = " ".join([
            item.get("title") or "",
            item.get("context_label") or "",
            item.get("notes") or "",
        ])
        if scheduler_text_implies_today(combined_text):
            item["scheduled_date"] = today.isoformat()
            item["is_due"] = False
            item["is_today"] = True
            item["scheduler_visual_priority"] = "today"
            return item
        item["is_due"] = False
        item["is_today"] = False
        item["scheduler_visual_priority"] = "unscheduled"
        return item
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", scheduled_for):
            scheduled_date = datetime.strptime(scheduled_for, "%Y-%m-%d").date()
        else:
            scheduled_at = datetime.fromisoformat(scheduled_for.replace("Z", "+00:00"))
            scheduled_date = scheduled_at.date()
    except ValueError:
        item["is_due"] = False
        item["is_today"] = False
        item["scheduler_visual_priority"] = "upcoming"
        return item
    is_today = scheduled_date == today
    is_due = scheduled_date < today
    is_this_week = today < scheduled_date <= today + timedelta(days=6)
    item["scheduled_date"] = scheduled_date.isoformat()
    item["is_due"] = is_due
    item["is_today"] = is_today
    item["is_this_week"] = is_this_week
    item["scheduler_visual_priority"] = "today" if is_today or is_due else "week" if is_this_week else "upcoming"
    return item

def scheduler_due_context():
    """Build visible scheduler notifications for app entry pages."""
    today = app_today()
    split_mixed_priority_scheduler_notes()
    open_items = dicts_from_rows(db.get_scheduler_items(status="open", limit=250))
    due_items = []
    upcoming_items = []
    for item in open_items:
        item = enrich_scheduler_item_priority(item, today)
        if item["is_due"]:
            due_items.append(item)
        elif item["scheduler_visual_priority"] in {"upcoming", "week", "today"}:
            upcoming_items.append(item)
    due_items.sort(key=lambda item: item.get("scheduled_date") or "")
    upcoming_items.sort(key=lambda item: (
        0 if item.get("is_today") or item.get("is_due") else 1 if item.get("is_this_week") else 2,
        item.get("scheduled_date") or "",
        (item.get("title") or "").lower(),
    ))
    today_items = [item for item in upcoming_items if item.get("is_today")]
    future_items = [item for item in upcoming_items if not item.get("is_today")]
    visible_upcoming_items = today_items + future_items[:max(0, 8 - len(today_items))]
    priority_rank = {"today": 0, "week": 1, "upcoming": 2, "unscheduled": 3}
    upcoming_priority = "upcoming"
    if visible_upcoming_items:
        upcoming_priority = min(
            (item.get("scheduler_visual_priority") or "upcoming" for item in visible_upcoming_items),
            key=lambda priority: priority_rank.get(priority, 4),
        )
    return {
        "due_items": due_items[:8],
        "upcoming_items": visible_upcoming_items,
        "upcoming_priority": upcoming_priority,
        "today": today.isoformat(),
    }

def prepare_scheduler_items_for_display(items):
    """Add priority display fields to scheduler items."""
    today = app_today()
    enriched_items = [
        enrich_scheduler_item_priority(dict(item), today)
        for item in items
    ]
    priority_order = {"today": 0, "week": 1, "upcoming": 2, "unscheduled": 3}
    enriched_items.sort(key=lambda item: (
        priority_order.get(item.get("scheduler_visual_priority"), 4),
        item.get("scheduled_date") or "9999-12-31",
        (item.get("title") or "").lower(),
    ))
    return enriched_items

def propose_planner_edit(user_message, page_url="", conversation_history=None):
    """Ask the model for a structured planner edit proposal."""
    target, context = build_planner_edit_context(page_url)
    if not llm_service:
        scheduler_proposal = local_scheduler_proposal(user_message, target)
        if scheduler_proposal:
            return scheduler_proposal
        result = agent_service.chat(
            user_message=user_message,
            project_context=agent_service.build_dashboard_context(),
            conversation_history=conversation_history,
        )
        return {
            "apply_change": bool(result.get("updates")),
            "summary": "Updated planner." if result.get("updates") else "No structured planner edit was applied.",
            "operations": [],
            "assistant_message": result.get("response", "Planner model is not configured."),
            "model": "local-planner",
            "local_result": result,
            "target": target,
        }

    prompt = (
        f"[Planner Context]\n{context}\n\n"
        f"Current date: {app_today().isoformat()}\n"
        "Interpret relative dates like today, tomorrow, and next week from the current date.\n\n"
        f"User request:\n{user_message}"
    )
    messages = list(conversation_history or [])[-6:]
    messages.append({"role": "user", "content": prompt})
    raw_response = llm_service.provider.chat(messages, PLANNER_EDIT_SYSTEM_PROMPT)
    parsed = extract_json_object(raw_response)
    parsed["model"] = getattr(llm_service.provider, "model", "")
    parsed["target"] = target
    return parsed

def snapshot_planner_target(target):
    """Capture before/after state for planner edit logs."""
    project_id = target.get("project_id")
    action_id = target.get("action_id")
    if action_id:
        action = dict_from_row(db.get_recommended_action(action_id))
        if action:
            return {
                "target_kind": "task",
                "target_id": action_id,
                "project": dict_from_row(db.get_project_by_id(action["project_id"])),
                "task": action,
                "steps": dicts_from_rows(db.get_task_steps(action_id)),
                "scheduler_items": dicts_from_rows(db.get_scheduler_items(status="open", limit=20)),
            }
    if project_id:
        return {
            "target_kind": "project",
            "target_id": project_id,
            "project": dict_from_row(db.get_project_by_id(project_id)),
            "tasks": dicts_from_rows(db.get_recommended_actions(project_id)),
            "blockers": dicts_from_rows(db.get_blockers(project_id)),
            "goals": dicts_from_rows(db.get_weekly_goals(project_id)),
            "notes": dicts_from_rows(db.get_notes(project_id)),
            "scheduler_items": [
                item for item in dicts_from_rows(db.get_scheduler_items(status="open", limit=50))
                if item.get("project_id") == project_id
            ],
        }
    return {
        "target_kind": "planner",
        "target_id": 0,
        "dashboard": agent_service.build_dashboard_context(),
        "scheduler_items": dicts_from_rows(db.get_scheduler_items(status="open", limit=20)),
    }

def apply_planner_edit(user_message, page_url, proposal):
    """Apply a structured planner edit proposal and record a change log."""
    target = proposal.get("target") or parse_planner_target_from_url(page_url)
    before = snapshot_planner_target(target)
    applied = []
    errors = []
    allowed_priorities = {"high", "medium", "low"}
    allowed_statuses = {"active", "paused", "done", "archived"}
    allowed_scheduler_statuses = {"open", "done", "archived"}

    if proposal.get("local_result"):
        updates = proposal["local_result"].get("updates", [])
        destination_message = format_planner_write_destinations(updates)
        assistant_message = proposal.get("assistant_message", "")
        if destination_message:
            assistant_message = f"{assistant_message}\n{destination_message}" if assistant_message else destination_message
        return {
            "changed_fields": [update.get("type", "planner_update") for update in updates],
            "summary": proposal.get("summary", "Updated planner."),
            "assistant_message": assistant_message,
            "operations": updates,
            "after": snapshot_planner_target(target),
        }

    if proposal.get("apply_change"):
        for operation in proposal.get("operations") or []:
            op = operation.get("op")
            try:
                if op == "add_project":
                    project_id = db.add_project(
                        operation.get("name", "").strip(),
                        operation.get("description", "").strip(),
                        int(operation.get("priority_score") or 3),
                    )
                    applied.append({"op": op, "project_id": project_id})
                elif op == "update_project":
                    status = operation.get("status")
                    if status is not None and status not in allowed_statuses:
                        raise ValueError("Unsupported project status")
                    db.update_project_details(
                        int(operation["project_id"]),
                        name=operation.get("name"),
                        description=operation.get("description"),
                        priority_score=operation.get("priority_score"),
                        focus_reason=operation.get("focus_reason"),
                    )
                    if status:
                        db.update_project_status(int(operation["project_id"]), status)
                    applied.append(operation)
                elif op == "add_note":
                    db.add_note(int(operation["project_id"]), operation.get("content", "").strip())
                    applied.append(operation)
                elif op == "add_task":
                    priority = operation.get("priority") or "medium"
                    if priority not in allowed_priorities:
                        priority = "medium"
                    action_id = db.add_recommended_action(
                        int(operation["project_id"]),
                        operation.get("action", "").strip(),
                        priority,
                    )
                    applied.append({"op": op, "action_id": action_id})
                elif op == "update_task":
                    priority = operation.get("priority")
                    if priority is not None and priority not in allowed_priorities:
                        raise ValueError("Unsupported task priority")
                    db.update_recommended_action_text(
                        int(operation["action_id"]),
                        action=operation.get("action"),
                        priority=priority,
                    )
                    applied.append(operation)
                elif op == "complete_task":
                    db.mark_recommended_action_complete(int(operation["action_id"]))
                    applied.append(operation)
                elif op == "reopen_task":
                    db.reopen_recommended_action(int(operation["action_id"]))
                    applied.append(operation)
                elif op == "add_step":
                    step_id = db.add_task_step(int(operation["action_id"]), operation.get("step", "").strip())
                    applied.append({"op": op, "step_id": step_id})
                elif op == "update_step":
                    db.update_task_step_text(int(operation["step_id"]), operation.get("step", "").strip())
                    applied.append(operation)
                elif op == "complete_step":
                    db.mark_task_step_complete(int(operation["step_id"]))
                    applied.append(operation)
                elif op == "reopen_step":
                    db.reopen_task_step(int(operation["step_id"]))
                    applied.append(operation)
                elif op == "add_blocker":
                    severity = operation.get("severity") or "medium"
                    if severity not in allowed_priorities:
                        severity = "medium"
                    db.add_blocker(int(operation["project_id"]), operation.get("description", "").strip(), severity)
                    applied.append(operation)
                elif op == "delete_blocker":
                    db.delete_blocker(int(operation["blocker_id"]))
                    applied.append(operation)
                elif op == "add_goal":
                    db.add_weekly_goal(int(operation["project_id"]), operation.get("goal", "").strip())
                    applied.append(operation)
                elif op == "complete_goal":
                    db.mark_goal_complete(int(operation["goal_id"]))
                    applied.append(operation)
                elif op == "add_scheduler_item":
                    operation = synthesize_scheduler_operation(user_message, operation)
                    project_id = operation.get("project_id")
                    action_id = operation.get("action_id")
                    title = operation.get("title", "").strip()
                    context_label = operation.get("context_label", "").strip()
                    scheduled_for = operation.get("scheduled_for", "").strip()
                    notes = operation.get("notes", "").strip()
                    existing_item = find_open_scheduler_item_for_context(context_label, title, scheduled_for) if notes else None
                    if existing_item:
                        item_id = int(existing_item["id"])
                        merged_notes = merge_scheduler_notes(existing_item.get("notes", ""), notes)
                        db.update_scheduler_item(
                            item_id,
                            title=existing_item.get("title") or title,
                            context_label=existing_item.get("context_label") or context_label,
                            scheduled_for=existing_item.get("scheduled_for") or scheduled_for,
                            notes=merged_notes,
                        )
                        applied.append({
                            "op": "update_scheduler_item",
                            "scheduler_item_id": item_id,
                            "title": existing_item.get("title") or title,
                            "context_label": existing_item.get("context_label") or context_label,
                            "scheduled_for": existing_item.get("scheduled_for") or scheduled_for,
                            "notes": merged_notes,
                        })
                    else:
                        item_id = db.add_scheduler_item(
                            title,
                            context_label=context_label,
                            scheduled_for=scheduled_for,
                            notes=notes,
                            source="dieter",
                            project_id=int(project_id) if project_id else None,
                            action_id=int(action_id) if action_id else None,
                        )
                        applied.append({
                            "op": op,
                            "scheduler_item_id": item_id,
                            "title": title,
                            "context_label": context_label,
                            "scheduled_for": scheduled_for,
                            "notes": notes,
                        })
                elif op == "update_scheduler_item":
                    status = operation.get("status")
                    if status is not None and status not in allowed_scheduler_statuses:
                        raise ValueError("Unsupported scheduler status")
                    item_id = int(operation["scheduler_item_id"])
                    existing_item = dict_from_row(db.get_scheduler_item(item_id))
                    notes = operation.get("notes")
                    raw_operation_notes = notes
                    requested_date = scheduler_date_from_text(user_message)
                    existing_date = ((existing_item or {}).get("scheduled_for") or "").strip()
                    if notes is not None:
                        extracted_additions = extract_scheduler_bullet_additions(
                            user_message,
                            operation.get("context_label") or (existing_item or {}).get("context_label") or "",
                        )
                        addition_notes = format_scheduler_note_lines(extracted_additions)
                        notes = merge_scheduler_notes((existing_item or {}).get("notes", ""), addition_notes or notes)
                    if (
                        existing_item
                        and notes is not None
                        and requested_date
                        and existing_date
                        and requested_date != existing_date
                    ):
                        new_title = (operation.get("title") or "").strip() or existing_item.get("context_label") or existing_item.get("title") or "Scheduler item"
                        new_context = (operation.get("context_label") or existing_item.get("context_label") or "").strip()
                        new_notes = addition_notes or normalize_scheduler_notes(raw_operation_notes)
                        item_id = db.add_scheduler_item(
                            new_title,
                            context_label=new_context,
                            scheduled_for=requested_date,
                            notes=new_notes,
                            source="dieter",
                            project_id=existing_item.get("project_id"),
                            action_id=existing_item.get("action_id"),
                        )
                        applied.append({
                            "op": "add_scheduler_item",
                            "scheduler_item_id": item_id,
                            "title": new_title,
                            "context_label": new_context,
                            "scheduled_for": requested_date,
                            "notes": new_notes,
                        })
                        continue
                    if (
                        existing_item
                        and scheduler_list_request(user_message)
                        and not scheduler_request_targets_existing_notes(user_message)
                    ):
                        new_operation = synthesize_scheduler_operation(user_message, {
                            "op": "add_scheduler_item",
                            "title": user_message,
                            "context_label": operation.get("context_label") or existing_item.get("context_label") or infer_scheduler_context_label(user_message),
                            "scheduled_for": requested_date or (operation.get("scheduled_for") or "").strip(),
                            "notes": operation.get("notes") or "",
                            "project_id": existing_item.get("project_id"),
                            "action_id": existing_item.get("action_id"),
                        })
                        title = new_operation.get("title", "").strip()
                        context_label = new_operation.get("context_label", "").strip()
                        scheduled_for = new_operation.get("scheduled_for", "").strip()
                        new_notes = new_operation.get("notes", "").strip()
                        matching_item = find_open_scheduler_item_for_context(context_label, title, scheduled_for) if new_notes else None
                        if matching_item and int(matching_item["id"]) != int(existing_item["id"]):
                            item_id = int(matching_item["id"])
                            merged_notes = merge_scheduler_notes(matching_item.get("notes", ""), new_notes)
                            db.update_scheduler_item(
                                item_id,
                                title=matching_item.get("title") or title,
                                context_label=matching_item.get("context_label") or context_label,
                                scheduled_for=matching_item.get("scheduled_for") or scheduled_for,
                                notes=merged_notes,
                            )
                            applied.append({
                                "op": "update_scheduler_item",
                                "scheduler_item_id": item_id,
                                "title": matching_item.get("title") or title,
                                "context_label": matching_item.get("context_label") or context_label,
                                "scheduled_for": matching_item.get("scheduled_for") or scheduled_for,
                                "notes": merged_notes,
                            })
                        else:
                            item_id = db.add_scheduler_item(
                                title,
                                context_label=context_label,
                                scheduled_for=scheduled_for,
                                notes=new_notes,
                                source="dieter",
                                project_id=existing_item.get("project_id"),
                                action_id=existing_item.get("action_id"),
                            )
                            applied.append({
                                "op": "add_scheduler_item",
                                "scheduler_item_id": item_id,
                                "title": title,
                                "context_label": context_label,
                                "scheduled_for": scheduled_for,
                                "notes": new_notes,
                            })
                        continue
                    db.update_scheduler_item(
                        item_id,
                        title=operation.get("title"),
                        context_label=operation.get("context_label"),
                        scheduled_for=operation.get("scheduled_for"),
                        notes=notes,
                        status=status,
                    )
                    operation["notes"] = notes if notes is not None else operation.get("notes")
                    applied.append(operation)
                elif op == "complete_scheduler_item":
                    db.mark_scheduler_item_complete(int(operation["scheduler_item_id"]))
                    applied.append(operation)
                elif op == "reopen_scheduler_item":
                    db.reopen_scheduler_item(int(operation["scheduler_item_id"]))
                    applied.append(operation)
                else:
                    errors.append(f"Skipped unsupported operation: {op}")
            except Exception as exc:
                errors.append(f"{op or 'operation'} failed: {exc}")

    after = snapshot_planner_target(target)
    summary = proposal.get("summary") or ("Updated planner." if applied else "No planner changes were applied.")
    if applied:
        db.add_planner_change_log(
            after.get("target_kind") or before.get("target_kind") or "planner",
            after.get("target_id") or before.get("target_id") or 0,
            user_message,
            summary,
            applied,
            before,
            after,
            proposal.get("model", ""),
        )

    scheduler_added = [
        operation for operation in applied
        if operation.get("op") == "add_scheduler_item"
    ]
    scheduler_changed = [
        operation for operation in applied
        if operation.get("op") in {
            "add_scheduler_item",
            "update_scheduler_item",
            "complete_scheduler_item",
            "reopen_scheduler_item",
            "delete_scheduler_item",
        }
    ]
    scheduler_confirmation = format_scheduler_confirmation(scheduler_added)
    assistant_message = scheduler_confirmation or proposal.get("assistant_message") or summary
    destination_message = format_planner_write_destinations(applied)
    if destination_message:
        assistant_message = f"{assistant_message}\n{destination_message}" if assistant_message else destination_message
    if errors:
        assistant_message = f"{assistant_message}\nSkipped: {'; '.join(errors)}"

    return {
        "changed_fields": [operation.get("op", "planner_update") for operation in applied],
        "summary": summary,
        "assistant_message": assistant_message,
        "operations": applied,
        "errors": errors,
        "after": after,
        "redirect_url": "/apps/assistant/scheduler" if scheduler_added else "",
        "redirect_label": "View Scheduler" if scheduler_added else "",
        "reload_page": bool(scheduler_changed),
    }

def message_requests_planner_action(text):
    """Detect planner/scheduler commands even when sent from the kitchen drawer."""
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    planner_cues = [
        "remind me",
        "remember to",
        "put this on",
        "add this to",
        "add to my",
        "schedule",
        "scheduler",
        "calendar",
        "appointment",
        "class",
        "pilates",
        "agenda",
        "due tomorrow",
        "for tomorrow",
        "tomorrow",
        "tonight",
        "before bed",
        "bedtime",
        "chore",
        "chores",
        "to do",
        "todo",
        "task",
        "card",
    ]
    if any(cue in normalized for cue in planner_cues):
        return True
    return bool(re.search(r"\b(make|create|add|save)\b.+\b(list|card|task|reminder)\b", normalized))

def message_reports_app_feedback(text):
    """Detect bug reports and UX feedback that should become code-work backlog."""
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    feedback_cues = [
        "bug",
        "broken",
        "doesn't work",
        "does not work",
        "isn't working",
        "not working",
        "wrong",
        "confusing",
        "hard to",
        "can't",
        "cannot",
        "problem",
        "issue",
        "error",
        "missing",
        "should",
        "needs to",
        "need to fix",
        "fix this",
        "developer feedback",
        "feedback for the developer",
        "tell curtis",
        "tell codex",
        "feedback",
    ]
    app_cues = [
        "app",
        "site",
        "website",
        "page",
        "button",
        "image",
        "photo",
        "picture",
        "kitchen",
        "scheduler",
        "planner",
        "trainer",
        "workout",
        "strava",
        "login",
        "guest",
        "dieter",
        "screen",
    ]
    return any(cue in normalized for cue in feedback_cues) and any(cue in normalized for cue in app_cues)

def app_area_from_url(page_url):
    """Infer the app area for a feedback report."""
    if page_url.startswith("/apps/recipes"):
        return "Kitchen / Recipes"
    if page_url.startswith("/apps/assistant/scheduler"):
        return "Scheduler"
    if page_url.startswith("/apps/trainer"):
        return "Trainer"
    if page_url.startswith("/apps/assistant") or page_url.startswith("/apps/planner") or page_url.startswith("/dashboard"):
        return "Assistant / Planner"
    if page_url.startswith("/login") or page_url.startswith("/register"):
        return "Auth"
    return "Dieter"

def get_or_create_app_feedback_project():
    """Return the current user's developer-feedback project."""
    project = dict_from_row(db.get_project_by_name("Dieter App Feedback"))
    if project:
        return project
    project_id = db.add_project(
        "Dieter App Feedback",
        "User-reported app bugs, UX issues, and code work to triage.",
        5,
    )
    return dict_from_row(db.get_project_by_id(project_id))

def share_app_feedback_project_with_admins(project_id):
    """Give admins access to user-reported app feedback."""
    active_user_id = get_current_user_id()
    for admin in dicts_from_rows(db.get_users_by_role("admin")):
        if admin.get("id") == active_user_id:
            continue
        db.share_project(project_id, admin["id"], "edit")

def summarize_app_feedback_title(content, area):
    """Create a concise task title for a feedback report."""
    cleaned = re.sub(r"\s+", " ", (content or "").strip())
    cleaned = re.sub(r"^(please\s+)?(note|save|record)\s+(that\s+)?", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" .")
    if len(cleaned) > 92:
        cleaned = cleaned[:89].rstrip() + "..."
    return f"Fix {area}: {cleaned}" if cleaned else f"Review {area} feedback"

def handle_app_feedback_request(message, page_url):
    """Save app feedback as a developer-ready planner task."""
    user = dict_from_row(db.get_user_by_id(get_current_user_id())) if get_current_user_id() else {}
    reporter = user.get("display_name") or user.get("email") or "Unknown user"
    reporter_email = user.get("email") or ""
    area = app_area_from_url(page_url)
    project = get_or_create_app_feedback_project()
    share_app_feedback_project_with_admins(project["id"])
    action = summarize_app_feedback_title(message.content, area)
    action_id = db.add_recommended_action(project["id"], action, "medium")
    note = "\n".join([
        f"Reporter: {reporter}{f' <{reporter_email}>' if reporter_email and reporter_email != reporter else ''}",
        f"Area: {area}",
        f"Page: {page_url or 'unknown'}",
        f"Source page title: {message.page_title or 'unknown'}",
        "Raw feedback:",
        message.content.strip(),
    ])
    db.add_note(project["id"], note)
    report_id = db.add_app_feedback_report(
        title=action,
        area=area,
        page_url=page_url or "",
        page_title=message.page_title or "",
        reporter_name=reporter,
        reporter_email=reporter_email,
        raw_feedback=message.content.strip(),
        destination_project_id=project["id"],
        destination_action_id=action_id,
    )
    operation = {"op": "add_task", "action_id": action_id}
    return {
        "changed_fields": ["add_task", "add_note", "add_app_feedback_report"],
        "summary": "Saved app feedback.",
        "assistant_message": "\n".join([
            "Saved app feedback for Curtis to implement.",
            f"Project: Dieter App Feedback",
            f"Task: {action}",
            f"Feedback report: #{report_id}",
            f"Reporter: {reporter}",
            f"Page: {page_url or 'unknown'}",
            "Wrote result to: Dieter App Feedback task list (/apps/assistant/planner).",
            "Codex inbox: app_feedback_reports table; export with /api/app-feedback/codex-inbox/save.",
        ]),
        "operations": [
            operation,
            {"op": "add_note", "project_id": project["id"]},
            {"op": "add_app_feedback_report", "report_id": report_id},
        ],
        "app_feedback_context": True,
        "redirect_url": f"/projects/{project['id']}/actions/{action_id}",
        "redirect_label": "Open Feedback Task",
        "reload_page": False,
    }

def handle_planner_action_request(message, page_url):
    """Apply a planner/scheduler request and return the action response shape."""
    proposal = propose_planner_edit(
        message.content,
        page_url=page_url,
        conversation_history=message.conversation_history,
    )
    result = apply_planner_edit(message.content, page_url, proposal)
    result["planner_context"] = True
    return result

def kitchen_cross_app_write_needs_confirmation(page_url):
    """Require preview/confirm for planner writes initiated from the kitchen app."""
    return (page_url or "").startswith("/apps/recipes")

def dieter_action_confirmation_token(content, page_url, action_kind):
    """Create a tamper-resistant confirmation token for one proposed write."""
    user_id = get_current_user_id() or 0
    payload = json.dumps(
        {
            "user_id": user_id,
            "content": content or "",
            "page_url": page_url or "",
            "action_kind": action_kind,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    signature = hmac.new(
        (os.getenv("SECRET_KEY") or REGISTRATION_CODE or "dieter-local-secret").encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{signature}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"

def dieter_action_confirmed(message, page_url, action_kind):
    """Return True when the client confirmed the exact proposed write."""
    expected = dieter_action_confirmation_token(message.content, page_url, action_kind)
    return bool(message.confirmation_token and hmac.compare_digest(message.confirmation_token, expected))

def preview_dieter_write(message, page_url, action_kind, destination):
    """Ask the user to confirm a cross-app write before applying it."""
    token = dieter_action_confirmation_token(message.content, page_url, action_kind)
    lines = [
        "Before I write this, please confirm.",
        f"Requested change: {message.content.strip()}",
        f"Destination: {destination}",
    ]
    if action_kind == "planner_action" and re.search(r"\b(chore|chores|to do|todo|checklist|list)\b|[,;\n]", message.content or "", flags=re.IGNORECASE):
        lines.append("If this is a list, I will save separate bullets as checkboxes where possible.")
    lines.append("Press Confirm to apply it, or edit your message and send again.")
    return {
        "assistant_message": "\n".join(lines),
        "changed_fields": [],
        "needs_confirmation": True,
        "confirmation_token": token,
        "confirmation_action": action_kind,
    }

def summarize_pending_planner_operation(user_message, operation):
    """Describe one proposed planner write before it is applied."""
    op = operation.get("op") or ""
    if op == "add_scheduler_item":
        planned = synthesize_scheduler_operation(user_message, dict(operation))
        lines = ["Plan: add a Scheduler item."]
        lines.append(f"Title: {planned.get('title') or 'Untitled'}")
        lines.append(f"Context: {planned.get('context_label') or 'General'}")
        lines.append(f"When: {planned.get('scheduled_for') or 'no exact date'}")
        if planned.get("notes"):
            lines.append(f"Notes: {planned.get('notes')}")
        return "\n".join(lines)
    if op == "update_scheduler_item":
        item_id = operation.get("scheduler_item_id")
        existing = dict_from_row(db.get_scheduler_item(int(item_id))) if item_id else {}
        if scheduler_list_request(user_message) and not scheduler_request_targets_existing_notes(user_message):
            planned = synthesize_scheduler_operation(user_message, {
                "op": "add_scheduler_item",
                "title": user_message,
                "context_label": operation.get("context_label") or (existing or {}).get("context_label") or infer_scheduler_context_label(user_message),
                "scheduled_for": scheduler_date_from_text(user_message) or (operation.get("scheduled_for") or ""),
                "notes": operation.get("notes") or "",
            })
            lines = ["Plan: create a separate Scheduler checklist instead of merging into an existing card."]
            lines.append(f"Title: {planned.get('title') or 'Untitled'}")
            lines.append(f"Context: {planned.get('context_label') or 'General'}")
            lines.append(f"When: {planned.get('scheduled_for') or 'no exact date'}")
            if planned.get("notes"):
                lines.append(f"Notes: {planned.get('notes')}")
            return "\n".join(lines)
        lines = [f"Plan: update Scheduler item #{item_id}."]
        if existing:
            lines.append(f"Existing title: {existing.get('title') or 'Untitled'}")
        for field in ["title", "context_label", "scheduled_for", "notes", "status"]:
            value = operation.get(field)
            if value is not None:
                lines.append(f"{field}: {value or '(blank)'}")
        return "\n".join(lines)
    if op == "complete_scheduler_item":
        return f"Plan: mark Scheduler item #{operation.get('scheduler_item_id')} done."
    if op == "add_task":
        return f"Plan: add Planner task: {operation.get('action') or 'Untitled task'}."
    if op == "add_step":
        return f"Plan: add checklist step: {operation.get('step') or 'Untitled step'}."
    if op == "add_project":
        return f"Plan: add Planner project: {operation.get('name') or 'Untitled project'}."
    return f"Plan: {op.replace('_', ' ') or 'planner update'}."

def preview_planner_action_write(message, page_url):
    """Preview the concrete planner/scheduler operation before writing."""
    proposal = propose_planner_edit(
        message.content,
        page_url=page_url,
        conversation_history=message.conversation_history,
    )
    token = dieter_action_confirmation_token(message.content, page_url, "planner_action")
    operations = proposal.get("operations") or []
    lines = ["Before I write this, please confirm the action plan."]
    if proposal.get("summary"):
        lines.append(f"Summary: {proposal.get('summary')}")
    if operations:
        for index, operation in enumerate(operations, 1):
            lines.append(f"\n{index}. {summarize_pending_planner_operation(message.content, operation)}")
    else:
        lines.append("Plan: no structured planner write was detected. Edit your message if you expected one.")
    lines.append("\nPress Confirm to apply it, or edit your message and send again.")
    return {
        "assistant_message": "\n".join(lines),
        "changed_fields": [],
        "needs_confirmation": True,
        "confirmation_token": token,
        "confirmation_action": "planner_action",
    }

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

def get_or_create_recipe_app_project():
    """Return this user's private Kitchen backing project."""
    user_id = get_current_user_id()
    project = db.get_project_by_name("Recipe display app")
    if project:
        return project
    if not user_id:
        return None

    project_name = f"Recipe display app ({user_id})"
    project = db.get_project_by_name(project_name)
    if project:
        return project

    try:
        project_id = db.add_project(
            project_name,
            "Private Dieter Kitchen workspace for recipes, meal plans, and grocery lists.",
            3,
        )
    except Exception:
        project = db.get_project_by_name(project_name)
        if project:
            return project
        raise
    db.add_recommended_action(project_id, "Import the first batch of recipe images", "medium")
    return db.get_project_by_id(project_id)

def empty_recipe_app_context(project=None, import_action=None):
    """Return the shared recipe app context shape with optional shell links."""
    import_url = "/apps/recipes/import" if import_action else ""
    return {
        "project": project,
        "import_action": import_action,
        "import_url": import_url,
        "groups": [],
        "complete_meals": [],
        "components": [],
        "component_sections": [],
        "available_grocery_items": [],
        "available_grocery_options": [],
        "meal_plan_items": [],
        "grocery_lists": [],
        "done_grocery_lists": [],
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

def recipe_maintenance_due(now=None):
    """Throttle recipe cleanup work so normal page navigation stays snappy."""
    global recipe_maintenance_last_run
    now = now or datetime.utcnow()
    if (
        recipe_maintenance_last_run
        and (now - recipe_maintenance_last_run).total_seconds() < RECIPE_MAINTENANCE_INTERVAL_SECONDS
    ):
        return False
    recipe_maintenance_last_run = now
    return True

def run_recipe_app_maintenance(force=False):
    """Run maintenance that keeps extracted recipes mirrored into app tables."""
    with recipe_context_lock:
        if not force and not recipe_maintenance_due():
            return
        db.sync_recipe_complete_meals_from_extractions()
        db.cleanup_empty_recipe_placeholders()
        db.cleanup_duplicate_recipes()
        db.share_recipe_library_with_all_users()

def get_recipe_app_context(include_library=True, run_maintenance=True):
    """Resolve planner-backed recipe app links and import status."""
    if run_maintenance:
        run_recipe_app_maintenance()
    project = get_or_create_recipe_app_project()
    if not project:
        return empty_recipe_app_context()

    import_action = db.find_recommended_action(
        project["id"],
        "Import the first batch of recipe images",
    )
    if not import_action:
        return empty_recipe_app_context(project)

    if not include_library:
        return empty_recipe_app_context(project, import_action)

    groups = prepare_recipe_image_groups(db.get_recipe_image_groups(import_action["id"]))
    complete_meals = prepare_recipe_complete_meals(db.get_recipe_complete_meals())
    components = prepare_recipe_components(db.get_recipe_components())
    component_sections = group_recipe_components(components)
    available_grocery_items = build_available_grocery_items(complete_meals, components)
    available_grocery_options = build_available_grocery_options(available_grocery_items)
    meal_plan_items = prepare_meal_plan_items(db.get_recipe_meal_plan_items("pending"))
    for grocery_list in prepare_grocery_lists(db.get_recipe_grocery_lists(50, "active")):
        refresh_grocery_list_completion(grocery_list["id"])
    grocery_lists = annotate_grocery_list_cook_counts(prepare_grocery_lists(db.get_recipe_grocery_lists(8, "active")))
    done_grocery_lists = annotate_grocery_list_cook_counts(prepare_grocery_lists(db.get_recipe_grocery_lists(8, "done")))
    scraped_pairs = sum(1 for group in groups if group.get("extraction_status") == "extracted")
    sections = sum(len(group.get("sections", [])) for group in groups)
    complete_meals_ready = sum(1 for meal in complete_meals if meal.get("status") == "ready")
    complete_meals_needing_review = len(complete_meals) - complete_meals_ready
    return {
        "project": project,
        "import_action": import_action,
        "import_url": "/apps/recipes/import",
        "groups": groups,
        "complete_meals": complete_meals,
        "components": components,
        "component_sections": component_sections,
        "available_grocery_items": available_grocery_items,
        "available_grocery_options": available_grocery_options,
        "meal_plan_items": meal_plan_items,
        "grocery_lists": grocery_lists,
        "done_grocery_lists": done_grocery_lists,
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
- Recipe management page: {recipe_import_url}
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
    recipe_chat = parse_recipe_app_chat_content(message.content)
    if recipe_chat:
        db.add_chat_message("user", recipe_chat["user_message"])
        response = recipe_chat_response(
            recipe_chat["user_message"],
            recipe_chat["page_url"],
            message.conversation_history,
        )
        model = (
            llm_service.provider.model
            if llm_service and hasattr(llm_service.provider, "model")
            else "local-recipe-context"
        )
        db.add_chat_message("assistant", response, model)
        return {
            "response": response,
            "model": model,
            "recipe_context": True,
        }

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

@app.post("/api/recipes/edit")
def api_recipe_edit(message: RecipeEditMessage):
    """Let Dieter edit a recipe from recipe-page chat and keep a structured log."""
    recipe_kind, recipe_id = parse_recipe_target_from_url(message.page_url)
    if not recipe_kind or not recipe_id:
        raise HTTPException(
            status_code=400,
            detail="Open a complete meal or meal component page before asking Dieter to edit a recipe.",
        )

    recipe = recipe_record_for_edit(recipe_kind, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")

    try:
        proposal = propose_recipe_edit(
            recipe_kind,
            recipe,
            message.content,
            conversation_history=message.conversation_history,
        )
    except Exception as exc:
        proposal = {
            "apply_change": False,
            "summary": "Feedback recorded; Dieter could not produce a structured recipe edit.",
            "title": None,
            "ingredients_text": None,
            "instructions_text": None,
            "changed_fields": [],
            "assistant_message": f"I recorded that feedback, but I could not safely amend the recipe automatically: {exc}",
            "model": getattr(llm_service.provider, "model", "") if llm_service else "",
        }

    result = apply_recipe_edit(recipe_kind, recipe_id, message.content, proposal)
    result["recipe_kind"] = recipe_kind
    result["recipe_id"] = recipe_id
    return result

@app.post("/api/dieter/action")
def api_dieter_action(message: DieterActionMessage):
    """Route Ask Dieter requests to recipe edits, planner edits, or contextual chat."""
    page_url = message.page_url or ""
    recipe_kind, recipe_id = parse_recipe_target_from_url(page_url)
    if message_reports_app_feedback(message.content):
        return handle_app_feedback_request(message, page_url)

    if message_requests_playlist_action(message.content, page_url):
        return handle_playlist_action_request(message, page_url)

    if message_requests_trainer_shoe_log(message.content, page_url):
        return handle_trainer_shoe_log_request(message, page_url)

    if message_requests_trainer_reflection(message.content, page_url):
        return handle_trainer_reflection_request(message, page_url)

    if message_requests_planner_action(message.content):
        if not dieter_action_confirmed(message, page_url, "planner_action"):
            return preview_planner_action_write(message, page_url)
        try:
            return handle_planner_action_request(message, page_url)
        except Exception as exc:
            result = agent_service.chat(
                user_message=f"Current page: {message.page_title}\nURL: {page_url}\n\n{message.content}",
                project_context=agent_service.build_dashboard_context(),
                conversation_history=message.conversation_history,
            )
            return {
                "assistant_message": f"{result.get('response', '')}\n\nI could not safely apply a structured planner edit: {exc}",
                "changed_fields": [],
                "planner_context": True,
                "model": "local-planner",
            }

    if recipe_kind and recipe_id:
        recipe_message = RecipeEditMessage(
            content=message.content,
            page_url=page_url,
            conversation_history=message.conversation_history,
        )
        return api_recipe_edit(recipe_message)

    if page_url.startswith("/apps/recipes"):
        response = recipe_chat_response(
            message.content,
            page_url,
            message.conversation_history,
        )
        return {
            "assistant_message": response,
            "changed_fields": [],
            "recipe_context": True,
            "model": (
                llm_service.provider.model
                if llm_service and hasattr(llm_service.provider, "model")
                else "local-recipe-context"
            ),
        }

    if page_url == "/" or re.search(r"/apps/assistant|/apps/planner|/dashboard|/projects|/apps\b", page_url):
        try:
            return handle_planner_action_request(message, page_url)
        except Exception as exc:
            result = agent_service.chat(
                user_message=f"Current page: {message.page_title}\nURL: {page_url}\n\n{message.content}",
                project_context=agent_service.build_dashboard_context(),
                conversation_history=message.conversation_history,
            )
            return {
                "assistant_message": f"{result.get('response', '')}\n\nI could not safely apply a structured planner edit: {exc}",
                "changed_fields": [],
                "planner_context": True,
                "model": "local-planner",
            }

    chat_payload = ChatMessage(
        content="\n".join([
            "Dieter chat request.",
            f"Current page: {message.page_title}",
            f"URL: {page_url}",
            "User message:",
            message.content,
        ]),
        include_context=True,
        conversation_history=message.conversation_history,
    )
    result = api_chat(chat_payload)
    return {
        "assistant_message": result.get("response", "I could not produce a response."),
        "changed_fields": result.get("updates", []),
        "model": result.get("model", ""),
    }


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

def build_app_feedback_codex_inbox(limit=50, status="open"):
    """Build a Markdown inbox of app feedback for Codex."""
    reports = dicts_from_rows(db.get_app_feedback_reports(status=status, limit=limit))
    if not reports:
        return "# Dieter App Feedback Inbox\n\nNo matching feedback reports.\n"
    lines = [
        "# Dieter App Feedback Inbox",
        "",
        f"Status filter: {status or 'all'}",
        "",
    ]
    for report in reports:
        reporter = report.get("reporter_name") or "Unknown"
        if report.get("reporter_email"):
            reporter = f"{reporter} <{report['reporter_email']}>"
        lines.extend([
            f"## #{report['id']} {report['title']}",
            "",
            f"- Status: {report.get('status') or 'open'}",
            f"- Area: {report.get('area') or 'Unknown'}",
            f"- Page: {report.get('page_url') or 'unknown'}",
            f"- Page title: {report.get('page_title') or 'unknown'}",
            f"- Reporter: {reporter}",
            f"- Planner task: {report.get('action_title') or 'not linked'}",
            f"- Created: {report.get('created_at') or 'unknown'}",
            "",
            "Raw feedback:",
            "",
            "```",
            report.get("raw_feedback") or "",
            "```",
            "",
        ])
    return "\n".join(lines)

@app.get("/api/app-feedback/codex-inbox")
def api_app_feedback_codex_inbox(limit: int = 50, status: str = "open"):
    """Return developer feedback as a Markdown Codex inbox."""
    safe_limit = min(max(limit, 1), 100)
    safe_status = status if status in {"open", "triaged", "done", ""} else "open"
    return {"markdown": build_app_feedback_codex_inbox(limit=safe_limit, status=safe_status)}

@app.post("/api/app-feedback/codex-inbox/save")
def api_save_app_feedback_codex_inbox(limit: int = 50, status: str = "open"):
    """Save developer feedback to codex_feedback_inbox.md for local Codex triage."""
    safe_limit = min(max(limit, 1), 100)
    safe_status = status if status in {"open", "triaged", "done", ""} else "open"
    markdown = build_app_feedback_codex_inbox(limit=safe_limit, status=safe_status)
    output_path = Path("codex_feedback_inbox.md").resolve()
    output_path.write_text(markdown, encoding="utf-8")
    return {"status": "success", "path": str(output_path), "markdown": markdown}


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


@app.post("/api/projects/{project_id}/share")
def api_share_project(project_id: int, share: ShareIn, request: Request):
    """Share a project/plan with another user."""
    project = dict_from_row(db.get_project_by_id(project_id))
    current_user = request.state.current_user
    if not project or not current_user or project.get("user_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only the project owner can share this project.")
    target_user = dict_from_row(db.get_user_by_email(share.email))
    if not target_user:
        raise HTTPException(status_code=404, detail="No user with that email exists.")
    permission = share.permission if share.permission in {"view", "edit"} else "view"
    db.share_project(project_id, target_user["id"], permission)
    return {"status": "success", "shared_with": target_user["email"], "permission": permission}


# ============================================================================
# API ROUTES - SCHEDULER
# ============================================================================

@app.get("/api/scheduler")
def api_get_scheduler_items():
    """Get open scheduler/agenda items."""
    return {"scheduler_items": dicts_from_rows(db.get_scheduler_items(status="open", limit=50))}

@app.post("/api/scheduler")
def api_add_scheduler_item(item: SchedulerItemIn):
    """Add a scheduler/agenda item."""
    item_id = db.add_scheduler_item(
        item.title,
        context_label=item.context_label,
        scheduled_for=item.scheduled_for,
        notes=item.notes,
        source="manual",
    )
    return {"status": "success", "scheduler_item_id": item_id}

@app.post("/api/scheduler/{item_id}/complete")
def api_complete_scheduler_item(item_id: int):
    """Mark a scheduler item complete."""
    db.mark_scheduler_item_complete(item_id)
    return {"status": "success"}

@app.post("/api/scheduler/{item_id}/reopen")
def api_reopen_scheduler_item(item_id: int):
    """Reopen a completed scheduler item."""
    db.reopen_scheduler_item(item_id)
    return {"status": "success"}

@app.delete("/api/scheduler/{item_id}")
def api_delete_scheduler_item(item_id: int):
    """Delete a scheduler item."""
    db.delete_scheduler_item(item_id)
    return {"status": "success"}


@app.post("/api/recipes/meals/{meal_id}/share")
def api_share_recipe_meal(meal_id: int, share: ShareIn, request: Request):
    """Share a saved/imported meal with another user."""
    meal = dict_from_row(db.get_recipe_complete_meal(meal_id))
    current_user = request.state.current_user
    if not meal or not current_user or meal.get("user_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only the recipe owner can share this meal.")
    target_user = dict_from_row(db.get_user_by_email(share.email))
    if not target_user:
        raise HTTPException(status_code=404, detail="No user with that email exists.")
    permission = share.permission if share.permission in {"view", "edit"} else "view"
    db.share_recipe("meal", meal_id, target_user["id"], permission)
    return {"status": "success", "shared_with": target_user["email"], "permission": permission}


@app.post("/api/recipes/components/{component_id}/share")
def api_share_recipe_component(component_id: int, share: ShareIn, request: Request):
    """Share a recipe component with another user."""
    component = dict_from_row(db.get_recipe_component(component_id))
    current_user = request.state.current_user
    if not component or not current_user or component.get("user_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only the recipe owner can share this component.")
    target_user = dict_from_row(db.get_user_by_email(share.email))
    if not target_user:
        raise HTTPException(status_code=404, detail="No user with that email exists.")
    permission = share.permission if share.permission in {"view", "edit"} else "view"
    db.share_recipe("component", component_id, target_user["id"], permission)
    return {"status": "success", "shared_with": target_user["email"], "permission": permission}


def share_project_with_email(project_id, email, permission, request):
    project = dict_from_row(db.get_project_by_id(project_id))
    current_user = request.state.current_user
    if not project or not current_user or project.get("user_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only the project owner can share this project.")
    target_user = dict_from_row(db.get_user_by_email(email))
    if not target_user:
        raise HTTPException(status_code=404, detail="No user with that email exists.")
    permission = permission if permission in {"view", "edit"} else "view"
    db.share_project(project_id, target_user["id"], permission)
    return target_user


def share_recipe_with_email(recipe_kind, recipe_id, email, permission, request):
    record = (
        dict_from_row(db.get_recipe_complete_meal(recipe_id))
        if recipe_kind == "meal"
        else dict_from_row(db.get_recipe_component(recipe_id))
    )
    current_user = request.state.current_user
    if not record or not current_user or record.get("user_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only the owner can share this recipe.")
    target_user = dict_from_row(db.get_user_by_email(email))
    if not target_user:
        raise HTTPException(status_code=404, detail="No user with that email exists.")
    permission = permission if permission in {"view", "edit"} else "view"
    db.share_recipe(recipe_kind, recipe_id, target_user["id"], permission)
    return target_user

def safe_redirect_path(path, fallback="/"):
    """Keep form redirects inside this app."""
    return path if path and path.startswith("/") and not path.startswith("//") else fallback

def append_query_param(path, **params):
    """Append query parameters to an internal redirect path."""
    safe_path = safe_redirect_path(path, "/")
    parts = urlsplit(safe_path)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in params.items() if value is not None and value != ""})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


# ============================================================================
# HTML ROUTES (Frontend)
# ============================================================================

@app.get("/login")
def login_page(request: Request):
    """Show login form."""
    if request.state.current_user and not is_guest_user(request.state.current_user):
        return RedirectResponse(url="/", status_code=303)
    return render_auth_page(request, "login")


@app.post("/login")
def login_form(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    """Log a user in."""
    user = db.get_user_by_email(email)
    if not user or not verify_password(password, user["password_hash"]):
        return render_auth_page(request, "login", "Email or password was not recognized.")
    if user["status"] != "active":
        return render_auth_page(request, "login", "This account is not active.")
    safe_next = safe_internal_redirect_target(next)
    return create_login_response(user["id"], safe_next)


@app.post("/guest-login")
def guest_login_form(next: str = Form("/apps/recipes")):
    """Start a read-only guest session."""
    user_id = get_or_create_guest_user_id()
    return create_login_response(user_id, safe_internal_redirect_target(next, "/apps/recipes"))


@app.get("/register")
def register_page(request: Request):
    """Show registration form."""
    if request.state.current_user and not is_guest_user(request.state.current_user):
        return RedirectResponse(url="/", status_code=303)
    return render_auth_page(request, "register")


@app.post("/register")
def register_form(
    request: Request,
    email: str = Form(...),
    display_name: str = Form(""),
    password: str = Form(...),
    registration_code: str = Form(""),
):
    """Create a user account."""
    email = email.strip().lower()
    display_name = display_name.strip() or email.split("@")[0]
    if len(password) < 10:
        return render_auth_page(request, "register", "Use a password with at least 10 characters.")
    if REGISTRATION_CODE and not hmac.compare_digest(registration_code.strip(), REGISTRATION_CODE):
        return render_auth_page(request, "register", "Registration code was not recognized.")
    if db.get_user_by_email(email):
        return render_auth_page(request, "register", "An account with that email already exists.")

    first_user = db.get_user_count() == 0
    role = "admin" if first_user else "user"
    user_id = db.create_user(email, display_name, hash_password(password), role)
    if first_user:
        db.claim_unowned_data(user_id)
    db.share_recipe_library_with_all_users()
    return create_login_response(user_id)


@app.post("/logout")
def logout_form(request: Request):
    """Log the current user out."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        db.delete_session(hash_session_token(token))
    return clear_login_response()


@app.get("/")
def home_default(request: Request):
    """Dieter app launcher."""
    return render_apps_page(request)


@app.get("/dashboard")
def dashboard(request: Request):
    """Legacy planner route."""
    return RedirectResponse(url="/apps/assistant/planner", status_code=303)


@app.get("/apps/planner")
def planner_app(request: Request):
    """Legacy planner route."""
    return RedirectResponse(url="/apps/assistant/planner", status_code=303)


@app.get("/apps/assistant")
def assistant_app(request: Request):
    """Dieter Assistant default page."""
    return RedirectResponse(url="/apps/assistant/planner", status_code=303)


@app.get("/apps/assistant/planner")
def assistant_planner_app(request: Request):
    """Assistant planner page."""
    return render_planner_app(request)


@app.get("/apps/assistant/scheduler")
def assistant_scheduler_app(request: Request):
    """Assistant scheduler page."""
    split_mixed_priority_scheduler_notes()
    data = api_dashboard()
    data_json = json.dumps(data, default=str)
    data_clean = json.loads(data_json)
    context = {
        "request": request,
        "scheduler_items": prepare_scheduler_items_for_display(data_clean["scheduler_items"]),
        "completed_scheduler_items": dicts_from_rows(db.get_recent_completed_scheduler_items(limit=10)),
        "stats": data_clean["stats"],
        "scheduler_due": scheduler_due_context(),
    }
    template = jinja_env.get_template("scheduler.html")
    return HTMLResponse(template.render(context))


def render_planner_app(request: Request):
    """Main dashboard view - renders HTML."""
    split_mixed_priority_scheduler_notes()
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
        "scheduler_items": prepare_scheduler_items_for_display(data_clean["scheduler_items"]),
        "stats": data_clean["stats"],
        "scheduler_due": scheduler_due_context(),
        "recipe_app_url": "/apps/recipes" if recipe_app["project"] else "",
        "recipe_app": recipe_app,
    }
    template = jinja_env.get_template("dashboard.html")
    html = template.render(context)
    return HTMLResponse(html)


@app.get("/apps")
def apps_page(request: Request):
    """Installed/local app launcher."""
    return render_apps_page(request)


def render_apps_page(request: Request):
    """Render the Dieter launcher with direct app entry points."""
    recipe_app = get_recipe_app_context()
    dashboard_context = agent_service.build_dashboard_context()
    context = {
        "request": request,
        "recipe_app_url": "/apps/recipes" if recipe_app["project"] else "",
        "recipe_app": recipe_app,
        "planner_url": "/apps/assistant/planner",
        "trainer_url": "/apps/trainer",
        "playlists_url": "/apps/music/playlists",
        "planner": {
            "recommended_project": dashboard_context.get("recommended_project"),
            "next_action": dashboard_context.get("next_action"),
            "open_actions": len([action for action in dashboard_context.get("actions", []) if not action.get("completed")]),
            "scheduler_items": dashboard_context.get("scheduler_items", []),
        },
        "scheduler_due": scheduler_due_context(),
    }
    template = jinja_env.get_template("apps.html")
    html = template.render(context)
    return HTMLResponse(html)


def trainer_workout_view(row):
    """Prepare a Trainer workout/session row for templates."""
    item = dict(row)
    try:
        item["details"] = json.loads(item.get("details_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        item["details"] = []
    return item

TRAINER_CATEGORY_LABELS = {
    "run_threshold": "Run: Threshold",
    "run_speed": "Run: Speed",
    "bike_tempo": "Bike: Tempo",
    "strength_glutes": "Strength: Glutes",
    "run": "Run",
    "bike": "Bike",
    "strength": "Strength",
}

jinja_env.globals["trainer_category_labels"] = TRAINER_CATEGORY_LABELS


def classify_strava_workout(activity_type, title=""):
    """Map a Strava activity into a Trainer bucket."""
    kind = (activity_type or "").strip().lower()
    text = f"{activity_type or ''} {title or ''}".lower()
    if kind in {"run", "trailrun", "virtualrun"}:
        if re.search(r"\b(threshold|tempo|mile|1k|cruise)\b", text):
            return "run_threshold"
        if re.search(r"\b(400|interval|speed|rep|track|fartlek)\b", text):
            return "run_speed"
        return "run"
    if kind in {"ride", "virtualride", "ebikeride"}:
        return "bike_tempo" if re.search(r"\b(tempo|threshold|interval)\b", text) else "bike"
    if kind in {"weighttraining", "workout", "crossfit", "highintensityintervaltraining"}:
        return "strength_glutes" if re.search(r"\b(glute|clam|rdl|deadlift|band|bridge|hip)\b", text) else "strength"
    return kind or "workout"

def strava_configured():
    """Return true when Strava OAuth credentials are available."""
    return bool(STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET)


def strava_callback_url(request):
    """Build the OAuth callback URL for the current deployment."""
    if STRAVA_REDIRECT_URI:
        return STRAVA_REDIRECT_URI
    return str(request.url_for("trainer_strava_callback"))


def strava_oauth_state(user_id):
    """Create a simple state token tied to the active Dieter user."""
    payload = str(user_id or 0)
    secret = (os.getenv("SECRET_KEY") or REGISTRATION_CODE or "dieter-local-secret").encode("utf-8")
    signature = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def verify_strava_oauth_state(state, user_id):
    """Validate the Strava OAuth state token."""
    return bool(state and hmac.compare_digest(state, strava_oauth_state(user_id)))


def strava_http_json(url, method="GET", data=None, access_token=""):
    """Call Strava and return decoded JSON."""
    body = None
    headers = {"Accept": "application/json"}
    if data is not None:
        body = urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"Strava request failed: {exc.code} {detail[:300]}")
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Strava: {exc.reason}")


def exchange_strava_code(code, request):
    """Exchange a Strava authorization code for tokens."""
    return strava_http_json(
        STRAVA_TOKEN_URL,
        method="POST",
        data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
        },
    )


def refresh_strava_profile_token(profile):
    """Refresh a Strava access token if needed."""
    if not profile or not profile.get("strava_refresh_token"):
        raise HTTPException(status_code=400, detail="Connect Strava before importing runs.")
    expires_at = int(profile.get("strava_token_expires_at") or 0)
    if profile.get("strava_access_token") and expires_at > int(time.time()) + 60:
        return profile.get("strava_access_token")
    payload = strava_http_json(
        STRAVA_TOKEN_URL,
        method="POST",
        data={
            "client_id": STRAVA_CLIENT_ID,
            "client_secret": STRAVA_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": profile.get("strava_refresh_token"),
        },
    )
    db.update_trainer_strava_tokens(
        athlete_id=profile.get("strava_athlete_id"),
        access_token=payload.get("access_token", ""),
        refresh_token=payload.get("refresh_token", profile.get("strava_refresh_token", "")),
        expires_at=payload.get("expires_at", 0),
        scope=profile.get("strava_scope", ""),
    )
    return payload.get("access_token", "")


def fetch_strava_activities(access_token, after_ts, before_ts=None, per_page=100, max_pages=10):
    """Fetch Strava activities for the authenticated athlete."""
    per_page = min(max(per_page, 1), 100)
    activities = []
    for page in range(1, max(max_pages, 1) + 1):
        params = {"after": int(after_ts), "per_page": per_page, "page": page}
        if before_ts:
            params["before"] = int(before_ts)
        url = f"{STRAVA_API_BASE}/athlete/activities?{urlencode(params)}"
        result = strava_http_json(url, access_token=access_token)
        if not isinstance(result, list) or not result:
            break
        activities.extend(result)
        if len(result) < per_page:
            break
    return activities


def fetch_strava_activity_detail(access_token, activity_id):
    """Fetch detailed fields for one Strava activity."""
    if not activity_id:
        return {}
    params = {"include_all_efforts": "true"}
    url = f"{STRAVA_API_BASE}/activities/{activity_id}?{urlencode(params)}"
    result = strava_http_json(url, access_token=access_token)
    return result if isinstance(result, dict) else {}


def strava_first_present(activity, detail, key):
    """Prefer detailed Strava fields, falling back to the summary activity."""
    value = (detail or {}).get(key)
    if value is None or value == "":
        value = (activity or {}).get(key)
    return value


def strava_activity_import_payload(activity, detail=None):
    """Build the persisted metric payload for a Strava activity."""
    detail = detail or {}
    merged = {**(activity or {}), **detail}
    activity_type = merged.get("sport_type") or merged.get("type") or ""
    title = merged.get("name") or activity_type
    gear = merged.get("gear") if isinstance(merged.get("gear"), dict) else {}
    return {
        "external_id": str(merged.get("id")),
        "activity_type": activity_type,
        "workout_category": classify_strava_workout(activity_type, title),
        "title": title,
        "started_at": (merged.get("start_date_local") or merged.get("start_date") or "")[:10],
        "distance_meters": strava_first_present(activity, detail, "distance"),
        "moving_time_seconds": strava_first_present(activity, detail, "moving_time"),
        "elapsed_time_seconds": strava_first_present(activity, detail, "elapsed_time"),
        "elevation_gain_meters": strava_first_present(activity, detail, "total_elevation_gain"),
        "average_speed_mps": strava_first_present(activity, detail, "average_speed"),
        "max_speed_mps": strava_first_present(activity, detail, "max_speed"),
        "average_heartrate": strava_first_present(activity, detail, "average_heartrate"),
        "max_heartrate": strava_first_present(activity, detail, "max_heartrate"),
        "average_cadence": strava_first_present(activity, detail, "average_cadence"),
        "average_watts": strava_first_present(activity, detail, "average_watts"),
        "kilojoules": strava_first_present(activity, detail, "kilojoules"),
        "suffer_score": strava_first_present(activity, detail, "suffer_score"),
        "perceived_exertion": strava_first_present(activity, detail, "perceived_exertion"),
        "gear_id": strava_first_present(activity, detail, "gear_id") or gear.get("id") or "",
        "gear_name": gear.get("name") or "",
        "start_latlng": strava_first_present(activity, detail, "start_latlng") or [],
        "end_latlng": strava_first_present(activity, detail, "end_latlng") or [],
        "splits_metric": detail.get("splits_metric") or [],
        "laps": detail.get("laps") or [],
        "raw": {"summary": activity or {}, "detail": detail or {}},
    }


def strava_activity_is_supported_training(activity_type):
    """Return true for Strava activities Trainer currently summarizes."""
    normalized = (activity_type or "").strip()
    return normalized in {"Run", "TrailRun", "VirtualRun", "Ride", "VirtualRide", "EBikeRide"}


def import_recent_strava_runs(days=7):
    """Import recent Strava run and bike activities for the active athlete."""
    profile = dict_from_row(db.get_trainer_profile())
    if not strava_configured():
        raise HTTPException(status_code=400, detail="Strava is not configured. Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET.")
    access_token = refresh_strava_profile_token(profile)
    now_ts = int(time.time())
    after_ts = now_ts - int(days * 86400)
    activities = fetch_strava_activities(access_token, after_ts=after_ts, before_ts=now_ts)
    imported = 0
    skipped = 0
    for activity in activities:
        activity_type = activity.get("type") or activity.get("sport_type") or ""
        if not strava_activity_is_supported_training(activity_type):
            skipped += 1
            continue
        try:
            detail = fetch_strava_activity_detail(access_token, activity.get("id"))
        except HTTPException:
            detail = {}
        db.add_trainer_imported_workout(**strava_activity_import_payload(activity, detail))
        imported += 1
    return {"imported": imported, "skipped": skipped, "days": days}


def import_single_strava_activity(activity_id):
    """Import one Strava activity by id for the active athlete."""
    profile = dict_from_row(db.get_trainer_profile())
    if not strava_configured():
        raise HTTPException(status_code=400, detail="Strava is not configured. Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET.")
    access_token = refresh_strava_profile_token(profile)
    detail = fetch_strava_activity_detail(access_token, activity_id)
    activity_type = detail.get("type") or detail.get("sport_type") or ""
    if not strava_activity_is_supported_training(activity_type):
        raise HTTPException(status_code=400, detail="That Strava activity is not a supported run or bike activity.")
    db.add_trainer_imported_workout(**strava_activity_import_payload(detail, detail))
    return {"imported": 1, "skipped": 0, "activity_id": activity_id}

def parse_trainer_reflection_target(page_url):
    """Find an imported workout id from the Trainer page URL."""
    try:
        params = dict(parse_qsl(urlsplit(page_url or "").query, keep_blank_values=True))
        target = params.get("reflection_workout_id") or params.get("workout_id")
        return int(target) if target and str(target).isdigit() else None
    except (TypeError, ValueError):
        return None


def parse_trainer_import_target(page_url):
    """Find an imported workout id for opening Trainer run detail."""
    try:
        params = dict(parse_qsl(urlsplit(page_url or "").query, keep_blank_values=True))
        target = params.get("workout_id") or params.get("reflection_workout_id") or params.get("shoe_workout_id")
        return int(target) if target and str(target).isdigit() else None
    except (TypeError, ValueError):
        return None


def parse_trainer_shoe_target(page_url):
    """Find an imported workout id for shoe logging from the Trainer page URL."""
    try:
        params = dict(parse_qsl(urlsplit(page_url or "").query, keep_blank_values=True))
        target = params.get("shoe_workout_id") or params.get("workout_id")
        return int(target) if target and str(target).isdigit() else None
    except (TypeError, ValueError):
        return None


def message_requests_trainer_shoe_log(text, page_url=""):
    """Detect Ask Dieter requests for run shoe logging."""
    if not (page_url or "").startswith("/apps/trainer"):
        return False
    if parse_trainer_shoe_target(page_url):
        return True
    normalized = (text or "").lower()
    shoe_words = any(cue in normalized for cue in ["shoe", "shoes", "spike", "spikes", "trainers", "flats"])
    log_words = any(cue in normalized for cue in ["log", "track", "mileage", "wore", "wearing", "used", "ran in", "warmup in", "workout in"])
    return shoe_words and log_words


def trainer_shoe_prompt(workout):
    """Ask for shoe usage details on an imported run."""
    title = workout.get("title") if workout else "that run"
    date = workout.get("started_at") if workout else ""
    shoes = dicts_from_rows(db.get_trainer_shoes())
    shoe_names = ", ".join(shoe["name"] for shoe in shoes) or "no saved shoes yet"
    return "\n".join([
        f"Which shoes did you use for {title}{f' ({date})' if date else ''}?",
        f"Saved shoes: {shoe_names}.",
        "You can answer like: trainers 2 miles warmup, spikes 4 miles workout.",
    ])


def extract_trainer_shoe_segments(text, shoes, default_distance_meters=None):
    """Extract shoe usage segments from simple free text."""
    normalized = (text or "").lower()
    segments = []
    for shoe in shoes:
        name = (shoe.get("name") or "").strip()
        if not name:
            continue
        name_pattern = re.escape(name.lower())
        if not re.search(rf"\b{name_pattern}\b", normalized):
            continue
        window_match = re.search(rf"(.{{0,35}}\b{name_pattern}\b.{{0,45}})", normalized)
        window = window_match.group(1) if window_match else normalized
        miles_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:mi|mile|miles)\b", window)
        segment_label = ""
        for label in ["warmup", "warm-up", "workout", "cooldown", "cool-down", "race", "strides", "all"]:
            if label in window:
                segment_label = label.replace("-", "")
                break
        distance_meters = miles_to_meters(miles_match.group(1)) if miles_match else default_distance_meters
        segments.append({
            "shoe_id": shoe["id"],
            "shoe_name": name,
            "segment_label": segment_label,
            "distance_meters": distance_meters,
        })
    return segments


def handle_trainer_shoe_log_request(message, page_url):
    """Prompt for or save shoe usage against an imported run."""
    target_id = parse_trainer_shoe_target(page_url) or parse_trainer_reflection_target(page_url)
    workout = dict_from_row(db.get_trainer_imported_workout(target_id)) if target_id else None
    if not workout:
        workout = dict_from_row(db.get_latest_trainer_imported_workout())
    if not workout:
        return {
            "assistant_message": "I do not see an imported run yet. Pull your Strava runs first, then I can log shoes.",
            "changed_fields": [],
            "trainer_context": True,
        }
    if workout.get("user_id") != get_current_user_id():
        return {
            "assistant_message": "Only the athlete can log shoe mileage on their own runs.",
            "changed_fields": [],
            "trainer_context": True,
        }
    shoes = dicts_from_rows(db.get_trainer_shoes())
    if not shoes:
        return {
            "assistant_message": "Add at least one shoe in Trainer settings first, then I can log mileage against it.",
            "changed_fields": [],
            "trainer_context": True,
            "redirect_url": "/apps/trainer/settings",
            "redirect_label": "Open Shoe Inventory",
        }
    segments = extract_trainer_shoe_segments(message.content, shoes, workout.get("distance_meters"))
    if not segments:
        return {
            "assistant_message": trainer_shoe_prompt(workout),
            "changed_fields": [],
            "trainer_context": True,
            "redirect_url": f"/apps/trainer/imports?shoe_workout_id={workout['id']}",
            "redirect_label": "Open Shoe Log",
        }
    saved = []
    for segment in segments:
        usage_id = db.add_trainer_workout_shoe(
            workout["id"],
            segment["shoe_id"],
            segment_label=segment["segment_label"],
            distance_meters=segment["distance_meters"],
            notes=message.content.strip(),
        )
        if usage_id:
            miles = (segment["distance_meters"] or 0) / 1609.344 if segment["distance_meters"] else 0
            miles_text = f" {miles:.1f} mi" if miles else ""
            segment_text = f" ({segment['segment_label']})" if segment["segment_label"] else ""
            saved.append(f"{segment['shoe_name']}{miles_text}{segment_text}")
    scan_trainer_audit_insights(user_id=workout.get("user_id"))
    return {
        "assistant_message": f"Saved shoe usage for {workout.get('title') or 'this run'}: {', '.join(saved)}.",
        "changed_fields": ["trainer_workout_shoes"],
        "trainer_context": True,
        "redirect_url": f"/apps/trainer/imports?shoe_workout_id={workout['id']}",
        "redirect_label": "Open Shoe Log",
    }


def message_requests_trainer_reflection(text, page_url=""):
    """Detect Ask Dieter requests that should become run reflections."""
    if not (page_url or "").startswith("/apps/trainer"):
        return False
    if parse_trainer_reflection_target(page_url):
        return True
    normalized = (text or "").lower()
    cues = [
        "how the run went",
        "run went",
        "felt",
        "rpe",
        "legs",
        "sore",
        "pain",
        "ache",
        "inserts",
        "shoes",
        "bike",
        "sleep",
        "fuel",
        "hydration",
        "reflect",
        "reflection",
        "post-run",
        "post run",
    ]
    return any(cue in normalized for cue in cues)


def extract_trainer_reflection_fields(text):
    """Extract lightweight subjective run-note fields from free text."""
    source = text or ""
    normalized = source.lower()
    rpe = None
    rpe_match = re.search(r"\b(?:rpe|effort|felt like)?\s*(10|[1-9])\s*(?:/10)?\b", normalized)
    if rpe_match:
        rpe = int(rpe_match.group(1))

    feel = ""
    for label, keywords in [
        ("great", ["great", "excellent", "amazing", "smooth"]),
        ("good", ["good", "solid", "fine"]),
        ("normal", ["normal", "okay", "ok", "average"]),
        ("flat", ["flat", "sluggish", "heavy", "tired"]),
        ("bad", ["bad", "rough", "awful", "terrible", "poor"]),
    ]:
        if any(word in normalized for word in keywords):
            feel = label
            break

    body_keywords = [
        "foot", "feet", "arch", "calf", "calves", "achilles", "knee", "hip", "back",
        "hamstring", "quad", "glute", "ankle", "shin", "plantar",
    ]
    context_keywords = [
        "bike", "biked", "cycling", "inserts", "insert", "shoes", "shoe",
        "sleep", "stress", "sick", "illness", "fuel", "fueling", "hydration",
        "dehydrated", "heat", "hot", "travel", "work",
    ]
    body_flags = sorted({word for word in body_keywords if re.search(rf"\b{re.escape(word)}s?\b", normalized)})
    context_flags = sorted({word for word in context_keywords if re.search(rf"\b{re.escape(word)}\b", normalized)})

    missing = []
    if rpe is None:
        missing.append("RPE 1-10")
    if not feel:
        missing.append("how it felt")
    if not body_flags:
        missing.append("body/leg symptoms")
    if not context_flags:
        missing.append("context like bike load, inserts, shoes, sleep, fuel, stress")

    return {
        "rpe": rpe,
        "feel": feel,
        "body_flags": body_flags,
        "context_flags": context_flags,
        "missing": missing,
    }


def trainer_reflection_prompt(workout, missing=None):
    """Ask for enough detail to save a useful run reflection."""
    title = workout.get("title") if workout else "that run"
    date = workout.get("started_at") if workout else ""
    missing = missing or []
    questions = [
        "RPE 1-10?",
        "How did it feel overall?",
        "Anything notable in legs/feet/body?",
        "Any context: bike load, new inserts/shoes, sleep, fueling, stress, weather?",
    ]
    if missing:
        questions = [q for q in questions if any(key.lower() in q.lower() for key in missing)] or questions
    return "\n".join([
        f"Let's capture notes for {title}{f' ({date})' if date else ''}.",
        "Answer in a sentence or two:",
        *[f"- {question}" for question in questions],
    ])


def trainer_recent_load_context(user_id=None, weeks=4):
    """Return a compact recent Strava load summary for Ask Dieter prompts."""
    summaries = [
        trainer_weekly_summary_view(row)
        for row in db.get_trainer_weekly_run_summaries(user_id=user_id, weeks=weeks)
    ]
    lines = []
    for week in summaries:
        parts = [
            week.get("week_label") or "week",
            f"{int(week.get('run_count') or 0)} runs",
        ]
        if week.get("distance_miles"):
            parts.append(f"{week['distance_miles']:.1f} mi")
        if week.get("moving_time_hours"):
            parts.append(f"{week['moving_time_hours']:.1f} hr")
        if week.get("load_score"):
            parts.append(f"load {week['load_score']:.0f}")
        lines.append(" - ".join(parts))
    return "\n".join(lines)


def handle_trainer_reflection_request(message, page_url):
    """Prompt for or save subjective notes about an imported run."""
    target_id = parse_trainer_reflection_target(page_url)
    workout = dict_from_row(db.get_trainer_imported_workout(target_id)) if target_id else None
    if not workout:
        workout = dict_from_row(db.get_latest_trainer_imported_workout())
    if not workout:
        return {
            "assistant_message": "I do not see an imported run yet. Pull your Strava runs first, then I can ask how one went.",
            "changed_fields": [],
            "trainer_context": True,
        }
    if workout.get("user_id") != get_current_user_id():
        return {
            "assistant_message": "I can view shared athlete notes, but only the athlete can add reflections to their own run.",
            "changed_fields": [],
            "trainer_context": True,
        }

    fields = extract_trainer_reflection_fields(message.content)
    enough_to_save = fields["rpe"] is not None and (fields["feel"] or fields["body_flags"] or fields["context_flags"])
    if not enough_to_save:
        recent_load = trainer_recent_load_context(workout.get("user_id"))
        prompt = trainer_reflection_prompt(workout, fields["missing"])
        if recent_load:
            prompt = f"{prompt}\n\nRecent Strava load:\n{recent_load}"
        return {
            "assistant_message": prompt,
            "changed_fields": [],
            "trainer_context": True,
            "redirect_url": f"/apps/trainer/imports?reflection_workout_id={workout['id']}",
            "redirect_label": "Open Run Reflection",
        }

    reflection_id = db.add_trainer_run_reflection(
        workout["id"],
        rpe=fields["rpe"],
        feel=fields["feel"],
        body_flags=fields["body_flags"],
        context_flags=fields["context_flags"],
        notes=message.content.strip(),
        missing_fields=fields["missing"],
    )
    scan_trainer_audit_insights(user_id=workout.get("user_id"))
    missing_note = f"\nMissing detail for later: {', '.join(fields['missing'])}." if fields["missing"] else ""
    return {
        "assistant_message": "\n".join([
            f"Saved reflection #{reflection_id} for {workout.get('title') or 'this run'}.",
            f"RPE: {fields['rpe']}/10",
            f"Feel: {fields['feel'] or 'not specified'}",
            f"Body flags: {', '.join(fields['body_flags']) or 'none noted'}",
            f"Context flags: {', '.join(fields['context_flags']) or 'none noted'}",
            missing_note.strip(),
        ]).strip(),
        "changed_fields": ["trainer_run_reflection"],
        "trainer_context": True,
        "redirect_url": f"/apps/trainer/imports?reflection_workout_id={workout['id']}",
        "redirect_label": "Open Run Reflection",
    }


def trainer_grouped_suggestions():
    """Return grouped Trainer catalog suggestions for the home page."""
    grouped = db.get_trainer_suggested_workouts_by_category()
    return [
        {
            "category": category,
            "label": TRAINER_CATEGORY_LABELS.get(category, category.replace("_", " ").title()),
            "workouts": [trainer_workout_view(row) for row in rows],
        }
        for category, rows in grouped.items()
    ]


def trainer_weekly_summary_view(row):
    """Format an imported Strava weekly summary for Trainer views."""
    item = dict(row)
    distance_meters = float(item.get("distance_meters") or 0)
    moving_time_seconds = int(item.get("moving_time_seconds") or 0)
    average_speed_mps = float(item.get("average_speed_mps") or 0)
    suffer_score = float(item.get("suffer_score") or 0)
    average_hr = float(item.get("average_heartrate") or 0)
    minutes = moving_time_seconds / 60 if moving_time_seconds else 0
    fallback_load = (minutes * average_hr / 100) if minutes and average_hr else 0
    item["distance_miles"] = distance_meters / 1609.344 if distance_meters else 0
    item["moving_time_hours"] = moving_time_seconds / 3600 if moving_time_seconds else 0
    item["average_pace_min_per_mile"] = 26.8224 / average_speed_mps if average_speed_mps else 0
    item["load_score"] = suffer_score or fallback_load
    return item


def trainer_week_offset(request):
    """Read the selected week offset from the request."""
    try:
        return max(int(request.query_params.get("week_offset", 0)), 0)
    except (TypeError, ValueError):
        return 0


def parse_trainer_started_date(value):
    """Parse an imported workout date from Strava/local storage."""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw[:10]):
            return datetime.strptime(raw[:10], "%Y-%m-%d").date()
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def trainer_row_is_run(row):
    """Return true for imported activities that should count toward run mileage."""
    activity_type = (row.get("activity_type") or "").strip().lower()
    category = (row.get("workout_category") or "").strip().lower()
    return activity_type in {"run", "trailrun", "virtualrun"} or category.startswith("run")


def trainer_row_is_bike(row):
    """Return true for imported activities that should count toward bike mileage."""
    activity_type = (row.get("activity_type") or "").strip().lower()
    category = (row.get("workout_category") or "").strip().lower()
    return activity_type in {"ride", "virtualride", "ebikeride"} or category.startswith("bike")


def trainer_workout_week_dashboard(user_id=None, week_offset=0):
    """Build a dashboard summary for one training week."""
    today = datetime.now().date()
    week_start = today - timedelta(days=today.weekday()) - timedelta(days=7 * max(week_offset, 0))
    week_end = week_start + timedelta(days=6)
    runs = []
    rides = []
    for row in dicts_from_rows(db.get_trainer_imported_workouts(user_id=user_id, limit=500)):
        is_run = trainer_row_is_run(row)
        is_bike = trainer_row_is_bike(row)
        if not is_run and not is_bike:
            continue
        started = parse_trainer_started_date(row.get("started_at"))
        if not started:
            continue
        if week_start <= started <= week_end:
            item = dict(row)
            distance_meters = float(item.get("distance_meters") or 0)
            moving_time_seconds = int(item.get("moving_time_seconds") or 0)
            average_speed_mps = float(item.get("average_speed_mps") or 0)
            item["distance_miles"] = distance_meters / 1609.344 if distance_meters else 0
            item["moving_time_minutes"] = moving_time_seconds / 60 if moving_time_seconds else 0
            item["average_pace_min_per_mile"] = 26.8224 / average_speed_mps if average_speed_mps else 0
            if is_run:
                runs.append(item)
            elif is_bike:
                rides.append(item)

    runs.sort(key=lambda item: item.get("started_at") or "", reverse=True)
    rides.sort(key=lambda item: item.get("started_at") or "", reverse=True)
    total_distance = sum(float(run.get("distance_miles") or 0) for run in runs)
    total_bike_distance = sum(float(ride.get("distance_miles") or 0) for ride in rides)
    total_seconds = sum(int(run.get("moving_time_seconds") or 0) for run in runs)
    total_gain = sum(float(run.get("elevation_gain_meters") or 0) for run in runs)
    total_load = sum(float(run.get("suffer_score") or 0) for run in runs)
    hr_values = [float(run.get("average_heartrate") or 0) for run in runs if run.get("average_heartrate")]
    sessions = [
        session
        for session in [trainer_workout_view(row) for row in db.get_trainer_sessions("upcoming", limit=80, user_id=user_id)]
        if session.get("scheduled_for") and week_start.isoformat() <= session.get("scheduled_for") <= week_end.isoformat()
    ]
    return {
        "week_offset": week_offset,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "label": "This week" if week_offset == 0 else f"{week_offset} week{'s' if week_offset != 1 else ''} ago",
        "next_offset": max(week_offset - 1, 0),
        "previous_offset": week_offset + 1,
        "runs": runs,
        "rides": rides,
        "run_count": len(runs),
        "distance_miles": total_distance,
        "ride_count": len(rides),
        "bike_distance_miles": total_bike_distance,
        "moving_time_hours": total_seconds / 3600 if total_seconds else 0,
        "elevation_gain_meters": total_gain,
        "load_score": total_load,
        "average_heartrate": (sum(hr_values) / len(hr_values)) if hr_values else 0,
        "scheduled_sessions": sessions,
    }


def trainer_weekly_mileage_chart(user_id=None, weeks=13):
    """Build chronological weekly run/bike mileage for the last several months."""
    today = datetime.now().date()
    current_week_start = today - timedelta(days=today.weekday())
    buckets = []
    by_start = {}
    for index in range(max(weeks, 1) - 1, -1, -1):
        week_start = current_week_start - timedelta(days=7 * index)
        item = {
            "week_start": week_start.isoformat(),
            "week_end": (week_start + timedelta(days=6)).isoformat(),
            "label": week_start.strftime("%b %-d") if os.name != "nt" else week_start.strftime("%b %#d"),
            "distance_miles": 0,
            "bike_distance_miles": 0,
            "run_count": 0,
            "ride_count": 0,
        }
        buckets.append(item)
        by_start[week_start] = item

    earliest = buckets[0]["week_start"]
    for row in dicts_from_rows(db.get_trainer_imported_workouts(user_id=user_id, limit=5000)):
        is_run = trainer_row_is_run(row)
        is_bike = trainer_row_is_bike(row)
        if not is_run and not is_bike:
            continue
        started = parse_trainer_started_date(row.get("started_at"))
        if not started:
            continue
        if started.isoformat() < earliest:
            continue
        week_start = started - timedelta(days=started.weekday())
        bucket = by_start.get(week_start)
        if not bucket:
            continue
        miles = float(row.get("distance_meters") or 0) / 1609.344
        if is_run:
            bucket["distance_miles"] += miles
            bucket["run_count"] += 1
        elif is_bike:
            bucket["bike_distance_miles"] += miles
            bucket["ride_count"] += 1

    max_miles = max(
        [bucket["distance_miles"] for bucket in buckets] +
        [bucket["bike_distance_miles"] for bucket in buckets] +
        [0]
    )
    chart_width = 560
    chart_height = 220
    left = 42
    right = 14
    top = 14
    bottom = 32
    plot_width = chart_width - left - right
    plot_height = chart_height - top - bottom
    scale_max = max_miles or 1
    for index, bucket in enumerate(buckets):
        x = left + (plot_width * index / max(len(buckets) - 1, 1))
        run_y = top + (1 - (bucket["distance_miles"] / scale_max)) * plot_height
        bike_y = top + (1 - (bucket["bike_distance_miles"] / scale_max)) * plot_height
        bucket["x"] = round(x, 1)
        bucket["run_y"] = round(run_y, 1)
        bucket["bike_y"] = round(bike_y, 1)
    run_points = " ".join(f"{bucket['x']},{bucket['run_y']}" for bucket in buckets)
    bike_points = " ".join(f"{bucket['x']},{bucket['bike_y']}" for bucket in buckets)
    axis_values = [scale_max, scale_max / 2, 0]
    axis_labels = []
    for value in axis_values:
        y = top + (1 - (value / scale_max)) * plot_height
        axis_labels.append({"value": value, "y": round(y, 1), "label": f"{value:.0f}"})
    return {
        "weeks": buckets,
        "max_miles": max_miles,
        "total_miles": sum(bucket["distance_miles"] for bucket in buckets),
        "total_bike_miles": sum(bucket["bike_distance_miles"] for bucket in buckets),
        "run_points": run_points,
        "bike_points": bike_points,
        "axis_labels": axis_labels,
        "chart_width": chart_width,
        "chart_height": chart_height,
        "plot_left": left,
        "plot_right": chart_width - right,
        "plot_top": top,
        "plot_bottom": chart_height - bottom,
    }


def trainer_weekly_workout_plan():
    """Pick a compact weekly menu from the Trainer catalog."""
    grouped = db.get_trainer_suggested_workouts_by_category(limit_per_category=2)
    plan_order = [
        ("run_threshold", "Threshold"),
        ("run_speed", "Speed"),
        ("strength_glutes", "Strength"),
        ("bike_tempo", "Bike"),
    ]
    plan = []
    for category, label in plan_order:
        rows = grouped.get(category) or []
        if rows:
            plan.append({
                "category": category,
                "label": label,
                "workout": trainer_workout_view(rows[0]),
            })
    return plan


def trainer_shoe_usage_by_workout(user_id):
    """Group shoe usage rows by imported workout id for templates."""
    grouped = {}
    for row in dicts_from_rows(db.get_trainer_workout_shoes(user_id=user_id, limit=500)):
        grouped.setdefault(row["imported_workout_id"], []).append(row)
    return grouped


def miles_to_meters(value):
    """Convert a form mileage value to meters."""
    try:
        miles = float(value)
    except (TypeError, ValueError):
        return None
    return max(miles, 0) * 1609.344


def parse_json_list(value):
    """Parse a JSON list column defensively."""
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def trainer_bad_outcome(row):
    """Classify a reflection as a bad/rough workout outcome."""
    feel = (row.get("feel") or "").lower()
    notes = (row.get("notes") or "").lower()
    rpe = row.get("rpe")
    rough_words = ["bad", "rough", "awful", "terrible", "flat", "sluggish", "heavy", "pain", "ache", "sore", "tired"]
    return bool((rpe is not None and int(rpe or 0) >= 8) or feel in {"bad", "flat"} or any(word in notes for word in rough_words))


def trainer_audit_signals(row):
    """Build auditable candidate signals from one reflected run."""
    signals = []
    for shoe in (row.get("shoe_names") or "").split(","):
        shoe = shoe.strip()
        if shoe:
            signals.append(("shoe", shoe))
    for flag in parse_json_list(row.get("context_flags_json")):
        signals.append(("context", str(flag)))
    for flag in parse_json_list(row.get("body_flags_json")):
        signals.append(("body", str(flag)))
    title = (row.get("workout_title") or "").lower()
    category = row.get("workout_category") or ""
    if category:
        signals.append(("workout_type", category))
    distance_miles = float(row.get("distance_meters") or 0) / 1609.344
    elevation_meters = float(row.get("elevation_gain_meters") or 0)
    if "hill" in title or "hills" in title or (distance_miles and elevation_meters / distance_miles >= 30):
        signals.append(("terrain", "hilly/elevation"))
    if float(row.get("suffer_score") or 0) >= 80:
        signals.append(("load", "high Strava suffer score"))
    if float(row.get("average_heartrate") or 0) >= 165:
        signals.append(("load", "high average HR"))
    return signals


def scan_trainer_audit_insights(user_id=None, minimum_total=2, minimum_bad=2):
    """Scan logged Trainer data for auditable rough-workout patterns."""
    target_user_id = user_id or get_current_user_id()
    rows = [dict(row) for row in db.get_trainer_audit_rows(user_id=target_user_id, limit=500)]
    buckets = {}
    for row in rows:
        bad = trainer_bad_outcome(row)
        for signal_type, signal_name in trainer_audit_signals(row):
            key = (signal_type, signal_name)
            bucket = buckets.setdefault(key, {"bad": 0, "total": 0, "evidence": []})
            bucket["total"] += 1
            if bad:
                bucket["bad"] += 1
                bucket["evidence"].append({
                    "reflection_id": row.get("reflection_id"),
                    "workout_id": row.get("imported_workout_id"),
                    "title": row.get("workout_title"),
                    "date": row.get("started_at"),
                    "rpe": row.get("rpe"),
                    "feel": row.get("feel"),
                    "notes": (row.get("notes") or "")[:180],
                })

    saved = []
    for (signal_type, signal_name), bucket in buckets.items():
        total = bucket["total"]
        bad_count = bucket["bad"]
        if total < minimum_total or bad_count < minimum_bad:
            continue
        bad_rate = bad_count / total if total else 0
        if bad_rate < 0.5:
            continue
        summary = f"{signal_name} appears in {bad_count}/{total} rough logged run reflections."
        db.upsert_trainer_audit_insight(
            signal_type,
            signal_name,
            bad_count,
            total,
            bad_rate,
            summary,
            bucket["evidence"][:6],
            user_id=target_user_id,
        )
        saved.append({"signal_type": signal_type, "signal_name": signal_name, "bad_count": bad_count, "total_count": total, "bad_rate": bad_rate})
    return saved


def trainer_context(request, active_tab="home", workout_type="", athlete_user_id=None):
    """Build shared Dieter Trainer template context."""
    current_user = request.state.current_user
    selected_athlete_id = athlete_user_id or (current_user or {}).get("id")
    week_offset = trainer_week_offset(request)
    workouts = [trainer_workout_view(row) for row in db.get_trainer_workouts(workout_type)]
    trainer_profile = dict_from_row(db.get_trainer_profile())
    import_target_id = parse_trainer_import_target(str(request.url)) if request else None
    reflection_target_id = parse_trainer_reflection_target(str(request.url)) if request else None
    reflection_target = dict_from_row(db.get_trainer_imported_workout(reflection_target_id)) if reflection_target_id else None
    shoe_target_id = parse_trainer_shoe_target(str(request.url)) if request else None
    shoe_target = dict_from_row(db.get_trainer_imported_workout(shoe_target_id)) if shoe_target_id else None
    return {
        "request": request,
        "active_tab": active_tab,
        "workout_type": workout_type,
        "trainer_profile": trainer_profile,
        "strava_configured": strava_configured(),
        "strava_connected": bool(trainer_profile and trainer_profile.get("strava_refresh_token")),
        "reflection_target": reflection_target,
        "shoe_target": shoe_target,
        "coach_grants": dicts_from_rows(db.get_trainer_coach_grants_for_athlete()),
        "coach_athletes": dicts_from_rows(db.get_trainer_athletes_for_coach()),
        "current_user_id": (current_user or {}).get("id"),
        "selected_athlete_id": selected_athlete_id,
        "is_viewing_own_trainer": selected_athlete_id == (current_user or {}).get("id"),
        "suggestion_groups": trainer_grouped_suggestions(),
        "weekly_workout_plan": trainer_weekly_workout_plan(),
        "week_dashboard": trainer_workout_week_dashboard(selected_athlete_id, week_offset),
        "mileage_chart": trainer_weekly_mileage_chart(selected_athlete_id, weeks=13),
        "workouts": workouts,
        "run_workouts": [item for item in workouts if item["workout_type"] == "run"],
        "bike_workouts": [item for item in workouts if item["workout_type"] == "bike"],
        "strength_workouts": [item for item in workouts if item["workout_type"] == "strength"],
        "upcoming_sessions": [trainer_workout_view(row) for row in db.get_trainer_sessions("upcoming", limit=40, user_id=selected_athlete_id)],
        "past_sessions": [trainer_workout_view(row) for row in db.get_trainer_sessions("done", limit=40, user_id=selected_athlete_id)],
        "imported_workouts": dicts_from_rows(db.get_trainer_imported_workouts(selected_athlete_id, limit=60)),
        "selected_imported_workout_id": import_target_id,
        "weekly_run_summaries": [
            trainer_weekly_summary_view(row)
            for row in db.get_trainer_weekly_run_summaries(user_id=selected_athlete_id, weeks=8)
        ],
        "trainer_shoes": dicts_from_rows(db.get_trainer_shoes(user_id=selected_athlete_id)),
        "shoe_usage_by_workout": trainer_shoe_usage_by_workout(selected_athlete_id),
        "runs_missing_shoes": dicts_from_rows(db.get_trainer_runs_missing_shoes(user_id=selected_athlete_id, limit=8)),
        "trainer_audit_insights": dicts_from_rows(db.get_trainer_audit_insights(user_id=selected_athlete_id, limit=10)),
        "run_reflections": dicts_from_rows(db.get_trainer_run_reflections(user_id=selected_athlete_id, limit=10)),
        "scheduler_due": scheduler_due_context(),
    }


def spotify_configured():
    """Return true when Spotify OAuth credentials are available."""
    return bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET)


def spotify_callback_url(request):
    """Build the Spotify OAuth callback URL."""
    if SPOTIFY_REDIRECT_URI:
        return SPOTIFY_REDIRECT_URI
    return str(request.url_for("music_spotify_callback"))


def spotify_oauth_state(user_id):
    """Create a state token tied to the active Dieter user."""
    payload = str(user_id or 0)
    secret = (os.getenv("SECRET_KEY") or REGISTRATION_CODE or "dieter-local-secret").encode("utf-8")
    signature = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def verify_spotify_oauth_state(state, user_id):
    """Validate Spotify OAuth state."""
    return bool(state and hmac.compare_digest(state, spotify_oauth_state(user_id)))


def spotify_http_json(path_or_url, method="GET", access_token="", data=None, form=None):
    """Call Spotify Accounts or Web API and return decoded JSON."""
    url = path_or_url if str(path_or_url).startswith("http") else f"{SPOTIFY_API_BASE}{path_or_url}"
    body = None
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if form is not None:
        body = urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        credentials = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode("utf-8")
        headers["Authorization"] = f"Basic {base64.b64encode(credentials).decode('ascii')}"
    elif data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"Spotify request failed: {exc.code} {detail[:300]}")
    except urllib.error.URLError as exc:
        raise HTTPException(status_code=502, detail=f"Could not reach Spotify: {exc.reason}")


def exchange_spotify_code(code, request):
    """Exchange a Spotify authorization code for tokens."""
    return spotify_http_json(
        f"{SPOTIFY_ACCOUNT_BASE}/api/token",
        method="POST",
        form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": spotify_callback_url(request),
        },
    )


def refresh_playlist_spotify_token(profile):
    """Refresh or return a valid Spotify access token."""
    if not profile or not profile.get("spotify_refresh_token"):
        raise HTTPException(status_code=400, detail="Connect Spotify before submitting playlists.")
    expires_at = int(profile.get("spotify_token_expires_at") or 0)
    if profile.get("spotify_access_token") and expires_at > int(time.time()) + 60:
        return profile.get("spotify_access_token")
    payload = spotify_http_json(
        f"{SPOTIFY_ACCOUNT_BASE}/api/token",
        method="POST",
        form={
            "grant_type": "refresh_token",
            "refresh_token": profile.get("spotify_refresh_token"),
        },
    )
    db.update_playlist_spotify_tokens(
        access_token=payload.get("access_token", ""),
        refresh_token=payload.get("refresh_token", profile.get("spotify_refresh_token", "")),
        expires_at=int(time.time()) + int(payload.get("expires_in", 3600)),
        scope=payload.get("scope", profile.get("spotify_scope", "")),
    )
    return payload.get("access_token", "")


def spotify_current_user(access_token):
    """Get the connected Spotify user profile."""
    return spotify_http_json("/me", access_token=access_token)


def spotify_search_track(access_token, title, artist=""):
    """Search Spotify for the best track match."""
    query = f'track:"{title}"'
    if artist:
        query += f' artist:"{artist}"'
    params = urlencode({"q": query, "type": "track", "limit": 5})
    result = spotify_http_json(f"/search?{params}", access_token=access_token)
    items = (((result or {}).get("tracks") or {}).get("items") or [])
    candidates = []
    for track in items:
        candidates.append({
            "id": track.get("id", ""),
            "uri": track.get("uri", ""),
            "name": track.get("name", ""),
            "artists": ", ".join(artist_item.get("name", "") for artist_item in track.get("artists", [])),
            "url": ((track.get("external_urls") or {}).get("spotify") or ""),
        })
    return candidates


def spotify_create_playlist(access_token, title, description="", is_public=False):
    """Create an empty Spotify playlist for the current user."""
    return spotify_http_json(
        "/me/playlists",
        method="POST",
        access_token=access_token,
        data={"name": title, "description": description, "public": bool(is_public)},
    )


def spotify_add_playlist_items(access_token, playlist_id, uris):
    """Add tracks to a Spotify playlist in batches."""
    snapshot = ""
    for index in range(0, len(uris), 100):
        payload = spotify_http_json(
            f"/playlists/{playlist_id}/tracks",
            method="POST",
            access_token=access_token,
            data={"uris": uris[index:index + 100]},
        )
        snapshot = payload.get("snapshot_id", snapshot)
    return snapshot


def parse_playlist_song_line(line):
    """Parse a dictated song line into title/artist fields."""
    text = re.sub(r"^\s*(?:\d+[\).\s-]*|[-*]\s*)", "", line or "").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return None
    for sep in [" by ", " - ", " -- ", " — "]:
        if sep in text:
            left, right = text.split(sep, 1)
            return {"raw_text": text, "title": left.strip(), "artist": right.strip()}
    return {"raw_text": text, "title": text, "artist": ""}


def parse_playlist_dictation(text):
    """Extract playlist title and song rows from free text."""
    source = text or ""
    title = ""
    title_match = re.search(r"(?:playlist (?:called|named)|call it|title)\s+['\"]?([^'\"\n.;]+)", source, flags=re.IGNORECASE)
    if title_match:
        title = title_match.group(1).strip()
    lines = [line.strip() for line in re.split(r"[\n;]+", source) if line.strip()]
    songs = []
    for line in lines:
        cleaned = re.sub(r"^(?:add|include|song|songs|playlist|called|named)\b[:\s-]*", "", line, flags=re.IGNORECASE).strip()
        if re.match(r"^(?:make|create|new)\s+(?:a\s+)?playlist\s+(?:called|named)\b", cleaned, flags=re.IGNORECASE):
            continue
        if title and title.lower() in cleaned.lower() and len(lines) > 1:
            continue
        if "," in cleaned and not re.search(r"\bby\b|\s-\s", cleaned, flags=re.IGNORECASE):
            pieces = [piece.strip() for piece in cleaned.split(",") if piece.strip()]
        else:
            pieces = [cleaned]
        for piece in pieces:
            song = parse_playlist_song_line(piece)
            if song and len(song["title"]) > 1 and not re.match(r"^(make|create|new)\s+playlist$", song["title"], flags=re.IGNORECASE):
                songs.append(song)
    return {"title": title or "Dieter Music Playlist", "songs": songs}


def parse_playlist_target(page_url):
    """Find playlist and repository ids from the Dieter Music page URL."""
    try:
        params = dict(parse_qsl(urlsplit(page_url or "").query, keep_blank_values=True))
        playlist_target = params.get("playlist_id")
        collection_target = params.get("collection_id")
        return {
            "playlist_id": int(playlist_target) if playlist_target and str(playlist_target).isdigit() else 0,
            "collection_id": int(collection_target) if collection_target and str(collection_target).isdigit() else 0,
        }
    except (TypeError, ValueError):
        return {"playlist_id": 0, "collection_id": 0}


def message_requests_playlist_action(text, page_url=""):
    """Detect Ask Dieter playlist dictation/edit requests."""
    normalized = (text or "").lower()
    if (page_url or "").startswith(("/apps/music/playlists", "/apps/playlists")):
        return any(cue in normalized for cue in ["playlist", "song", "songs", "spotify", "add", "include", "called", "named"])
    return any(cue in normalized for cue in ["parrisa playlist", "spotify playlist", "make a playlist", "create a playlist"])


def add_songs_to_playlist_draft(playlist_id, songs):
    """Add parsed song rows to a draft."""
    added = 0
    for song in songs:
        db.add_playlist_item(
            playlist_id,
            raw_text=song.get("raw_text", ""),
            title=song.get("title", ""),
            artist=song.get("artist", ""),
        )
        added += 1
    return added


def playlist_redirect_url(playlist_id, **params):
    """Build a Dieter Music URL that keeps the playlist's repository selected."""
    playlist = dict_from_row(db.get_playlist_draft(playlist_id)) if playlist_id else None
    query = {}
    if playlist and playlist.get("collection_id"):
        query["collection_id"] = playlist["collection_id"]
    if playlist_id:
        query["playlist_id"] = playlist_id
    for key, value in params.items():
        if value:
            query[key] = value
    return f"/apps/music/playlists?{urlencode(query)}" if query else "/apps/music/playlists"


def handle_playlist_action_request(message, page_url):
    """Create or edit playlist drafts from Ask Dieter."""
    parsed = parse_playlist_dictation(message.content)
    target = parse_playlist_target(page_url)
    playlist_id = target["playlist_id"]
    collection_id = target["collection_id"] or db.ensure_default_playlist_collection()
    playlist = dict_from_row(db.get_playlist_draft(playlist_id)) if playlist_id else None
    created = False
    if not playlist:
        playlist_id = db.add_playlist_draft(parsed["title"], collection_id=collection_id)
        playlist = dict_from_row(db.get_playlist_draft(playlist_id))
        created = True
    elif parsed.get("title") and re.search(r"\b(rename|call it|title|named)\b", message.content or "", flags=re.IGNORECASE):
        db.update_playlist_draft(playlist_id, title=parsed["title"])
        playlist["title"] = parsed["title"]
    added = add_songs_to_playlist_draft(playlist_id, parsed["songs"])
    if not added and not created:
        return {
            "assistant_message": "I found the playlist, but I did not detect any new songs. Dictate songs like `Song Title by Artist`, one per line.",
            "changed_fields": [],
            "playlist_context": True,
            "redirect_url": playlist_redirect_url(playlist_id),
            "redirect_label": "Open Playlist",
        }
    return {
        "assistant_message": f"{'Created' if created else 'Updated'} {playlist.get('title') or parsed['title']} with {added} song{'s' if added != 1 else ''}. Review matches before submitting to Spotify.",
        "changed_fields": ["playlist_draft", "playlist_items"],
        "playlist_context": True,
        "redirect_url": playlist_redirect_url(playlist_id),
        "redirect_label": "Open Playlist",
    }


def playlist_context(request, playlist_id=0, collection_id=0):
    """Build Dieter Music template context."""
    profile = dict_from_row(db.get_playlist_profile())
    default_collection_id = db.ensure_default_playlist_collection()
    if default_collection_id:
        db.assign_uncollected_playlist_drafts(default_collection_id)
    collections = dicts_from_rows(db.get_playlist_collections())
    selected_collection = None
    if collection_id:
        selected_collection = dict_from_row(db.get_playlist_collection(collection_id))
    if not selected_collection and collections:
        selected_collection = collections[0]
        collection_id = selected_collection["id"]
    playlists = dicts_from_rows(db.get_playlist_drafts(limit=50, collection_id=collection_id))
    selected = None
    if playlist_id:
        selected = dict_from_row(db.get_playlist_draft(playlist_id))
        if selected and collection_id and selected.get("collection_id") != collection_id:
            selected = None
    if not selected and playlists:
        selected = playlists[0]
    items = dicts_from_rows(db.get_playlist_items(selected["id"])) if selected else []
    return {
        "request": request,
        "spotify_configured": spotify_configured(),
        "spotify_connected": bool(profile and profile.get("spotify_refresh_token")),
        "playlist_profile": profile,
        "playlist_collections": collections,
        "selected_collection": selected_collection,
        "playlists": playlists,
        "selected_playlist": selected,
        "playlist_items": items,
    }


@app.get("/apps/playlists")
def legacy_playlists_app(request: Request):
    """Redirect the old Dieter Music path to the canonical music route."""
    query = request.url.query
    suffix = f"?{query}" if query else ""
    return RedirectResponse(url=f"/apps/music/playlists{suffix}", status_code=303)


@app.get("/apps/music/playlists")
def playlists_app(request: Request, playlist_id: int = 0, collection_id: int = 0):
    """Dieter Music home."""
    template = jinja_env.get_template("playlists.html")
    return HTMLResponse(template.render(playlist_context(request, playlist_id, collection_id)))


@app.post("/apps/music/playlists/collections/create")
@app.post("/apps/playlists/collections/create")
def create_playlist_collection(
    title: str = Form("Parrisa's Playlists"),
    description: str = Form(""),
    visibility: str = Form("visible"),
):
    """Create a visible playlist repository."""
    collection_id = db.add_playlist_collection(title, description=description, visibility=visibility)
    return RedirectResponse(url=f"/apps/music/playlists?collection_id={collection_id}", status_code=303)


@app.post("/apps/music/playlists/create")
@app.post("/apps/playlists/create")
def create_playlist_draft(
    title: str = Form("Dieter Music Playlist"),
    description: str = Form(""),
    is_public: str = Form(""),
    collection_id: int = Form(0),
):
    """Create a playlist draft."""
    target_collection_id = collection_id or db.ensure_default_playlist_collection()
    playlist_id = db.add_playlist_draft(title, description=description, is_public=bool(is_public), collection_id=target_collection_id)
    return RedirectResponse(url=playlist_redirect_url(playlist_id), status_code=303)


@app.post("/apps/music/playlists/{playlist_id}/update")
@app.post("/apps/playlists/{playlist_id}/update")
def update_playlist_draft_form(
    playlist_id: int,
    title: str = Form(""),
    description: str = Form(""),
    is_public: str = Form(""),
    collection_id: int = Form(0),
):
    """Update playlist metadata."""
    db.update_playlist_draft(playlist_id, title=title, description=description, is_public=bool(is_public), collection_id=collection_id or None)
    return RedirectResponse(url=playlist_redirect_url(playlist_id), status_code=303)


@app.post("/apps/music/playlists/{playlist_id}/items/add")
@app.post("/apps/playlists/{playlist_id}/items/add")
def add_playlist_item_form(
    playlist_id: int,
    title: str = Form(""),
    artist: str = Form(""),
    raw_text: str = Form(""),
):
    """Add one song to a playlist draft."""
    if raw_text and not title:
        parsed = parse_playlist_song_line(raw_text) or {}
        title = parsed.get("title", title)
        artist = parsed.get("artist", artist)
    db.add_playlist_item(playlist_id, raw_text=raw_text or f"{title} by {artist}".strip(), title=title, artist=artist)
    return RedirectResponse(url=playlist_redirect_url(playlist_id), status_code=303)


@app.post("/apps/music/playlists/items/{item_id}/update")
@app.post("/apps/playlists/items/{item_id}/update")
def update_playlist_item_form(
    item_id: int,
    playlist_id: int = Form(...),
    title: str = Form(""),
    artist: str = Form(""),
    position: int = Form(0),
):
    """Edit one playlist song row."""
    db.update_playlist_item(item_id, title=title, artist=artist, position=position, match_status="unmatched", spotify_track_id="", spotify_uri="", spotify_url="")
    return RedirectResponse(url=playlist_redirect_url(playlist_id), status_code=303)


@app.post("/apps/music/playlists/items/{item_id}/delete")
@app.post("/apps/playlists/items/{item_id}/delete")
def delete_playlist_item_form(item_id: int, playlist_id: int = Form(...)):
    """Delete one playlist song row."""
    db.delete_playlist_item(item_id)
    return RedirectResponse(url=playlist_redirect_url(playlist_id), status_code=303)


@app.get("/apps/music/playlists/spotify/connect")
@app.get("/apps/playlists/spotify/connect")
def playlists_spotify_connect(request: Request):
    """Send the active user to Spotify OAuth."""
    if not spotify_configured():
        raise HTTPException(status_code=400, detail="Spotify is not configured. Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.")
    user_id = get_current_user_id()
    params = {
        "client_id": SPOTIFY_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": spotify_callback_url(request),
        "scope": SPOTIFY_SCOPES,
        "state": spotify_oauth_state(user_id),
        "show_dialog": "false",
    }
    return RedirectResponse(url=f"{SPOTIFY_ACCOUNT_BASE}/authorize?{urlencode(params)}", status_code=303)


@app.get("/apps/music/playlists/spotify/callback", name="music_spotify_callback")
@app.get("/apps/playlists/spotify/callback", name="playlists_spotify_callback")
def playlists_spotify_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Handle Spotify OAuth callback."""
    user_id = get_current_user_id()
    if error:
        raise HTTPException(status_code=400, detail=f"Spotify authorization failed: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing Spotify authorization code.")
    if not verify_spotify_oauth_state(state, user_id):
        raise HTTPException(status_code=400, detail="Spotify authorization state did not match.")
    payload = exchange_spotify_code(code, request)
    access_token = payload.get("access_token", "")
    spotify_user = spotify_current_user(access_token) if access_token else {}
    db.update_playlist_spotify_tokens(
        spotify_user_id=spotify_user.get("id", ""),
        display_name=spotify_user.get("display_name", "") or spotify_user.get("email", ""),
        access_token=access_token,
        refresh_token=payload.get("refresh_token", ""),
        expires_at=int(time.time()) + int(payload.get("expires_in", 3600)),
        scope=payload.get("scope", SPOTIFY_SCOPES),
    )
    return RedirectResponse(url="/apps/music/playlists?spotify_connected=1", status_code=303)


@app.post("/apps/music/playlists/spotify/disconnect")
@app.post("/apps/playlists/spotify/disconnect")
def playlists_spotify_disconnect():
    """Disconnect Spotify."""
    db.clear_playlist_spotify_tokens()
    return RedirectResponse(url="/apps/music/playlists", status_code=303)


@app.post("/apps/music/playlists/{playlist_id}/spotify/match")
@app.post("/apps/playlists/{playlist_id}/spotify/match")
def match_playlist_tracks(playlist_id: int):
    """Resolve playlist draft rows to Spotify track URIs."""
    profile = dict_from_row(db.get_playlist_profile())
    access_token = refresh_playlist_spotify_token(profile)
    items = dicts_from_rows(db.get_playlist_items(playlist_id))
    for item in items:
        candidates = spotify_search_track(access_token, item.get("title", ""), item.get("artist", ""))
        best = candidates[0] if candidates else {}
        db.update_playlist_item(
            item["id"],
            spotify_track_id=best.get("id", ""),
            spotify_uri=best.get("uri", ""),
            spotify_url=best.get("url", ""),
            match_status="matched" if best.get("uri") else "unmatched",
            candidates=candidates,
        )
    return RedirectResponse(url=playlist_redirect_url(playlist_id), status_code=303)


@app.post("/apps/music/playlists/{playlist_id}/spotify/submit")
@app.post("/apps/playlists/{playlist_id}/spotify/submit")
def submit_playlist_to_spotify(playlist_id: int):
    """Create the playlist in Spotify and add matched tracks."""
    profile = dict_from_row(db.get_playlist_profile())
    access_token = refresh_playlist_spotify_token(profile)
    playlist = dict_from_row(db.get_playlist_draft(playlist_id))
    if not playlist:
        raise HTTPException(status_code=404, detail="Playlist not found.")
    items = dicts_from_rows(db.get_playlist_items(playlist_id))
    unmatched = [item for item in items if not item.get("spotify_uri")]
    if unmatched:
        raise HTTPException(status_code=400, detail="Match all songs to Spotify before submitting.")
    created = spotify_create_playlist(access_token, playlist["title"], playlist.get("description", ""), bool(playlist.get("is_public")))
    spotify_id = created.get("id", "")
    snapshot_id = spotify_add_playlist_items(access_token, spotify_id, [item["spotify_uri"] for item in items]) if spotify_id and items else created.get("snapshot_id", "")
    spotify_url = ((created.get("external_urls") or {}).get("spotify") or "")
    db.update_playlist_draft(
        playlist_id,
        status="submitted",
        spotify_playlist_id=spotify_id,
        spotify_url=spotify_url,
        spotify_snapshot_id=snapshot_id,
    )
    return RedirectResponse(url=playlist_redirect_url(playlist_id, submitted=1), status_code=303)


@app.get("/apps/trainer")
def trainer_app(request: Request):
    """Dieter Trainer home."""
    template = jinja_env.get_template("trainer.html")
    return HTMLResponse(template.render(trainer_context(request, "home")))


@app.get("/apps/trainer/workouts")
def trainer_workouts_app(request: Request, workout_type: str = ""):
    """Dieter Trainer workout catalog."""
    safe_type = workout_type if workout_type in {"run", "bike", "strength"} else ""
    template = jinja_env.get_template("trainer.html")
    return HTMLResponse(template.render(trainer_context(request, "library", safe_type)))


@app.get("/apps/trainer/upcoming")
def trainer_upcoming_app(request: Request, athlete_user_id: int = 0):
    """Upcoming Dieter Trainer workouts."""
    selected_athlete_id = athlete_user_id or (request.state.current_user or {}).get("id")
    if selected_athlete_id and not db.can_view_trainer_user(selected_athlete_id):
        raise HTTPException(status_code=403, detail="This athlete has not shared Trainer access with you.")
    template = jinja_env.get_template("trainer.html")
    return HTMLResponse(template.render(trainer_context(request, "upcoming", athlete_user_id=selected_athlete_id)))


@app.get("/apps/trainer/past")
def trainer_past_app(request: Request, athlete_user_id: int = 0):
    """Past Dieter Trainer workouts."""
    selected_athlete_id = athlete_user_id or (request.state.current_user or {}).get("id")
    if selected_athlete_id and not db.can_view_trainer_user(selected_athlete_id):
        raise HTTPException(status_code=403, detail="This athlete has not shared Trainer access with you.")
    template = jinja_env.get_template("trainer.html")
    return HTMLResponse(template.render(trainer_context(request, "past", athlete_user_id=selected_athlete_id)))


@app.get("/apps/trainer/imports")
def trainer_imports_app(request: Request, athlete_user_id: int = 0):
    """Imported Strava workouts."""
    selected_athlete_id = athlete_user_id or (request.state.current_user or {}).get("id")
    if selected_athlete_id and not db.can_view_trainer_user(selected_athlete_id):
        raise HTTPException(status_code=403, detail="This athlete has not shared Trainer access with you.")
    template = jinja_env.get_template("trainer.html")
    return HTMLResponse(template.render(trainer_context(request, "imports", athlete_user_id=selected_athlete_id)))


@app.get("/apps/trainer/strava/connect")
def trainer_strava_connect(request: Request):
    """Send the athlete to Strava OAuth."""
    if not strava_configured():
        raise HTTPException(status_code=400, detail="Strava is not configured. Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET.")
    user_id = get_current_user_id()
    params = {
        "client_id": STRAVA_CLIENT_ID,
        "redirect_uri": strava_callback_url(request),
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": "read,activity:read_all",
        "state": strava_oauth_state(user_id),
    }
    return RedirectResponse(url=f"{STRAVA_AUTHORIZE_URL}?{urlencode(params)}", status_code=303)


@app.get("/apps/trainer/strava/callback")
def trainer_strava_callback(request: Request, code: str = "", scope: str = "", state: str = "", error: str = ""):
    """Handle Strava OAuth callback."""
    user_id = get_current_user_id()
    if error:
        raise HTTPException(status_code=400, detail=f"Strava authorization failed: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Missing Strava authorization code.")
    if not verify_strava_oauth_state(state, user_id):
        raise HTTPException(status_code=400, detail="Strava authorization state did not match.")
    payload = exchange_strava_code(code, request)
    athlete = payload.get("athlete") or {}
    db.update_trainer_strava_tokens(
        athlete_id=athlete.get("id", ""),
        access_token=payload.get("access_token", ""),
        refresh_token=payload.get("refresh_token", ""),
        expires_at=payload.get("expires_at", 0),
        scope=scope or payload.get("scope", ""),
    )
    return RedirectResponse(url="/apps/trainer/imports?strava_connected=1", status_code=303)


@app.post("/apps/trainer/strava/disconnect")
def trainer_strava_disconnect():
    """Disconnect the active athlete's Strava tokens."""
    db.clear_trainer_strava_tokens()
    return RedirectResponse(url="/apps/trainer/imports", status_code=303)


@app.post("/apps/trainer/imports/strava/last-week-runs")
def import_strava_last_week_runs():
    """Pull the active athlete's last week of Strava runs."""
    result = import_recent_strava_runs(days=7)
    return RedirectResponse(
        url=f"/apps/trainer/imports?imported={result['imported']}&skipped={result['skipped']}&shoe_prompt=1",
        status_code=303,
    )


@app.post("/apps/trainer/imports/strava/recent-weeks-runs")
def import_strava_recent_weeks_runs():
    """Pull the active athlete's last four weeks of Strava runs."""
    result = import_recent_strava_runs(days=28)
    return RedirectResponse(
        url=f"/apps/trainer/imports?imported={result['imported']}&skipped={result['skipped']}&days={result['days']}&shoe_prompt=1",
        status_code=303,
    )


@app.post("/apps/trainer/imports/strava/six-month-runs")
def import_strava_six_month_runs():
    """Pull the active athlete's last six months of Strava runs."""
    result = import_recent_strava_runs(days=183)
    return RedirectResponse(
        url=f"/apps/trainer/imports?imported={result['imported']}&skipped={result['skipped']}&days={result['days']}&shoe_prompt=1",
        status_code=303,
    )


@app.post("/apps/trainer/imports/strava/activity")
def import_strava_activity(activity_id: str = Form(...)):
    """Pull one Strava run by activity id."""
    result = import_single_strava_activity(activity_id.strip())
    return RedirectResponse(
        url=f"/apps/trainer/imports?imported={result['imported']}&skipped={result['skipped']}&activity_id={result['activity_id']}&shoe_prompt=1",
        status_code=303,
    )


@app.post("/apps/trainer/shoes")
def add_trainer_shoe(
    name: str = Form(...),
    brand: str = Form(""),
    model: str = Form(""),
    initial_miles: str = Form("0"),
    notes: str = Form(""),
    next: str = Form("/apps/trainer/settings"),
):
    """Add or update a shoe in the athlete inventory."""
    try:
        starting_miles = float(initial_miles or 0)
    except ValueError:
        starting_miles = 0
    db.add_trainer_shoe(name, brand=brand, model=model, initial_miles=starting_miles, notes=notes)
    return RedirectResponse(url=next or "/apps/trainer/settings", status_code=303)


@app.post("/apps/trainer/imports/{workout_id}/shoes")
def add_trainer_workout_shoe(
    workout_id: int,
    shoe_id: int = Form(0),
    new_shoe_name: str = Form(""),
    segment_label: str = Form(""),
    distance_miles: str = Form(""),
    notes: str = Form(""),
):
    """Log which shoe was used for all or part of an imported run."""
    selected_shoe_id = shoe_id
    if not selected_shoe_id and new_shoe_name.strip():
        selected_shoe_id = db.add_trainer_shoe(new_shoe_name.strip())
    if not selected_shoe_id:
        raise HTTPException(status_code=400, detail="Choose a shoe or enter a new shoe name.")
    workout = dict_from_row(db.get_trainer_imported_workout(workout_id))
    default_meters = workout.get("distance_meters") if workout else None
    db.add_trainer_workout_shoe(
        workout_id,
        selected_shoe_id,
        segment_label=segment_label,
        distance_meters=miles_to_meters(distance_miles) if distance_miles else default_meters,
        notes=notes,
    )
    if workout:
        scan_trainer_audit_insights(user_id=workout.get("user_id"))
    return RedirectResponse(url="/apps/trainer/imports", status_code=303)


@app.post("/apps/trainer/imports/shoes/{usage_id}/delete")
def delete_trainer_workout_shoe(usage_id: int):
    """Remove a shoe usage row."""
    db.delete_trainer_workout_shoe(usage_id)
    return RedirectResponse(url="/apps/trainer/imports", status_code=303)


@app.post("/apps/trainer/audit/scan")
def scan_trainer_audit():
    """Refresh Trainer audit insights from logged runs."""
    scan_trainer_audit_insights()
    return RedirectResponse(url="/apps/trainer/imports#audit-insights", status_code=303)


@app.get("/apps/trainer/settings")
def trainer_settings_app(request: Request):
    """Trainer profile and coach permission settings."""
    template = jinja_env.get_template("trainer.html")
    return HTMLResponse(template.render(trainer_context(request, "settings")))


@app.post("/apps/trainer/settings/mode")
def update_trainer_mode(mode: str = Form("athlete")):
    """Switch between athlete and coach mode."""
    db.update_trainer_mode(mode)
    return RedirectResponse(url="/apps/trainer/settings", status_code=303)


@app.post("/apps/trainer/settings/coaches/grant")
def grant_trainer_coach(email: str = Form(...)):
    """Grant a coach permission to view the active athlete's workouts."""
    coach = dict_from_row(db.get_user_by_email(email))
    if not coach:
        raise HTTPException(status_code=404, detail="No user with that email exists.")
    db.grant_trainer_coach(coach["id"])
    return RedirectResponse(url="/apps/trainer/settings", status_code=303)


@app.post("/apps/trainer/settings/coaches/{grant_id}/revoke")
def revoke_trainer_coach(grant_id: int):
    """Revoke a coach's Trainer access."""
    db.revoke_trainer_coach(grant_id)
    return RedirectResponse(url="/apps/trainer/settings", status_code=303)


@app.post("/apps/trainer/imports/strava/manual")
def import_strava_workout_manual(
    external_id: str = Form(...),
    activity_type: str = Form("Run"),
    title: str = Form(""),
    started_at: str = Form(""),
    distance_meters: str = Form(""),
    moving_time_seconds: str = Form(""),
):
    """Manual Strava import placeholder until OAuth/webhooks are configured."""
    category = classify_strava_workout(activity_type, title)
    db.add_trainer_imported_workout(
        external_id=external_id,
        activity_type=activity_type,
        workout_category=category,
        title=title or activity_type,
        started_at=started_at,
        distance_meters=float(distance_meters) if distance_meters else None,
        moving_time_seconds=int(moving_time_seconds) if moving_time_seconds else None,
        raw={"manual_import": True},
    )
    return RedirectResponse(url="/apps/trainer/imports", status_code=303)


@app.post("/apps/trainer/workouts/{workout_id}/schedule")
def schedule_trainer_workout(
    workout_id: int,
    scheduled_for: str = Form(""),
    notes: str = Form(""),
):
    """Schedule a workout from the Trainer catalog."""
    workout = db.get_trainer_workout(workout_id)
    if not workout:
        raise HTTPException(status_code=404, detail="Workout not found")
    db.add_trainer_session(workout_id, scheduled_for=scheduled_for, notes=notes)
    return RedirectResponse(url="/apps/trainer/upcoming", status_code=303)


@app.post("/apps/trainer/sessions/{session_id}/complete")
def complete_trainer_session(session_id: int, notes: str = Form("")):
    """Mark a Trainer workout done."""
    session = dict_from_row(db.get_trainer_session(session_id))
    if not session:
        raise HTTPException(status_code=404, detail="Workout session not found")
    if session.get("user_id") != get_current_user_id():
        raise HTTPException(status_code=403, detail="Only the athlete can change this workout.")
    db.complete_trainer_session(session_id, notes=notes)
    return RedirectResponse(url="/apps/trainer/past", status_code=303)


@app.post("/apps/trainer/sessions/{session_id}/reopen")
def reopen_trainer_session(session_id: int):
    """Move a past Trainer workout back to upcoming."""
    session = dict_from_row(db.get_trainer_session(session_id))
    if not session:
        raise HTTPException(status_code=404, detail="Workout session not found")
    if session.get("user_id") != get_current_user_id():
        raise HTTPException(status_code=403, detail="Only the athlete can change this workout.")
    db.reopen_trainer_session(session_id)
    return RedirectResponse(url="/apps/trainer/upcoming", status_code=303)


@app.post("/apps/trainer/sessions/{session_id}/delete")
def delete_trainer_session(session_id: int, next: str = Form("/apps/trainer/upcoming")):
    """Delete a Trainer workout session."""
    session = dict_from_row(db.get_trainer_session(session_id))
    if not session:
        raise HTTPException(status_code=404, detail="Workout session not found")
    if session.get("user_id") != get_current_user_id():
        raise HTTPException(status_code=403, detail="Only the athlete can change this workout.")
    db.delete_trainer_session(session_id)
    return RedirectResponse(url=safe_redirect_path(next, "/apps/trainer/upcoming"), status_code=303)


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
        "recipe_import_url": "/apps/recipes/manage",
    }
    template = jinja_env.get_template("action_detail.html")
    html = template.render(context)
    return HTMLResponse(html)


@app.post("/scheduler/create")
def create_scheduler_item_form(
    title: str = Form(...),
    context_label: str = Form(""),
    scheduled_for: str = Form(""),
    notes: str = Form(""),
):
    """Create a scheduler item from the planner UI."""
    db.add_scheduler_item(
        title,
        context_label=context_label,
        scheduled_for=scheduled_for,
        notes=notes,
        source="manual",
    )
    return RedirectResponse(url="/apps/assistant/scheduler", status_code=303)


@app.post("/scheduler/{item_id}/complete")
def complete_scheduler_item_form(item_id: int, next: str = Form("/apps/assistant/scheduler")):
    """Complete a scheduler item from the planner UI."""
    db.mark_scheduler_item_complete(item_id)
    redirect_url = append_query_param(
        safe_redirect_path(next, "/apps/assistant/scheduler"),
        undo_scheduler_id=item_id,
    )
    return RedirectResponse(url=redirect_url, status_code=303)

@app.post("/scheduler/{item_id}/reopen")
def reopen_scheduler_item_form(item_id: int, next: str = Form("/apps/assistant/scheduler")):
    """Reopen a completed scheduler item from the planner UI."""
    db.reopen_scheduler_item(item_id)
    return RedirectResponse(url=safe_redirect_path(next, "/apps/assistant/scheduler"), status_code=303)

@app.post("/scheduler/{item_id}/notes/{line_index}/toggle")
def toggle_scheduler_note_form(
    item_id: int,
    line_index: int,
    next: str = Form("/apps/assistant/scheduler"),
):
    """Toggle one scheduler note checkbox bullet."""
    item = dict_from_row(db.get_scheduler_item(item_id))
    if not item:
        raise HTTPException(status_code=404, detail="Scheduler item not found")
    notes = toggle_scheduler_note_checkbox(item.get("notes") or "", line_index)
    db.update_scheduler_item(item_id, notes=notes)
    return RedirectResponse(url=safe_redirect_path(next, "/apps/assistant/scheduler"), status_code=303)

@app.post("/scheduler/{item_id}/notes/make-checklist")
def make_scheduler_notes_checklist_form(
    item_id: int,
    next: str = Form("/apps/assistant/scheduler"),
):
    """Convert scheduler note bullets into checkbox bullets."""
    item = dict_from_row(db.get_scheduler_item(item_id))
    if not item:
        raise HTTPException(status_code=404, detail="Scheduler item not found")
    db.update_scheduler_item(item_id, notes=make_scheduler_notes_checklist(item.get("notes") or ""))
    return RedirectResponse(url=safe_redirect_path(next, "/apps/assistant/scheduler"), status_code=303)

@app.post("/scheduler/{item_id}/delete")
def delete_scheduler_item_form(item_id: int, next: str = Form("/apps/assistant/scheduler")):
    """Delete a scheduler item from the planner UI."""
    db.delete_scheduler_item(item_id)
    return RedirectResponse(url=safe_redirect_path(next, "/apps/assistant/scheduler"), status_code=303)


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
    recipe_import_url = "/apps/recipes/manage"
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

    public_complete_meals = filter_public_complete_meals(recipe_app["complete_meals"])
    context = {
        "request": request,
        "recipe_app": recipe_app,
        "project": recipe_app["project"],
        "import_action": recipe_app["import_action"],
        "groups": recipe_app["groups"],
        "complete_meals": public_complete_meals,
        "components": recipe_app["components"],
        "component_sections": recipe_app["component_sections"],
        "meal_plan_items": recipe_app["meal_plan_items"],
        "grocery_lists": recipe_app["grocery_lists"],
        "done_grocery_lists": recipe_app["done_grocery_lists"],
        "stats": recipe_app["stats"],
        "recipe_view": "home",
    }
    template = jinja_env.get_template("recipe_home.html")
    html = template.render(context)
    return HTMLResponse(html)


@app.get("/apps/recipes/create-meal")
def recipe_create_meal_page(request: Request):
    """Recipe app component picker page."""
    recipe_app = get_recipe_app_context()
    if not recipe_app["project"]:
        raise HTTPException(status_code=404, detail="Recipe app project not found")

    context = {
        "request": request,
        "recipe_app": recipe_app,
        "project": recipe_app["project"],
        "import_action": recipe_app["import_action"],
        "groups": recipe_app["groups"],
        "complete_meals": filter_public_complete_meals(recipe_app["complete_meals"]),
        "components": recipe_app["components"],
        "component_sections": recipe_app["component_sections"],
        "meal_plan_items": recipe_app["meal_plan_items"],
        "grocery_lists": recipe_app["grocery_lists"],
        "done_grocery_lists": recipe_app["done_grocery_lists"],
        "stats": recipe_app["stats"],
        "recipe_view": "create_meal",
    }
    template = jinja_env.get_template("recipe_home.html")
    html = template.render(context)
    return HTMLResponse(html)


@app.get("/apps/recipes/manage")
def recipe_manage_page(request: Request):
    """Recipe app management/admin page."""
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
    template = jinja_env.get_template("recipe_manage.html")
    html = template.render(context)
    return HTMLResponse(html)


@app.get("/apps/recipes/grocery-lists")
def recipe_grocery_lists_page(request: Request):
    """Recipe app archive of completed grocery lists."""
    recipe_app = get_recipe_app_context(include_library=False, run_maintenance=False)
    if not recipe_app["project"]:
        raise HTTPException(status_code=404, detail="Recipe app project not found")

    done_grocery_lists = annotate_grocery_list_cook_counts(
        prepare_grocery_lists(db.get_recipe_grocery_lists(100, "done"))
    )
    context = {
        "request": request,
        "recipe_app": recipe_app,
        "project": recipe_app["project"],
        "done_grocery_lists": done_grocery_lists,
    }
    template = jinja_env.get_template("recipe_grocery_lists.html")
    html = template.render(context)
    return HTMLResponse(html)


@app.get("/apps/recipes/meals/{meal_id}")
def recipe_meal_detail_page(request: Request, meal_id: int):
    """Complete meal detail page."""
    recipe_app = get_recipe_app_context(include_library=False, run_maintenance=False)
    meal = dict_from_row(db.get_recipe_complete_meal(meal_id))
    if not recipe_app["project"] or not meal:
        raise HTTPException(status_code=404, detail="Complete meal not found")

    meal = prepare_recipe_complete_meals([meal])[0]
    ingredient_sections = parse_baking_ingredient_sections(meal.get("display_ingredients_text") or "")
    ingredient_sections = attach_baking_instructions_to_sections(
        ingredient_sections,
        meal.get("display_instructions_text") or "",
    )
    change_log = prepare_recipe_change_log(db.get_recipe_change_log("meal", meal_id))
    context = {
        "request": request,
        "recipe_app": recipe_app,
        "meal": meal,
        "ingredient_sections": ingredient_sections,
        "sectioned_ingredient_values": {
            section["key"]: "\n".join(section["items"])
            for section in ingredient_sections
            if section["key"] in BAKING_SECTION_LABELS
        },
        "change_log": change_log,
        "cooked_prompt": request.query_params.get("cooked") == "1",
    }
    template = jinja_env.get_template("recipe_meal_detail.html")
    html = template.render(context)
    return HTMLResponse(html)


@app.post("/apps/recipes/meals/{meal_id}/ingredient-sections/save")
def save_recipe_meal_ingredient_sections_form(
    meal_id: int,
    dough_ingredients: str = Form(""),
    filling_ingredients: str = Form(""),
    icing_ingredients: str = Form(""),
):
    """Save deliberate baking ingredient sections for a complete meal."""
    meal = dict_from_row(db.get_recipe_complete_meal(meal_id))
    if not meal:
        raise HTTPException(status_code=404, detail="Complete meal not found")
    ingredients_text = format_baking_ingredient_sections({
        "dough": dough_ingredients,
        "filling": filling_ingredients,
        "icing": icing_ingredients,
    })
    if not ingredients_text:
        raise HTTPException(status_code=400, detail="Add at least one ingredient section")
    before = prepare_recipe_complete_meals([meal])[0]
    db.update_recipe_complete_meal_edits(meal_id, ingredients_text=ingredients_text)
    after = dict(before)
    after["display_ingredients_text"] = ingredients_text
    db.add_recipe_change_log(
        "meal",
        meal_id,
        "Sectioned baking ingredient intake saved.",
        "Saved dough/filling/icing ingredient sections.",
        ["ingredients_text"],
        {
            "title": before.get("display_title") or before.get("title") or "",
            "ingredients_text": before.get("display_ingredients_text") or "",
            "instructions_text": before.get("display_instructions_text") or "",
        },
        {
            "title": after.get("display_title") or after.get("title") or "",
            "ingredients_text": ingredients_text,
            "instructions_text": after.get("display_instructions_text") or "",
        },
        "baking-intake",
    )
    return RedirectResponse(url=f"/apps/recipes/meals/{meal_id}", status_code=303)


@app.get("/apps/recipes/components/{component_id}")
def recipe_component_detail_page(request: Request, component_id: int):
    """Meal component detail page."""
    recipe_app = get_recipe_app_context(include_library=False, run_maintenance=False)
    component = dict_from_row(db.get_recipe_component(component_id))
    if not recipe_app["project"] or not component:
        raise HTTPException(status_code=404, detail="Meal component not found")
    component = prepare_recipe_components([component])[0]
    ingredient_sections = parse_baking_ingredient_sections(component.get("display_ingredients_text") or "")
    ingredient_sections = attach_baking_instructions_to_sections(
        ingredient_sections,
        component.get("display_instructions_text") or "",
    )
    change_log = prepare_recipe_change_log(db.get_recipe_change_log("component", component_id))

    context = {
        "request": request,
        "recipe_app": recipe_app,
        "component": component,
        "ingredient_sections": ingredient_sections,
        "sectioned_ingredient_values": {
            section["key"]: "\n".join(section["items"])
            for section in ingredient_sections
            if section["key"] in BAKING_SECTION_LABELS
        },
        "change_log": change_log,
    }
    template = jinja_env.get_template("recipe_component_detail.html")
    html = template.render(context)
    return HTMLResponse(html)


@app.post("/apps/recipes/components/{component_id}/ingredient-sections/save")
def save_recipe_component_ingredient_sections_form(
    component_id: int,
    dough_ingredients: str = Form(""),
    filling_ingredients: str = Form(""),
    icing_ingredients: str = Form(""),
):
    """Save deliberate baking ingredient sections for a component."""
    component = dict_from_row(db.get_recipe_component(component_id))
    if not component:
        raise HTTPException(status_code=404, detail="Meal component not found")
    ingredients_text = format_baking_ingredient_sections({
        "dough": dough_ingredients,
        "filling": filling_ingredients,
        "icing": icing_ingredients,
    })
    if not ingredients_text:
        raise HTTPException(status_code=400, detail="Add at least one ingredient section")
    before = prepare_recipe_components([component])[0]
    db.update_recipe_component_edits(component_id, ingredients_text=ingredients_text)
    db.add_recipe_change_log(
        "component",
        component_id,
        "Sectioned baking ingredient intake saved.",
        "Saved dough/filling/icing ingredient sections.",
        ["ingredients_text"],
        {
            "title": before.get("display_title") or before.get("title") or "",
            "ingredients_text": before.get("display_ingredients_text") or "",
            "instructions_text": before.get("display_instructions_text") or "",
        },
        {
            "title": before.get("display_title") or before.get("title") or "",
            "ingredients_text": ingredients_text,
            "instructions_text": before.get("display_instructions_text") or "",
        },
        "baking-intake",
    )
    return RedirectResponse(url=f"/apps/recipes/components/{component_id}", status_code=303)


@app.get("/apps/recipes/import")
def recipe_import_page(request: Request):
    """Recipe app import surface for uploading recipe images."""
    recipe_app = get_recipe_app_context(include_library=False, run_maintenance=False)
    project = recipe_app["project"]
    action = recipe_app["import_action"]
    if not project or not action:
        raise HTTPException(status_code=404, detail="Recipe import task not found")
    project_id = project["id"]
    action_id = action["id"]
    db.sync_recipe_complete_meals_from_extractions()
    recipe_image_groups = prepare_recipe_image_groups(db.get_recipe_image_groups(action_id))
    meals_by_source_group = {
        meal.get("source_group_id"): meal
        for meal in prepare_recipe_complete_meals(db.get_recipe_complete_meals())
    }
    for group in recipe_image_groups:
        meal = meals_by_source_group.get(group.get("id"))
        group["imported_meal_id"] = meal.get("id") if meal else None
        group["imported_meal_status"] = meal.get("status") if meal else ""

    context = {
        "request": request,
        "project": project,
        "action": action,
        "recipe_app": recipe_app,
        "recipe_image_groups": recipe_image_groups,
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
    db.cleanup_duplicate_recipes()

    return RedirectResponse(url="/apps/recipes", status_code=303)


@app.post("/apps/recipes/components/amounts/analyze")
def analyze_recipe_component_amounts_form():
    """Analyze component ingredients into measurable structured amounts."""
    components = dicts_from_rows(db.get_recipe_components())
    for component in components:
        images = []
        if component.get("source_group_id"):
            images = dicts_from_rows(db.get_recipe_images_for_group(component["source_group_id"]))
        result = recipe_ocr_service.analyze_component_ingredient_amounts(component, images)
        if result["status"] == "analyzed":
            db.update_recipe_component_structured_ingredients(component["id"], result["ingredients"])

    return RedirectResponse(url="/apps/recipes/create-meal", status_code=303)


@app.post("/apps/recipes/meals/save")
def save_selected_recipe_meal_form(
    meal_name: str = Form(...),
    selected_component_ids: str = Form("[]"),
):
    """Save selected meal components as a named complete meal."""
    title = meal_name.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Meal name is required")
    try:
        component_ids = json.loads(selected_component_ids or "[]")
    except json.JSONDecodeError:
        component_ids = []
    component_ids = [int(component_id) for component_id in component_ids if str(component_id).isdigit()]
    if not component_ids:
        raise HTTPException(status_code=400, detail="Select at least one meal part")

    components_by_id = {}
    for component_id in component_ids:
        component = dict_from_row(db.get_recipe_component(component_id))
        if component:
            components_by_id[component_id] = prepare_recipe_components([component])[0]
    components = [components_by_id[component_id] for component_id in component_ids if component_id in components_by_id]
    if not components:
        raise HTTPException(status_code=400, detail="Selected meal parts were not found")

    ingredients_text, instructions_text = build_saved_meal_text_from_components(components)
    meal_id = db.create_saved_recipe_meal(title, ingredients_text, instructions_text)
    db.cleanup_duplicate_recipes()
    db.share_recipe_library_with_all_users()
    return RedirectResponse(url=f"/apps/recipes/meals/{meal_id}", status_code=303)


@app.post("/apps/recipes/meal-plan/meals/{meal_id}/add")
def add_complete_meal_to_plan_form(meal_id: int):
    """Add a complete meal to the pending meal plan."""
    meal = dict_from_row(db.get_recipe_complete_meal(meal_id))
    if not meal:
        raise HTTPException(status_code=404, detail="Complete meal not found")
    meal = prepare_recipe_complete_meals([meal])[0]
    title = meal.get("display_title") or meal.get("title") or "Complete meal"
    db.add_recipe_meal_plan_item("complete_meal", title, source_id=meal_id)
    return RedirectResponse(url="/apps/recipes#meal-plan", status_code=303)


@app.post("/apps/recipes/meals/{meal_id}/favorite")
def favorite_recipe_meal_form(
    meal_id: int,
    next: str = Form("/apps/recipes"),
):
    """Mark a recipe as a per-user favorite."""
    meal = dict_from_row(db.get_recipe_complete_meal(meal_id))
    if not meal:
        raise HTTPException(status_code=404, detail="Recipe not found")
    db.set_recipe_favorite("meal", meal_id, True)
    return RedirectResponse(url=safe_redirect_path(next, "/apps/recipes"), status_code=303)


@app.post("/apps/recipes/meals/{meal_id}/favorite/delete")
def unfavorite_recipe_meal_form(
    meal_id: int,
    next: str = Form("/apps/recipes"),
):
    """Remove a recipe from the current user's favorites."""
    meal = dict_from_row(db.get_recipe_complete_meal(meal_id))
    if not meal:
        raise HTTPException(status_code=404, detail="Recipe not found")
    db.set_recipe_favorite("meal", meal_id, False)
    return RedirectResponse(url=safe_redirect_path(next, "/apps/recipes"), status_code=303)


@app.post("/apps/recipes/meals/{meal_id}/share")
def share_recipe_meal_form(
    request: Request,
    meal_id: int,
    email: str = Form(...),
    permission: str = Form("view"),
):
    """Share a complete meal from the detail page."""
    share_recipe_with_email("meal", meal_id, email, permission, request)
    return RedirectResponse(url=f"/apps/recipes/meals/{meal_id}", status_code=303)


@app.post("/apps/recipes/meal-plan/components/add")
def add_component_meal_to_plan_form(
    meal_name: str = Form(""),
    selected_component_ids: str = Form("[]"),
):
    """Add selected components to the pending meal plan without saving a full recipe."""
    try:
        component_ids = json.loads(selected_component_ids or "[]")
    except json.JSONDecodeError:
        component_ids = []
    component_ids = [int(component_id) for component_id in component_ids if str(component_id).isdigit()]
    if not component_ids:
        raise HTTPException(status_code=400, detail="Select at least one meal part")

    components_by_id = {}
    for component_id in component_ids:
        component = dict_from_row(db.get_recipe_component(component_id))
        if component:
            components_by_id[component_id] = prepare_recipe_components([component])[0]
    components = [components_by_id[component_id] for component_id in component_ids if component_id in components_by_id]
    if not components:
        raise HTTPException(status_code=400, detail="Selected meal parts were not found")

    title = meal_name.strip()
    if not title:
        component_titles = [component.get("display_title") or component.get("title") for component in components[:3]]
        suffix = "..." if len(components) > 3 else ""
        title = f"Custom meal: {', '.join(component_titles)}{suffix}"

    db.add_recipe_meal_plan_item("components", title, component_ids=component_ids)
    return RedirectResponse(url="/apps/recipes#meal-plan", status_code=303)


@app.post("/apps/recipes/components/{component_id}/share")
def share_recipe_component_form(
    request: Request,
    component_id: int,
    email: str = Form(...),
    permission: str = Form("view"),
):
    """Share a recipe component from the detail page."""
    share_recipe_with_email("component", component_id, email, permission, request)
    return RedirectResponse(url=f"/apps/recipes/components/{component_id}", status_code=303)


@app.post("/apps/recipes/meal-plan/{item_id}/remove")
def remove_meal_plan_item_form(item_id: int):
    """Remove a pending meal from the active meal plan."""
    removed = db.remove_recipe_meal_plan_item(item_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Pending meal plan item not found")
    return RedirectResponse(url="/apps/recipes#meal-plan", status_code=303)


@app.post("/apps/recipes/grocery-lists/create")
def create_recipe_grocery_list_form():
    """Create a persistent grocery list from all pending meal-plan items."""
    meal_plan_items = prepare_meal_plan_items(db.get_recipe_meal_plan_items("pending"))
    if not meal_plan_items:
        return RedirectResponse(url="/apps/recipes#meal-plan", status_code=303)
    grocery_items = build_grocery_items_for_plan(meal_plan_items)
    title = f"Grocery list {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    list_id = db.create_recipe_grocery_list(
        title,
        [item["id"] for item in meal_plan_items],
        grocery_items,
    )
    return RedirectResponse(url=f"/apps/recipes/grocery-lists/{list_id}", status_code=303)


@app.get("/apps/recipes/grocery-lists/{list_id}")
def recipe_grocery_list_page(request: Request, list_id: int):
    """Show one generated grocery list record."""
    recipe_app = get_recipe_app_context(include_library=False, run_maintenance=False)
    grocery_list = dict_from_row(db.get_recipe_grocery_list(list_id))
    if not recipe_app["project"] or not grocery_list:
        raise HTTPException(status_code=404, detail="Grocery list not found")
    refresh_grocery_list_completion(list_id)
    grocery_list = prepare_grocery_lists([dict_from_row(db.get_recipe_grocery_list(list_id))])[0]
    needed_grocery_items = [
        item for item in grocery_list["items"]
        if item.get("status") != "gotten"
    ]
    needed_grocery_sections = group_grocery_items_by_category(needed_grocery_items)
    gotten_grocery_items = [
        item for item in grocery_list["items"]
        if item.get("status") == "gotten"
    ]
    linked_items = prepare_meal_plan_items(db.get_recipe_meal_plan_items(None, 250))
    linked_items_by_id = {item["id"]: item for item in linked_items}
    linked_cookable_items = cookable_meal_plan_items_for_grocery_list(grocery_list, linked_items_by_id)
    context = {
        "request": request,
        "recipe_app": recipe_app,
        "grocery_list": grocery_list,
        "needed_grocery_items": needed_grocery_items,
        "needed_grocery_sections": needed_grocery_sections,
        "gotten_grocery_items": gotten_grocery_items,
        "linked_meal_plan_items": linked_cookable_items,
    }
    template = jinja_env.get_template("recipe_grocery_list.html")
    html = template.render(context)
    return HTMLResponse(html)


@app.post("/apps/recipes/grocery-lists/{list_id}/delete")
def delete_recipe_grocery_list_form(list_id: int):
    """Delete an old grocery list record."""
    deleted = db.delete_recipe_grocery_list(list_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Grocery list not found")
    return RedirectResponse(url="/apps/recipes#meal-plan", status_code=303)


def append_manual_grocery_item(list_id, item_name, quantity="", note="", category=""):
    """Append a manually entered grocery item to an existing grocery list."""
    grocery_list = dict_from_row(db.get_recipe_grocery_list(list_id))
    if not grocery_list:
        raise HTTPException(status_code=404, detail="Grocery list not found")
    name = item_name.strip()
    if not name:
        return False
    try:
        items = json.loads(grocery_list.get("items_json") or "[]")
    except json.JSONDecodeError:
        items = []

    manual_item = {
        "name": title_case_grocery_name(normalize_grocery_name(name) or name),
        "quantities": [quantity.strip()] if quantity.strip() else [],
        "notes": [note.strip()] if note.strip() else [],
        "sources": ["Manual"],
        "status": "need",
        "source_kind": "manual",
    }
    allowed_categories = {category_key for category_key, _, _ in GROCERY_CATEGORY_RULES}
    if category in allowed_categories:
        manual_item["category"] = category
    items.append(manual_item)
    db.update_recipe_grocery_list_items(list_id, items)
    return True


@app.post("/apps/recipes/grocery-lists/items/add")
def add_manual_grocery_item_to_current_form(
    item_name: str = Form(...),
    quantity: str = Form(""),
    note: str = Form(""),
    category: str = Form(""),
):
    """Add a manual grocery item to the pending meal plan."""
    name = item_name.strip()
    if not name:
        return RedirectResponse(url="/apps/recipes#meal-plan", status_code=303)
    display_name = title_case_grocery_name(normalize_grocery_name(name) or name)
    allowed_categories = {category_key for category_key, _, _ in GROCERY_CATEGORY_RULES}
    manual_item = {
        "name": display_name,
        "quantity": quantity.strip(),
        "note": note.strip(),
        "category": category if category in allowed_categories else "",
    }
    db.add_recipe_meal_plan_item(
        "manual_item",
        f"Manual item: {display_name}",
        component_ids=[manual_item],
    )
    return RedirectResponse(url="/apps/recipes#meal-plan", status_code=303)


@app.post("/apps/recipes/grocery-lists/{list_id}/items/add")
def add_manual_grocery_item_form(
    list_id: int,
    item_name: str = Form(...),
    quantity: str = Form(""),
    note: str = Form(""),
    category: str = Form(""),
):
    """Add a manual item to a grocery list."""
    append_manual_grocery_item(list_id, item_name, quantity, note, category)
    return RedirectResponse(url=f"/apps/recipes/grocery-lists/{list_id}", status_code=303)


@app.post("/apps/recipes/grocery-lists/{list_id}/items/{item_index}/status")
def update_recipe_grocery_item_status_form(
    list_id: int,
    item_index: int,
    status: str = Form(...),
):
    """Mark a grocery list item as needed or gotten."""
    grocery_list = dict_from_row(db.get_recipe_grocery_list(list_id))
    if not grocery_list:
        raise HTTPException(status_code=404, detail="Grocery list not found")
    try:
        items = json.loads(grocery_list.get("items_json") or "[]")
    except json.JSONDecodeError:
        items = []
    if item_index < 0 or item_index >= len(items) or not isinstance(items[item_index], dict):
        raise HTTPException(status_code=404, detail="Grocery item not found")
    if status not in {"need", "gotten"}:
        raise HTTPException(status_code=400, detail="Unsupported grocery item status")
    items[item_index]["status"] = status
    db.update_recipe_grocery_list_items(list_id, items)
    return RedirectResponse(url=f"/apps/recipes/grocery-lists/{list_id}", status_code=303)

def record_meal_plan_cooked_feedback(item, feedback):
    """Persist cooking feedback for a meal-plan item and mirror recipe feedback when possible."""
    feedback = (feedback or "").strip()
    db.add_recipe_meal_feedback(
        item["id"],
        item.get("source_kind", ""),
        item.get("source_id"),
        item.get("title", ""),
        feedback,
    )
    if item.get("source_kind") == "complete_meal" and item.get("source_id") and feedback:
        recipe = recipe_record_for_edit("meal", item["source_id"])
        before = {
            "title": recipe.get("display_title") or recipe.get("title") or item.get("title", ""),
            "ingredients_text": recipe.get("display_ingredients_text") or "",
            "instructions_text": recipe.get("display_instructions_text") or "",
        } if recipe else {"title": item.get("title", ""), "ingredients_text": "", "instructions_text": ""}
        db.add_recipe_change_log(
            "meal",
            item["source_id"],
            feedback,
            "Cooking feedback recorded.",
            [],
            before,
            before,
            "user-feedback",
        )


@app.post("/apps/recipes/grocery-lists/{list_id}/meal-plan/{item_id}/cooked")
def mark_grocery_list_meal_plan_item_cooked_form(
    list_id: int,
    item_id: int,
    feedback: str = Form(""),
):
    """Record cooking feedback from a grocery list and mark the meal cooked."""
    grocery_list = dict_from_row(db.get_recipe_grocery_list(list_id))
    if not grocery_list:
        raise HTTPException(status_code=404, detail="Grocery list not found")
    item = dict_from_row(db.get_recipe_meal_plan_item(item_id))
    if not item:
        raise HTTPException(status_code=404, detail="Meal plan item not found")
    try:
        linked_ids = json.loads(grocery_list.get("meal_plan_item_ids_json") or "[]")
    except json.JSONDecodeError:
        linked_ids = []
    if item_id not in linked_ids:
        raise HTTPException(status_code=400, detail="Meal is not linked to this grocery list")
    record_meal_plan_cooked_feedback(item, feedback)
    db.mark_recipe_meal_plan_item_cooked(item_id)
    refresh_grocery_list_completion(list_id)
    return RedirectResponse(url=f"/apps/recipes/grocery-lists/{list_id}", status_code=303)


@app.post("/apps/recipes/meal-plan/{item_id}/cooked")
def mark_meal_plan_item_cooked_form(item_id: int, feedback: str = Form("")):
    """Mark a planned meal cooked and offer a recipe feedback moment."""
    item = dict_from_row(db.get_recipe_meal_plan_item(item_id))
    if not item:
        raise HTTPException(status_code=404, detail="Meal plan item not found")
    record_meal_plan_cooked_feedback(item, feedback)
    db.mark_recipe_meal_plan_item_cooked(item_id)
    if item.get("source_kind") == "complete_meal" and item.get("source_id"):
        return RedirectResponse(url=f"/apps/recipes/meals/{item['source_id']}?cooked=1", status_code=303)
    return RedirectResponse(url="/apps/recipes#meal-plan", status_code=303)


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


@app.post("/projects/{project_id}/share")
def share_project_form(
    request: Request,
    project_id: int,
    email: str = Form(...),
    permission: str = Form("view"),
):
    """Share a project from the detail page."""
    share_project_with_email(project_id, email, permission, request)
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
    recipe_import_url = "/apps/recipes/manage"
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
    """Upload recipe image/PDF files to the recipe app import queue."""
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
        original_name = Path(upload.filename).name
        extension = Path(original_name).suffix.lower()
        content_type = upload.content_type or mimetypes.guess_type(original_name)[0] or ""
        is_pdf = content_type == "application/pdf" or extension == ".pdf"
        is_image = content_type.startswith("image/")
        if not is_image and not is_pdf:
            continue

        if is_pdf:
            group_count += 1
            image_group_id = db.create_recipe_image_group(
                project_id,
                action_id,
                Path(original_name).stem or f"PDF recipe {group_count}",
                layout="pdf",
            )
            stored_name = f"{uuid.uuid4().hex}.pdf"
            destination = recipe_uploads_dir / stored_name
            with destination.open("wb") as handle:
                shutil.copyfileobj(upload.file, handle)
            try:
                cloud_persistence.sync_upload_file(destination)
            except Exception as exc:
                print(f"Warning: PDF upload cloud sync failed - {exc}")

            db.add_recipe_image(
                project_id,
                action_id,
                f"recipe_images/{stored_name}",
                original_name,
                "application/pdf",
                image_group_id,
                "pdf",
            )

            preview_name = ""
            preview_label = ""
            try:
                preview_name, preview_label = extract_recipe_pdf_preview(destination, recipe_uploads_dir)
            except Exception as exc:
                print(f"Warning: PDF preview extraction failed - {exc}")
            if preview_name:
                preview_path = recipe_uploads_dir / preview_name
                try:
                    cloud_persistence.sync_upload_file(preview_path)
                except Exception as exc:
                    print(f"Warning: PDF preview cloud sync failed - {exc}")
                db.add_recipe_image(
                    project_id,
                    action_id,
                    f"recipe_images/{preview_name}",
                    preview_label or f"{Path(original_name).stem} preview",
                    mimetypes.guess_type(preview_name)[0] or "image/png",
                    image_group_id,
                    "photo",
                )
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

        stored_name = f"{uuid.uuid4().hex}{extension}"
        destination = recipe_uploads_dir / stored_name
        with destination.open("wb") as handle:
            shutil.copyfileobj(upload.file, handle)
        try:
            cloud_persistence.sync_upload_file(destination)
        except Exception as exc:
            print(f"Warning: upload cloud sync failed - {exc}")

        db.add_recipe_image(
            project_id,
            action_id,
            f"recipe_images/{stored_name}",
            original_name,
            content_type,
            image_group_id,
            side,
        )

        if side == "front":
            open_group_id = image_group_id

    return RedirectResponse(
        url="/apps/recipes/import",
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

    db.sync_recipe_complete_meals_from_extractions()

    return RedirectResponse(
        url="/apps/recipes/import",
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
        url="/apps/recipes/import",
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
