"""
Database module for project management.
Handles SQLite operations for projects, notes, blockers, and goals.
"""

import json
import re
import sqlite3
import threading
import contextvars
from datetime import datetime

_current_user_id = contextvars.ContextVar("current_user_id", default=None)


def set_current_user_id(user_id):
    """Set the active request user for database scoping."""
    return _current_user_id.set(user_id)


def reset_current_user_id(token):
    """Reset the active request user after a request."""
    _current_user_id.reset(token)


def get_current_user_id():
    """Return the active request user id, if any."""
    return _current_user_id.get()


class Database:
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = None
        self._connection_lock = threading.RLock()
        self.after_commit = None

    def connect(self):
        """Establish database connection."""
        with self._connection_lock:
            if self.conn is None:
                self.conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
                self.conn.execute("PRAGMA busy_timeout = 30000")
                self.conn.row_factory = sqlite3.Row
        return self.conn

    def close(self):
        """Close database connection."""
        # The FastAPI app shares one Database instance across worker threads.
        # Closing that shared connection after each helper call lets one request
        # invalidate another request's active cursor, so keep it open for the
        # life of the local app process.
        return

    def _commit(self):
        """Commit and notify optional persistence hooks."""
        self.conn.commit()
        if self.after_commit:
            try:
                self.after_commit()
            except Exception as exc:
                print(f"Warning: after-commit hook failed: {exc}")

    @staticmethod
    def _normalized_recipe_title(title):
        """Normalize recipe/component titles for duplicate detection."""
        normalized = (title or "").strip().lower().replace("&", " and ")
        normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
        return re.sub(r"\s+", " ", normalized).strip()

    def init(self):
        """Initialize database schema."""
        self.connect()
        cursor = self.conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                display_name TEXT DEFAULT '',
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'user',
                status TEXT DEFAULT 'active',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT UNIQUE NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS project_shares (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                owner_user_id INTEGER NOT NULL,
                shared_with_user_id INTEGER NOT NULL,
                permission TEXT DEFAULT 'view',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, shared_with_user_id),
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (shared_with_user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recipe_shares (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipe_kind TEXT NOT NULL,
                recipe_id INTEGER NOT NULL,
                owner_user_id INTEGER NOT NULL,
                shared_with_user_id INTEGER NOT NULL,
                permission TEXT DEFAULT 'view',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(recipe_kind, recipe_id, shared_with_user_id),
                FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (shared_with_user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recipe_favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipe_kind TEXT NOT NULL,
                recipe_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(recipe_kind, recipe_id, user_id),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recipe_variations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipe_kind TEXT NOT NULL,
                recipe_id INTEGER NOT NULL,
                title TEXT DEFAULT '',
                ingredients_text TEXT DEFAULT '',
                instructions_text TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                status TEXT DEFAULT 'candidate',
                review_status TEXT DEFAULT 'pending',
                upvote_count INTEGER DEFAULT 0,
                promotion_threshold INTEGER DEFAULT 2,
                created_by_user_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recipe_variation_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                variation_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                vote TEXT DEFAULT 'up',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(variation_id, user_id),
                FOREIGN KEY (variation_id) REFERENCES recipe_variations(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Projects table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                status TEXT DEFAULT 'active',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Project notes table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)

        # Blockers table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS blockers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                description TEXT NOT NULL,
                severity TEXT DEFAULT 'medium',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)

        # Weekly goals table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS weekly_goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                goal TEXT NOT NULL,
                completed BOOLEAN DEFAULT 0,
                week_start TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)

        # Recommended actions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recommended_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                priority TEXT DEFAULT 'medium',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_id INTEGER NOT NULL,
                step TEXT NOT NULL,
                status TEXT DEFAULT 'open',
                sort_order INTEGER DEFAULT 100,
                completed_at TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (action_id) REFERENCES recommended_actions(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS planner_change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_kind TEXT NOT NULL,
                target_id INTEGER NOT NULL,
                user_message TEXT NOT NULL,
                summary TEXT NOT NULL,
                operations_json TEXT DEFAULT '[]',
                before_json TEXT DEFAULT '{}',
                after_json TEXT DEFAULT '{}',
                model TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scheduler_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                context_label TEXT DEFAULT '',
                scheduled_for TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                status TEXT DEFAULT 'open',
                source TEXT DEFAULT '',
                project_id INTEGER,
                action_id INTEGER,
                completed_at TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
                FOREIGN KEY (action_id) REFERENCES recommended_actions(id) ON DELETE SET NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trainer_workouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                workout_type TEXT NOT NULL,
                workout_category TEXT DEFAULT '',
                focus TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                details_json TEXT DEFAULT '[]',
                source_url TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trainer_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                mode TEXT DEFAULT 'athlete',
                strava_athlete_id TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trainer_coach_grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                athlete_user_id INTEGER NOT NULL,
                coach_user_id INTEGER NOT NULL,
                permission TEXT DEFAULT 'view',
                status TEXT DEFAULT 'active',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(athlete_user_id, coach_user_id),
                FOREIGN KEY (athlete_user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (coach_user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trainer_imported_workouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT DEFAULT 'strava',
                external_id TEXT NOT NULL,
                user_id INTEGER,
                activity_type TEXT DEFAULT '',
                workout_category TEXT DEFAULT '',
                title TEXT DEFAULT '',
                started_at TEXT DEFAULT '',
                distance_meters REAL,
                moving_time_seconds INTEGER,
                elapsed_time_seconds INTEGER,
                raw_json TEXT DEFAULT '{}',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, external_id, user_id),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trainer_workout_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workout_id INTEGER NOT NULL,
                scheduled_for TEXT DEFAULT '',
                completed_at TEXT DEFAULT '',
                status TEXT DEFAULT 'upcoming',
                notes TEXT DEFAULT '',
                user_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (workout_id) REFERENCES trainer_workouts(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recipe_images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                action_id INTEGER NOT NULL,
                group_id INTEGER,
                side TEXT DEFAULT '',
                filename TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                content_type TEXT DEFAULT '',
                status TEXT DEFAULT 'uploaded',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY (action_id) REFERENCES recommended_actions(id) ON DELETE CASCADE,
                FOREIGN KEY (group_id) REFERENCES recipe_image_groups(id) ON DELETE SET NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recipe_image_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                action_id INTEGER NOT NULL,
                layout TEXT DEFAULT 'front_back',
                label TEXT DEFAULT '',
                status TEXT DEFAULT 'uploaded',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY (action_id) REFERENCES recommended_actions(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recipe_extractions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL UNIQUE,
                status TEXT DEFAULT 'pending',
                ingredients_text TEXT DEFAULT '',
                instructions_text TEXT DEFAULT '',
                sections_json TEXT DEFAULT '[]',
                raw_response TEXT DEFAULT '',
                error TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (group_id) REFERENCES recipe_image_groups(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recipe_complete_meals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_group_id INTEGER NOT NULL UNIQUE,
                source_kind TEXT DEFAULT 'card',
                title TEXT DEFAULT '',
                ingredients_text TEXT DEFAULT '',
                instructions_text TEXT DEFAULT '',
                status TEXT DEFAULT 'needs_review',
                quality_notes_json TEXT DEFAULT '[]',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (source_group_id) REFERENCES recipe_image_groups(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recipe_components (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_meal_id INTEGER,
                title TEXT NOT NULL,
                component_type TEXT DEFAULT 'unknown',
                ingredients_text TEXT DEFAULT '',
                structured_ingredients_json TEXT DEFAULT '[]',
                instructions_text TEXT DEFAULT '',
                status TEXT DEFAULT 'draft',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (source_meal_id) REFERENCES recipe_complete_meals(id) ON DELETE SET NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recipe_change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipe_kind TEXT NOT NULL,
                recipe_id INTEGER NOT NULL,
                user_message TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                changed_fields_json TEXT DEFAULT '[]',
                before_json TEXT DEFAULT '{}',
                after_json TEXT DEFAULT '{}',
                model TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recipe_meal_plan_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_kind TEXT NOT NULL,
                source_id INTEGER,
                title TEXT NOT NULL,
                component_ids_json TEXT DEFAULT '[]',
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                cooked_at TEXT DEFAULT ''
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recipe_meal_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meal_plan_item_id INTEGER NOT NULL,
                source_kind TEXT DEFAULT '',
                source_id INTEGER,
                title TEXT DEFAULT '',
                feedback TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (meal_plan_item_id) REFERENCES recipe_meal_plan_items(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS app_feedback_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                area TEXT DEFAULT '',
                page_url TEXT DEFAULT '',
                page_title TEXT DEFAULT '',
                reporter_name TEXT DEFAULT '',
                reporter_email TEXT DEFAULT '',
                raw_feedback TEXT NOT NULL,
                destination_project_id INTEGER,
                destination_action_id INTEGER,
                status TEXT DEFAULT 'open',
                user_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (destination_project_id) REFERENCES projects(id) ON DELETE SET NULL,
                FOREIGN KEY (destination_action_id) REFERENCES recommended_actions(id) ON DELETE SET NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recipe_grocery_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT DEFAULT '',
                meal_plan_item_ids_json TEXT DEFAULT '[]',
                items_json TEXT DEFAULT '[]',
                status TEXT DEFAULT 'active',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_step_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_id INTEGER NOT NULL,
                summary TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                applied_at TEXT DEFAULT '',
                FOREIGN KEY (action_id) REFERENCES recommended_actions(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS priority_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                summary TEXT NOT NULL,
                model TEXT NOT NULL,
                raw_response TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS priority_review_instructions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_id INTEGER NOT NULL,
                operation TEXT NOT NULL,
                project_name TEXT DEFAULT '',
                payload TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                result TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                applied_at TEXT DEFAULT '',
                FOREIGN KEY (review_id) REFERENCES priority_reviews(id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                model TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        self._ensure_column(cursor, "projects", "description", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "projects", "priority_score", "INTEGER DEFAULT 3")
        self._ensure_column(cursor, "projects", "focus_reason", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "projects", "user_id", "INTEGER")
        self._ensure_column(cursor, "recommended_actions", "sort_order", "INTEGER DEFAULT 100")
        self._ensure_column(cursor, "recommended_actions", "status", "TEXT DEFAULT 'open'")
        self._ensure_column(cursor, "recommended_actions", "completed_at", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "scheduler_items", "source", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "scheduler_items", "user_id", "INTEGER")
        self._ensure_column(cursor, "scheduler_items", "project_id", "INTEGER")
        self._ensure_column(cursor, "scheduler_items", "action_id", "INTEGER")
        self._ensure_column(cursor, "scheduler_items", "completed_at", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "scheduler_items", "updated_at", "TEXT DEFAULT CURRENT_TIMESTAMP")
        self._ensure_column(cursor, "recipe_images", "user_id", "INTEGER")
        self._ensure_column(cursor, "recipe_images", "group_id", "INTEGER")
        self._ensure_column(cursor, "recipe_images", "side", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "recipe_image_groups", "user_id", "INTEGER")
        self._ensure_column(cursor, "recipe_image_groups", "layout", "TEXT DEFAULT 'front_back'")
        self._ensure_column(cursor, "recipe_extractions", "user_id", "INTEGER")
        self._ensure_column(cursor, "recipe_complete_meals", "user_id", "INTEGER")
        self._ensure_column(cursor, "recipe_complete_meals", "quality_notes_json", "TEXT DEFAULT '[]'")
        self._ensure_column(cursor, "recipe_complete_meals", "source_kind", "TEXT DEFAULT 'card'")
        self._ensure_column(cursor, "recipe_complete_meals", "edited_title", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "recipe_complete_meals", "edited_ingredients_text", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "recipe_complete_meals", "edited_instructions_text", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "recipe_components", "user_id", "INTEGER")
        self._ensure_column(cursor, "recipe_components", "structured_ingredients_json", "TEXT DEFAULT '[]'")
        self._ensure_column(cursor, "recipe_components", "edited_title", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "recipe_components", "edited_ingredients_text", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "recipe_components", "edited_instructions_text", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "recipe_change_log", "user_id", "INTEGER")
        self._ensure_column(cursor, "recipe_meal_plan_items", "user_id", "INTEGER")
        self._ensure_column(cursor, "recipe_meal_feedback", "user_id", "INTEGER")
        self._ensure_column(cursor, "recipe_grocery_lists", "user_id", "INTEGER")
        self._ensure_column(cursor, "planner_change_log", "user_id", "INTEGER")
        self._ensure_column(cursor, "chat_messages", "user_id", "INTEGER")
        self._ensure_column(cursor, "app_feedback_reports", "user_id", "INTEGER")
        self._ensure_column(cursor, "app_feedback_reports", "destination_project_id", "INTEGER")
        self._ensure_column(cursor, "app_feedback_reports", "destination_action_id", "INTEGER")
        self._ensure_column(cursor, "recipe_complete_meals", "visibility", "TEXT DEFAULT 'shared'")
        self._ensure_column(cursor, "recipe_components", "visibility", "TEXT DEFAULT 'shared'")
        self._ensure_column(cursor, "recipe_variations", "promotion_threshold", "INTEGER DEFAULT 2")
        self._ensure_column(cursor, "trainer_workouts", "workout_category", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "trainer_workout_sessions", "user_id", "INTEGER")
        self._repair_sample_data_links(cursor)
        self._deprioritize_overlong_actions(cursor)
        self._ensure_recipe_import_steps(cursor)
        self._ensure_recipe_image_groups(cursor)
        self._ensure_trainer_workouts(cursor)

        self._commit()
        self.close()

    def _ensure_column(self, cursor, table, column, definition):
        """Add a column when opening an older database file."""
        cursor.execute(f"PRAGMA table_info({table})")
        columns = [row["name"] for row in cursor.fetchall()]
        if column not in columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _active_user_id(self):
        """Current request user id for row ownership."""
        return get_current_user_id()

    def get_user_count(self):
        """Get count of user accounts."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) AS count FROM users")
        result = cursor.fetchone()
        self.close()
        return result["count"]

    def create_user(self, email, display_name, password_hash, role="user"):
        """Create an application user."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO users (email, display_name, password_hash, role, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (email.strip().lower(), display_name.strip(), password_hash, role),
        )
        user_id = cursor.lastrowid
        self._commit()
        self.close()
        return user_id

    def get_user_by_email(self, email):
        """Get a user by email."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM users WHERE lower(email) = lower(?)", (email.strip(),))
        user = cursor.fetchone()
        self.close()
        return user

    def get_user_by_id(self, user_id):
        """Get a user by id."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        self.close()
        return user

    def get_users_by_role(self, role):
        """Get active users by role."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM users WHERE role = ? AND status = 'active' ORDER BY id",
            (role,),
        )
        users = cursor.fetchall()
        self.close()
        return users

    def create_session(self, user_id, token_hash, expires_at):
        """Persist a login session."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO user_sessions (user_id, token_hash, expires_at) VALUES (?, ?, ?)",
            (user_id, token_hash, expires_at),
        )
        self._commit()
        self.close()

    def get_session_user(self, token_hash, now):
        """Resolve a session token hash to an active user."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT users.*
            FROM user_sessions
            JOIN users ON users.id = user_sessions.user_id
            WHERE user_sessions.token_hash = ?
              AND user_sessions.expires_at > ?
              AND users.status = 'active'
            """,
            (token_hash, now),
        )
        user = cursor.fetchone()
        self.close()
        return user

    def delete_session(self, token_hash):
        """Delete one session."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM user_sessions WHERE token_hash = ?", (token_hash,))
        self._commit()
        self.close()

    def cleanup_expired_sessions(self, now):
        """Delete expired sessions."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM user_sessions WHERE expires_at <= ?", (now,))
        self._commit()
        self.close()

    def claim_unowned_data(self, user_id):
        """Assign pre-login single-user data to the first registered user."""
        self.connect()
        cursor = self.conn.cursor()
        for table in [
            "projects",
            "scheduler_items",
            "recipe_images",
            "recipe_image_groups",
            "recipe_extractions",
            "recipe_complete_meals",
            "recipe_components",
            "recipe_change_log",
            "recipe_meal_plan_items",
            "recipe_meal_feedback",
            "recipe_grocery_lists",
            "planner_change_log",
            "chat_messages",
            "trainer_workout_sessions",
        ]:
            cursor.execute(f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL", (user_id,))
        self._commit()
        self.close()

    def share_project(self, project_id, shared_with_user_id, permission="view"):
        """Share a project/plan with another user."""
        owner_user_id = self._active_user_id()
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO project_shares (project_id, owner_user_id, shared_with_user_id, permission)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(project_id, shared_with_user_id)
            DO UPDATE SET permission = excluded.permission
            """,
            (project_id, owner_user_id, shared_with_user_id, permission),
        )
        self._commit()
        self.close()

    def share_recipe(self, recipe_kind, recipe_id, shared_with_user_id, permission="view"):
        """Share a recipe meal/component with another user."""
        owner_user_id = self._active_user_id()
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO recipe_shares (recipe_kind, recipe_id, owner_user_id, shared_with_user_id, permission)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(recipe_kind, recipe_id, shared_with_user_id)
            DO UPDATE SET permission = excluded.permission
            """,
            (recipe_kind, recipe_id, owner_user_id, shared_with_user_id, permission),
        )
        self._commit()
        self.close()

    def share_recipe_library_with_all_users(self):
        """Share every owned recipe record with every other user as view-only."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT OR IGNORE INTO recipe_shares
                (recipe_kind, recipe_id, owner_user_id, shared_with_user_id, permission)
            SELECT 'meal', recipe_complete_meals.id, recipe_complete_meals.user_id, users.id, 'view'
            FROM recipe_complete_meals
            JOIN users ON users.id != recipe_complete_meals.user_id
            WHERE recipe_complete_meals.user_id IS NOT NULL
            """
        )
        cursor.execute(
            """
            INSERT OR IGNORE INTO recipe_shares
                (recipe_kind, recipe_id, owner_user_id, shared_with_user_id, permission)
            SELECT 'component', recipe_components.id, recipe_components.user_id, users.id, 'view'
            FROM recipe_components
            JOIN users ON users.id != recipe_components.user_id
            WHERE recipe_components.user_id IS NOT NULL
            """
        )
        self._commit()
        self.close()

    def _repair_sample_data_links(self, cursor):
        """Repair older sample rows that were linked to the wrong project ids."""
        cursor.execute("SELECT id, name FROM projects")
        project_ids = {row["name"]: row["id"] for row in cursor.fetchall()}

        repairs = [
            ("recommended_actions", "action", "Research EEG signal amplification circuits", "EEG headband"),
            ("recommended_actions", "action", "Set up development board", "EEG headband"),
            ("recommended_actions", "action", "Implement recipe search feature", "Recipe display app"),
            ("recommended_actions", "action", "Design database schema", "Recipe display app"),
            ("recommended_actions", "action", "Optimize image processing pipeline", "Calcium imaging analysis"),
            ("recommended_actions", "action", "Add visualization tools", "Calcium imaging analysis"),
            ("blockers", "description", "Waiting for hardware samples", "EEG headband"),
            ("blockers", "description", "Budget approval pending", "EEG headband"),
            ("blockers", "description", "UI framework selection needed", "Recipe display app"),
            ("blockers", "description", "Compute resources limited", "Calcium imaging analysis"),
            ("weekly_goals", "goal", "Make progress on EEG headband", "EEG headband"),
            ("weekly_goals", "goal", "Make progress on Recipe display app", "Recipe display app"),
            ("weekly_goals", "goal", "Make progress on Calcium imaging analysis", "Calcium imaging analysis"),
        ]

        for table, column, value, project_name in repairs:
            project_id = project_ids.get(project_name)
            if project_id:
                cursor.execute(
                    f"UPDATE {table} SET project_id = ? WHERE {column} = ?",
                    (project_id, value),
                )

    def _deprioritize_overlong_actions(self, cursor):
        """Keep pasted paragraphs from acting like crisp next actions."""
        cursor.execute(
            """
            UPDATE recommended_actions
            SET priority = 'low'
            WHERE length(action) > 180 AND priority != 'low'
            """
        )

    def _ensure_recipe_import_steps(self, cursor):
        """Seed useful subtasks for the first recipe image import task."""
        cursor.execute(
            """
            SELECT recommended_actions.id
            FROM recommended_actions
            JOIN projects ON projects.id = recommended_actions.project_id
            WHERE projects.name = ?
              AND recommended_actions.action = ?
            """,
            ("Recipe display app", "Import the first batch of recipe images"),
        )
        action = cursor.fetchone()
        if not action:
            return

        action_id = action["id"]
        cursor.execute("SELECT COUNT(*) AS count FROM task_steps WHERE action_id = ?", (action_id,))
        if cursor.fetchone()["count"]:
            return

        steps = [
            "Create a mobile-friendly recipe image upload page",
            "Add image upload handling and storage",
            "Save uploaded image metadata in the database",
            "Show uploaded images in an import queue",
            "Mark uploaded images ready for OCR",
        ]
        for index, step in enumerate(steps, 1):
            cursor.execute(
                """
                INSERT INTO task_steps (action_id, step, sort_order)
                VALUES (?, ?, ?)
                """,
                (action_id, step, index * 10),
            )

    def _ensure_recipe_image_groups(self, cursor):
        """Backfill front/back pair groups for older uploaded recipe images."""
        cursor.execute(
            """
            SELECT DISTINCT project_id, action_id
            FROM recipe_images
            WHERE group_id IS NULL
            """
        )
        scopes = cursor.fetchall()

        for scope in scopes:
            cursor.execute(
                """
                SELECT *
                FROM recipe_images
                WHERE project_id = ? AND action_id = ? AND group_id IS NULL
                ORDER BY id ASC
                """,
                (scope["project_id"], scope["action_id"]),
            )
            images = cursor.fetchall()

            for index in range(0, len(images), 2):
                pair = images[index:index + 2]
                label = f"Recipe pair {(index // 2) + 1}"
                cursor.execute(
                    """
                    INSERT INTO recipe_image_groups (project_id, action_id, layout, label)
                    VALUES (?, ?, 'front_back', ?)
                    """,
                    (scope["project_id"], scope["action_id"], label),
                )
                group_id = cursor.lastrowid

                for side, image in zip(["front", "back"], pair):
                    cursor.execute(
                        """
                        UPDATE recipe_images
                        SET group_id = ?, side = ?
                        WHERE id = ?
                        """,
                        (group_id, side, image["id"]),
                    )

    def _ensure_trainer_workouts(self, cursor):
        """Seed the starter Dieter Trainer workout catalog."""
        workouts = [
            {
                "slug": "run-threshold-mile-repeats",
                "title": "4-6 x 1 Mile Threshold",
                "workout_type": "run",
                "workout_category": "run_threshold",
                "focus": "Threshold",
                "summary": "Controlled mile repeats at threshold effort with short recoveries.",
                "source_url": "",
                "details": [
                    {"label": "Warm up", "text": "10-20 minutes easy plus relaxed strides."},
                    {"label": "Main set", "text": "4-6 x 1 mile at threshold effort."},
                    {"label": "Recovery", "text": "60 seconds easy jog or walk between reps."},
                    {"label": "Cool down", "text": "10-15 minutes easy."},
                ],
            },
            {
                "slug": "run-threshold-1k-repeats",
                "title": "8-10 x 1K Threshold",
                "workout_type": "run",
                "workout_category": "run_threshold",
                "focus": "Threshold",
                "summary": "Shorter threshold repeats with very compact recovery.",
                "source_url": "",
                "details": [
                    {"label": "Warm up", "text": "10-20 minutes easy plus 4 strides."},
                    {"label": "Main set", "text": "8-10 x 1 kilometer at controlled threshold effort."},
                    {"label": "Recovery", "text": "30 seconds easy jog between reps."},
                    {"label": "Cool down", "text": "10-15 minutes easy."},
                ],
            },
            {
                "slug": "run-400m-volume-sets",
                "title": "3-4 Sets of 8 x 400",
                "workout_type": "run",
                "workout_category": "run_speed",
                "focus": "Speed endurance",
                "summary": "Grouped 400s for rhythm, turnover, and durable speed.",
                "source_url": "",
                "details": [
                    {"label": "Warm up", "text": "15-20 minutes easy plus drills or strides."},
                    {"label": "Main set", "text": "3-4 sets of 8 x 400 meters."},
                    {"label": "Recovery", "text": "30 seconds between reps, 60 seconds between sets."},
                    {"label": "Cool down", "text": "10-15 minutes easy."},
                ],
            },
            {
                "slug": "bike-aerobic-tempo-builder",
                "title": "Bike Aerobic Tempo Builder",
                "workout_type": "bike",
                "workout_category": "bike_tempo",
                "focus": "Aerobic tempo",
                "summary": "A simple bike workout to start the Trainer bike library.",
                "source_url": "",
                "details": [
                    {"label": "Warm up", "text": "10 minutes easy spinning."},
                    {"label": "Main set", "text": "3 x 8 minutes comfortably hard tempo."},
                    {"label": "Recovery", "text": "3 minutes easy between efforts."},
                    {"label": "Cool down", "text": "8-10 minutes easy."},
                ],
            },
            {
                "slug": "strength-glute-med-basics",
                "title": "Glute Med Basics",
                "workout_type": "strength",
                "workout_category": "strength_glutes",
                "focus": "Glutes and hip stability",
                "summary": "A PT-style foundation session for hip control and lateral glute strength.",
                "source_url": "https://www.manhattanptandpain.com/physical-therapy-exercises-for-gluteus-medius-strength",
                "details": [
                    {"label": "Side-lying hip abduction", "text": "2-3 sets of 10-15 per side, slow lower."},
                    {"label": "Clamshells", "text": "2-3 sets of 12-20 per side, keep hips stacked."},
                    {"label": "Glute bridges", "text": "2-3 sets of 10-15, pause at the top."},
                    {"label": "Lateral band walks", "text": "2-3 passes each direction, keep band tension."},
                ],
            },
            {
                "slug": "strength-runner-glute-activation",
                "title": "Runner Glute Activation",
                "workout_type": "strength",
                "workout_category": "strength_glutes",
                "focus": "Pre-run activation",
                "summary": "Short activation sequence for runners before easy or quality days.",
                "source_url": "https://www.therapeuticassociates.com/glute-activation-for-runners-3-moves-beyond-the-basic-clamshell/",
                "details": [
                    {"label": "Runner's clam", "text": "2 sets of 10-15 per side."},
                    {"label": "Banded lateral walk", "text": "2 sets of 10-15 steps each direction."},
                    {"label": "Single-leg bridge", "text": "2 sets of 8-12 per side."},
                ],
            },
            {
                "slug": "strength-band-and-rdl-glutes",
                "title": "Band + RDL Glute Builder",
                "workout_type": "strength",
                "workout_category": "strength_glutes",
                "focus": "Glutes, hamstrings, and single-leg control",
                "summary": "Progressive glute work with bands and Romanian deadlift patterns.",
                "source_url": "https://theprehabguys.com/the-best-exercises-for-the-glute-med/",
                "details": [
                    {"label": "Monster walks", "text": "2-3 sets of 8-12 steps forward and back."},
                    {"label": "Banded glute bridge", "text": "3 sets of 10-15 reps."},
                    {"label": "Romanian deadlift", "text": "3 sets of 8-10 controlled reps."},
                    {"label": "Single-leg RDL reach", "text": "2 sets of 6-8 per side, light and precise."},
                ],
            },
        ]

        for workout in workouts:
            cursor.execute(
                """
                INSERT INTO trainer_workouts
                    (slug, title, workout_type, workout_category, focus, summary, details_json, source_url, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(slug) DO UPDATE SET
                    title = excluded.title,
                    workout_type = excluded.workout_type,
                    workout_category = excluded.workout_category,
                    focus = excluded.focus,
                    summary = excluded.summary,
                    details_json = excluded.details_json,
                    source_url = excluded.source_url,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    workout["slug"],
                    workout["title"],
                    workout["workout_type"],
                    workout["workout_category"],
                    workout["focus"],
                    workout["summary"],
                    json.dumps(workout["details"]),
                    workout["source_url"],
                ),
            )

    def populate_sample_data(self):
        """Populate database with sample projects."""
        self.connect()
        cursor = self.conn.cursor()

        projects = [
            ("EEG headband", "active", "Prototype a wearable EEG/BCI hardware and signal processing stack.", 4),
            ("Recipe display app", "active", "Build a kitchen-friendly recipe browsing and display app.", 3),
            ("Calcium imaging analysis", "active", "Improve analysis tools for calcium imaging datasets.", 3),
        ]

        for name, status, description, priority_score in projects:
            cursor.execute(
                "INSERT INTO projects (name, status, description, priority_score) VALUES (?, ?, ?, ?)",
                (name, status, description, priority_score),
            )

        self._commit()

        # Add sample notes, blockers, and actions
        cursor.execute("SELECT id, name FROM projects ORDER BY id")
        projects_data = cursor.fetchall()

        sample_notes = {
            0: ["Need to research BCI signal processing", "Contact hardware supplier for quotes"],
            1: ["UI design mockups ready", "Database schema finalized"],
            2: ["Data preprocessing pipeline working", "Need to optimize for large datasets"],
        }

        sample_blockers = {
            0: [("Waiting for hardware samples", "high"), ("Budget approval pending", "high")],
            1: [("UI framework selection needed", "medium")],
            2: [("Compute resources limited", "medium")],
        }

        sample_actions = {
            0: [("Research EEG signal amplification circuits", "high"), ("Set up development board", "high")],
            1: [("Implement recipe search feature", "high"), ("Design database schema", "medium")],
            2: [("Optimize image processing pipeline", "high"), ("Add visualization tools", "medium")],
        }

        for idx, (proj_id, _) in enumerate(projects_data):
            # Add notes
            for note in sample_notes.get(idx, []):
                cursor.execute(
                    "INSERT INTO notes (project_id, content) VALUES (?, ?)",
                    (proj_id, note),
                )

            # Add blockers
            for blocker, severity in sample_blockers.get(idx, []):
                cursor.execute(
                    "INSERT INTO blockers (project_id, description, severity) VALUES (?, ?, ?)",
                    (proj_id, blocker, severity),
                )

            # Add recommended actions
            for action, priority in sample_actions.get(idx, []):
                cursor.execute(
                    "INSERT INTO recommended_actions (project_id, action, priority) VALUES (?, ?, ?)",
                    (proj_id, action, priority),
                )

            # Add weekly goals
            cursor.execute(
                "INSERT INTO weekly_goals (project_id, goal, week_start) VALUES (?, ?, ?)",
                (proj_id, f"Make progress on {projects[idx][0]}", datetime.now().strftime("%Y-%m-%d")),
            )

        self._commit()
        self.close()

    def get_project_count(self):
        """Get count of projects."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM projects")
        result = cursor.fetchone()
        self.close()
        return result["count"]

    def get_all_projects(self):
        """Get all active projects."""
        self.connect()
        cursor = self.conn.cursor()
        user_id = self._active_user_id()
        if user_id:
            cursor.execute(
                """
                SELECT DISTINCT projects.*
                FROM projects
                LEFT JOIN project_shares ON project_shares.project_id = projects.id
                WHERE projects.status = 'active'
                  AND (projects.user_id = ? OR project_shares.shared_with_user_id = ?)
                ORDER BY projects.priority_score DESC, projects.updated_at DESC
                """,
                (user_id, user_id),
            )
        else:
            cursor.execute("SELECT * FROM projects WHERE status = 'active' ORDER BY priority_score DESC, updated_at DESC")
        projects = cursor.fetchall()
        self.close()
        return projects

    def add_project(self, name, description="", priority_score=3):
        """Add a project."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO projects (name, description, priority_score, status, updated_at, user_id)
            VALUES (?, ?, ?, 'active', CURRENT_TIMESTAMP, ?)
            """,
            (name, description, priority_score, self._active_user_id()),
        )
        self._commit()
        project_id = cursor.lastrowid
        self.close()
        return project_id

    def update_project_priority(self, project_id, priority_score, focus_reason=""):
        """Update project priority metadata."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE projects
            SET priority_score = ?, focus_reason = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (priority_score, focus_reason, project_id),
        )
        self._commit()
        self.close()

    def update_project_status(self, project_id, status):
        """Update project status."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE projects SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, project_id),
        )
        self._commit()
        self.close()

    def update_project_details(self, project_id, name=None, description=None, priority_score=None, focus_reason=None):
        """Update editable project fields."""
        updates = []
        values = []
        if name is not None:
            updates.append("name = ?")
            values.append(name)
        if description is not None:
            updates.append("description = ?")
            values.append(description)
        if priority_score is not None:
            updates.append("priority_score = ?")
            values.append(priority_score)
        if focus_reason is not None:
            updates.append("focus_reason = ?")
            values.append(focus_reason)
        if not updates:
            return
        updates.append("updated_at = CURRENT_TIMESTAMP")
        values.append(project_id)
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            f"UPDATE projects SET {', '.join(updates)} WHERE id = ?",
            values,
        )
        self._commit()
        self.close()

    def get_project_by_id(self, project_id):
        """Get a specific project."""
        self.connect()
        cursor = self.conn.cursor()
        user_id = self._active_user_id()
        if user_id:
            cursor.execute(
                """
                SELECT DISTINCT projects.*
                FROM projects
                LEFT JOIN project_shares ON project_shares.project_id = projects.id
                WHERE projects.id = ?
                  AND (projects.user_id = ? OR project_shares.shared_with_user_id = ?)
                """,
                (project_id, user_id, user_id),
            )
        else:
            cursor.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        project = cursor.fetchone()
        self.close()
        return project

    def get_project_by_name(self, name):
        """Get project by name."""
        self.connect()
        cursor = self.conn.cursor()
        user_id = self._active_user_id()
        if user_id:
            cursor.execute(
                """
                SELECT DISTINCT projects.*
                FROM projects
                LEFT JOIN project_shares ON project_shares.project_id = projects.id
                WHERE projects.name = ?
                  AND (projects.user_id = ? OR project_shares.shared_with_user_id = ?)
                """,
                (name, user_id, user_id),
            )
        else:
            cursor.execute("SELECT * FROM projects WHERE name = ?", (name,))
        project = cursor.fetchone()
        self.close()
        return project

    def get_notes(self, project_id):
        """Get all notes for a project."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM notes WHERE project_id = ? ORDER BY created_at DESC", (project_id,))
        notes = cursor.fetchall()
        self.close()
        return notes

    def add_note(self, project_id, content):
        """Add a note to a project."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO notes (project_id, content) VALUES (?, ?)", (project_id, content))
        self._commit()
        self.close()

    def get_scheduler_items(self, status="open", limit=25):
        """Get scheduler/agenda items across projects."""
        self.connect()
        cursor = self.conn.cursor()
        query = """
            SELECT scheduler_items.*, projects.name AS project_name, recommended_actions.action AS action_title
            FROM scheduler_items
            LEFT JOIN projects ON projects.id = scheduler_items.project_id
            LEFT JOIN recommended_actions ON recommended_actions.id = scheduler_items.action_id
        """
        params = []
        conditions = []
        user_id = self._active_user_id()
        if user_id:
            conditions.append("scheduler_items.user_id = ?")
            params.append(user_id)
        if status:
            conditions.append("scheduler_items.status = ?")
            params.append(status)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += """
            ORDER BY
                CASE
                    WHEN scheduler_items.scheduled_for IS NULL OR scheduler_items.scheduled_for = '' THEN 1
                    ELSE 0
                END,
                scheduler_items.scheduled_for ASC,
                scheduler_items.created_at DESC
            LIMIT ?
        """
        params.append(limit)
        cursor.execute(query, params)
        items = cursor.fetchall()
        self.close()
        return items

    def get_recent_completed_scheduler_items(self, limit=10):
        """Get recently completed scheduler items for the active user."""
        self.connect()
        cursor = self.conn.cursor()
        query = """
            SELECT scheduler_items.*, projects.name AS project_name, recommended_actions.action AS action_title
            FROM scheduler_items
            LEFT JOIN projects ON projects.id = scheduler_items.project_id
            LEFT JOIN recommended_actions ON recommended_actions.id = scheduler_items.action_id
            WHERE scheduler_items.status = 'done'
        """
        params = []
        user_id = self._active_user_id()
        if user_id:
            query += " AND scheduler_items.user_id = ?"
            params.append(user_id)
        query += """
            ORDER BY
                CASE
                    WHEN scheduler_items.completed_at IS NULL OR scheduler_items.completed_at = '' THEN 1
                    ELSE 0
                END,
                scheduler_items.completed_at DESC,
                scheduler_items.updated_at DESC
            LIMIT ?
        """
        params.append(limit)
        cursor.execute(query, params)
        items = cursor.fetchall()
        self.close()
        return items

    def get_scheduler_item(self, item_id):
        """Get a scheduler item by id."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM scheduler_items WHERE id = ?", (item_id,))
        item = cursor.fetchone()
        self.close()
        return item

    def add_scheduler_item(
        self,
        title,
        context_label="",
        scheduled_for="",
        notes="",
        source="",
        project_id=None,
        action_id=None,
    ):
        """Add a scheduler/agenda item."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO scheduler_items
                (title, context_label, scheduled_for, notes, source, project_id, action_id, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (title, context_label, scheduled_for, notes, source, project_id, action_id, self._active_user_id()),
        )
        self._commit()
        item_id = cursor.lastrowid
        self.close()
        return item_id

    def update_scheduler_item(
        self,
        item_id,
        title=None,
        context_label=None,
        scheduled_for=None,
        notes=None,
        status=None,
    ):
        """Update editable scheduler item fields."""
        updates = []
        values = []
        fields = {
            "title": title,
            "context_label": context_label,
            "scheduled_for": scheduled_for,
            "notes": notes,
            "status": status,
        }
        for field, value in fields.items():
            if value is not None:
                updates.append(f"{field} = ?")
                values.append(value)
        if status == "done":
            updates.append("completed_at = CURRENT_TIMESTAMP")
        elif status == "open":
            updates.append("completed_at = ''")
        if not updates:
            return
        updates.append("updated_at = CURRENT_TIMESTAMP")
        self.connect()
        cursor = self.conn.cursor()
        active_user_id = self._active_user_id()
        values.append(item_id)
        where_clause = "id = ?"
        if active_user_id:
            where_clause += " AND user_id = ?"
            values.append(active_user_id)
        cursor.execute(
            f"UPDATE scheduler_items SET {', '.join(updates)} WHERE {where_clause}",
            values,
        )
        self._commit()
        self.close()

    def mark_scheduler_item_complete(self, item_id):
        """Mark a scheduler item complete."""
        self.update_scheduler_item(item_id, status="done")

    def reopen_scheduler_item(self, item_id):
        """Reopen a completed scheduler item."""
        self.update_scheduler_item(item_id, status="open")

    def delete_scheduler_item(self, item_id):
        """Delete a scheduler item for the active user."""
        self.connect()
        cursor = self.conn.cursor()
        active_user_id = self._active_user_id()
        if active_user_id:
            cursor.execute(
                "DELETE FROM scheduler_items WHERE id = ? AND user_id = ?",
                (item_id, active_user_id),
            )
        else:
            cursor.execute("DELETE FROM scheduler_items WHERE id = ?", (item_id,))
        self._commit()
        self.close()

    def get_blockers(self, project_id):
        """Get all blockers for a project."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM blockers WHERE project_id = ? ORDER BY severity DESC", (project_id,))
        blockers = cursor.fetchall()
        self.close()
        return blockers

    def add_blocker(self, project_id, description, severity="medium"):
        """Add a blocker to a project."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO blockers (project_id, description, severity) VALUES (?, ?, ?)",
            (project_id, description, severity),
        )
        self._commit()
        self.close()

    def get_recommended_actions(self, project_id):
        """Get recommended actions for a project."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM recommended_actions
            WHERE project_id = ?
            ORDER BY
                CASE status
                    WHEN 'open' THEN 1
                    WHEN 'done' THEN 2
                    WHEN 'archived' THEN 3
                    ELSE 4
                END,
                CASE priority
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                    ELSE 4
                END,
                sort_order ASC,
                id DESC
            """,
            (project_id,),
        )
        actions = cursor.fetchall()
        self.close()
        return actions

    def get_open_recommended_actions(self, project_id):
        """Get open recommended actions for a project."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM recommended_actions
            WHERE project_id = ? AND status = 'open'
            ORDER BY
                CASE priority
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                    ELSE 4
                END,
                sort_order ASC,
                id DESC
            """,
            (project_id,),
        )
        actions = cursor.fetchall()
        self.close()
        return actions

    def add_recommended_action(self, project_id, action, priority="medium"):
        """Add a recommended action."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO recommended_actions (project_id, action, priority) VALUES (?, ?, ?)",
            (project_id, action, priority),
        )
        self._commit()
        action_id = cursor.lastrowid
        self.close()
        return action_id

    def get_recommended_action(self, action_id):
        """Get a recommended action by id."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM recommended_actions WHERE id = ?", (action_id,))
        action = cursor.fetchone()
        self.close()
        return action

    def update_recommended_action_priority(self, action_id, priority):
        """Update a recommended action's priority."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE recommended_actions SET priority = ? WHERE id = ?",
            (priority, action_id),
        )
        self._commit()
        self.close()

    def update_recommended_action_text(self, action_id, action=None, priority=None):
        """Update editable recommended action fields."""
        updates = []
        values = []
        if action is not None:
            updates.append("action = ?")
            values.append(action)
        if priority is not None:
            updates.append("priority = ?")
            values.append(priority)
        if not updates:
            return
        values.append(action_id)
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            f"UPDATE recommended_actions SET {', '.join(updates)} WHERE id = ?",
            values,
        )
        self._commit()
        self.close()

    def update_recommended_action_order(self, action_id, sort_order):
        """Update a recommended action's ordering within its priority bucket."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE recommended_actions SET sort_order = ? WHERE id = ?",
            (sort_order, action_id),
        )
        self._commit()
        self.close()

    def mark_recommended_action_complete(self, action_id):
        """Mark a recommended action as done."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE recommended_actions
            SET status = 'done', completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (action_id,),
        )
        self._commit()
        self.close()

    def reopen_recommended_action(self, action_id):
        """Reopen a completed recommended action."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE recommended_actions
            SET status = 'open', completed_at = ''
            WHERE id = ?
            """,
            (action_id,),
        )
        self._commit()
        self.close()

    def find_recommended_action(self, project_id, action):
        """Find an action by exact text."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM recommended_actions WHERE project_id = ? AND lower(action) = lower(?)",
            (project_id, action),
        )
        row = cursor.fetchone()
        self.close()
        return row

    def get_task_steps(self, action_id):
        """Get all checklist steps for a task."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM task_steps
            WHERE action_id = ? AND status != 'archived'
            ORDER BY
                CASE status WHEN 'open' THEN 1 WHEN 'done' THEN 2 ELSE 3 END,
                sort_order ASC,
                id ASC
            """,
            (action_id,),
        )
        steps = cursor.fetchall()
        self.close()
        return steps

    def add_task_step(self, action_id, step):
        """Add a checklist step to a task."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 10 AS next_order FROM task_steps WHERE action_id = ?",
            (action_id,),
        )
        sort_order = cursor.fetchone()["next_order"]
        cursor.execute(
            "INSERT INTO task_steps (action_id, step, sort_order) VALUES (?, ?, ?)",
            (action_id, step, sort_order),
        )
        step_id = cursor.lastrowid
        cursor.execute(
            """
            UPDATE recommended_actions
            SET status = 'open', completed_at = ''
            WHERE id = ?
            """,
            (action_id,),
        )
        self._commit()
        self.close()
        return step_id

    def update_task_step_text(self, step_id, step):
        """Update the wording for a checklist step."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE task_steps SET step = ? WHERE id = ?",
            (step, step_id),
        )
        self._commit()
        self.close()

    def mark_task_step_complete(self, step_id):
        """Mark a checklist step as done and complete the parent task if all steps are done."""
        self.connect()
        cursor = self.conn.cursor()
        row = cursor.execute(
            "SELECT action_id FROM task_steps WHERE id = ?",
            (step_id,),
        ).fetchone()
        if not row:
            self.close()
            return

        action_id = row["action_id"]
        cursor.execute(
            """
            UPDATE task_steps
            SET status = 'done', completed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (step_id,),
        )
        cursor.execute(
            """
            SELECT COUNT(*) AS count
            FROM task_steps
            WHERE action_id = ? AND status = 'open'
            """,
            (action_id,),
        )
        open_count = cursor.fetchone()["count"]
        if open_count == 0:
            cursor.execute(
                """
                UPDATE recommended_actions
                SET status = 'done', completed_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (action_id,),
            )
        self._commit()
        self.close()

    def reopen_task_step(self, step_id):
        """Reopen a done checklist step and reopen its parent task."""
        self.connect()
        cursor = self.conn.cursor()
        row = cursor.execute(
            "SELECT action_id FROM task_steps WHERE id = ?",
            (step_id,),
        ).fetchone()
        if not row:
            self.close()
            return

        action_id = row["action_id"]
        cursor.execute(
            """
            UPDATE task_steps
            SET status = 'open', completed_at = ''
            WHERE id = ?
            """,
            (step_id,),
        )
        cursor.execute(
            """
            UPDATE recommended_actions
            SET status = 'open', completed_at = ''
            WHERE id = ?
            """,
            (action_id,),
        )
        self._commit()
        self.close()

    def create_task_step_review(self, action_id, summary, payload):
        """Store a pending step cleanup review."""
        import json

        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO task_step_reviews (action_id, summary, payload)
            VALUES (?, ?, ?)
            """,
            (action_id, summary, json.dumps(payload)),
        )
        self._commit()
        review_id = cursor.lastrowid
        self.close()
        return review_id

    def get_task_step_review(self, review_id):
        """Get a stored task step cleanup review."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM task_step_reviews WHERE id = ?", (review_id,))
        review = cursor.fetchone()
        self.close()
        return review

    def apply_task_step_review(self, review_id):
        """Apply a task step cleanup review by replacing open steps."""
        import json

        self.connect()
        cursor = self.conn.cursor()
        review = cursor.execute(
            "SELECT * FROM task_step_reviews WHERE id = ?",
            (review_id,),
        ).fetchone()
        if not review or review["status"] != "pending":
            self.close()
            return False

        payload = json.loads(review["payload"])
        action_id = review["action_id"]
        cursor.execute(
            """
            UPDATE task_steps
            SET status = 'archived'
            WHERE action_id = ? AND status = 'open'
            """,
            (action_id,),
        )

        for index, step in enumerate(payload.get("proposed_steps", []), 1):
            cursor.execute(
                """
                INSERT INTO task_steps (action_id, step, sort_order)
                VALUES (?, ?, ?)
                """,
                (action_id, step, index * 10),
            )

        cursor.execute(
            """
            UPDATE task_step_reviews
            SET status = 'applied', applied_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (review_id,),
        )
        self._commit()
        self.close()
        return True

    def get_recipe_images(self, action_id):
        """Get uploaded recipe images for a task."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM recipe_images WHERE action_id = ? ORDER BY id DESC",
            (action_id,),
        )
        images = cursor.fetchall()
        self.close()
        return images

    def get_recipe_image_groups(self, action_id):
        """Get recipe image groups with their assigned images."""
        self.connect()
        cursor = self.conn.cursor()
        user_id = self._active_user_id()
        cursor.execute(
            """
            SELECT
                recipe_image_groups.*,
                recipe_extractions.status AS extraction_status,
                recipe_extractions.ingredients_text,
                recipe_extractions.instructions_text,
                recipe_extractions.sections_json,
                recipe_extractions.error AS extraction_error
            FROM recipe_image_groups
            LEFT JOIN recipe_extractions
                ON recipe_extractions.group_id = recipe_image_groups.id
            WHERE action_id = ?
              AND (? IS NULL OR recipe_image_groups.user_id = ?)
            ORDER BY recipe_image_groups.id DESC
            """,
            (action_id, user_id, user_id),
        )
        groups = []
        for group in cursor.fetchall():
            group_data = dict(group)
            cursor.execute(
                """
                SELECT * FROM recipe_images
                WHERE group_id = ?
                ORDER BY CASE side WHEN 'front' THEN 1 WHEN 'back' THEN 2 ELSE 3 END, id ASC
                """,
                (group["id"],),
            )
            group_data["images"] = [dict(row) for row in cursor.fetchall()]
            groups.append(group_data)
        self.close()
        return groups

    def create_recipe_image_group(self, project_id, action_id, label="", layout="front_back"):
        """Create a recipe image group."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO recipe_image_groups (project_id, action_id, layout, label, user_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (project_id, action_id, layout, label, self._active_user_id()),
        )
        self._commit()
        group_id = cursor.lastrowid
        self.close()
        return group_id

    def add_recipe_image(self, project_id, action_id, filename, original_filename, content_type="", group_id=None, side=""):
        """Record an uploaded recipe image."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO recipe_images
                (project_id, action_id, group_id, side, filename, original_filename, content_type, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, action_id, group_id, side, filename, original_filename, content_type, self._active_user_id()),
        )
        self._commit()
        self.close()

    def get_recipe_image(self, image_id):
        """Get a single uploaded recipe image."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM recipe_images WHERE id = ?", (image_id,))
        image = cursor.fetchone()
        self.close()
        return image

    def update_recipe_image_assignment(self, image_id, group_id, side):
        """Move an image to a recipe group and update its role."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE recipe_images
            SET group_id = ?, side = ?
            WHERE id = ?
            """,
            (group_id, side, image_id),
        )
        self._commit()
        self.close()

    def upsert_recipe_extraction(
        self,
        group_id,
        status,
        ingredients_text="",
        instructions_text="",
        sections_json="[]",
        raw_response="",
        error="",
    ):
        """Create or update OCR/extraction output for a recipe image group."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO recipe_extractions
                (group_id, status, ingredients_text, instructions_text, sections_json, raw_response, error, updated_at, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                status = excluded.status,
                ingredients_text = excluded.ingredients_text,
                instructions_text = excluded.instructions_text,
                sections_json = excluded.sections_json,
                raw_response = excluded.raw_response,
                error = excluded.error,
                user_id = COALESCE(recipe_extractions.user_id, excluded.user_id),
                updated_at = CURRENT_TIMESTAMP
            """,
            (group_id, status, ingredients_text, instructions_text, sections_json, raw_response, error, self._active_user_id()),
        )
        self._commit()
        self.close()

    def sync_recipe_complete_meals_from_extractions(self):
        """Copy extracted card-level recipes into the complete meals pathway."""
        self.connect()
        cursor = self.conn.cursor()
        user_id = self._active_user_id()
        cursor.execute(
            """
            SELECT
                recipe_image_groups.id AS group_id,
                recipe_image_groups.user_id,
                recipe_image_groups.label,
                recipe_image_groups.layout,
                recipe_extractions.status AS extraction_status,
                recipe_extractions.ingredients_text,
                recipe_extractions.instructions_text,
                recipe_extractions.sections_json,
                recipe_extractions.error AS extraction_error
            FROM recipe_image_groups
            LEFT JOIN recipe_extractions
                ON recipe_extractions.group_id = recipe_image_groups.id
            WHERE (? IS NULL OR recipe_image_groups.user_id = ?)
            """
            ,
            (user_id, user_id),
        )
        groups = cursor.fetchall()

        for group in groups:
            if not group["extraction_status"]:
                continue
            image_roles = self._get_recipe_image_roles(cursor, group["group_id"])
            quality_notes = self._build_complete_meal_quality_notes(group, image_roles)
            status = "ready" if not quality_notes else "needs_review"
            title = self._title_from_sections_json(
                group["sections_json"],
                group["label"] or "Complete meal",
            )
            cursor.execute(
                """
                INSERT INTO recipe_complete_meals
                    (source_group_id, source_kind, title, ingredients_text, instructions_text, status, quality_notes_json, updated_at, user_id)
                VALUES (?, 'card', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                ON CONFLICT(source_group_id) DO UPDATE SET
                    source_kind = 'card',
                    title = excluded.title,
                    ingredients_text = excluded.ingredients_text,
                    instructions_text = excluded.instructions_text,
                    status = excluded.status,
                    quality_notes_json = excluded.quality_notes_json,
                    user_id = COALESCE(recipe_complete_meals.user_id, excluded.user_id),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    group["group_id"],
                    title,
                    group["ingredients_text"] or "",
                    group["instructions_text"] or "",
                    status,
                    json.dumps(quality_notes),
                    group["user_id"] or user_id,
                ),
            )

        self._commit()
        self.close()

    @staticmethod
    def _get_recipe_image_roles(cursor, group_id):
        cursor.execute(
            "SELECT side FROM recipe_images WHERE group_id = ?",
            (group_id,),
        )
        return {row["side"] for row in cursor.fetchall()}

    @staticmethod
    def _build_complete_meal_quality_notes(group, image_roles):
        notes = []
        is_pdf_import = group["layout"] == "pdf"
        if not is_pdf_import:
            if "front" not in image_roles:
                notes.append("Add or label a front image for ingredients.")
            if "back" not in image_roles:
                notes.append("Add or label a back image for steps.")

        extraction_status = group["extraction_status"]
        if extraction_status is None:
            notes.append("Run OCR/scrape for this card pair.")
        elif extraction_status != "extracted":
            error = group["extraction_error"] or "OCR did not complete cleanly."
            notes.append(error)

        ingredients_text = (group["ingredients_text"] or "").strip()
        instructions_text = (group["instructions_text"] or "").strip()
        if extraction_status == "extracted" and not ingredients_text:
            notes.append("Ingredients could not be read clearly from the import.")
        if extraction_status == "extracted" and not instructions_text:
            notes.append("Steps could not be read clearly from the import.")
        return notes

    @staticmethod
    def _title_from_sections_json(sections_json, fallback):
        try:
            sections = json.loads(sections_json or "[]")
        except json.JSONDecodeError:
            sections = []

        for section in sections:
            title = str(section.get("title") or "").strip()
            if title:
                return title
        return fallback

    def get_recipe_complete_meals(self):
        """Get faithful complete-meal records copied from recipe cards."""
        self.connect()
        cursor = self.conn.cursor()
        user_id = self._active_user_id()
        cursor.execute(
            """
            SELECT DISTINCT
                recipe_complete_meals.*,
                recipe_image_groups.label AS source_label,
                recipe_image_groups.layout AS source_layout,
                (
                    SELECT recipe_images.filename
                    FROM recipe_images
                    WHERE recipe_images.group_id = recipe_complete_meals.source_group_id
                    ORDER BY recipe_images.id DESC
                    LIMIT 1
                ) AS thumbnail_filename,
                (
                    SELECT GROUP_CONCAT(recipe_images.filename, '||')
                    FROM recipe_images
                    WHERE recipe_images.group_id = recipe_complete_meals.source_group_id
                ) AS thumbnail_candidates
                ,
                CASE WHEN recipe_complete_meals.user_id = ? THEN 1 ELSE 0 END AS is_owner,
                CASE WHEN recipe_favorites.id IS NULL THEN 0 ELSE 1 END AS is_favorite
            FROM recipe_complete_meals
            LEFT JOIN recipe_image_groups
                ON recipe_image_groups.id = recipe_complete_meals.source_group_id
            LEFT JOIN recipe_shares
                ON recipe_shares.recipe_kind = 'meal'
                AND recipe_shares.recipe_id = recipe_complete_meals.id
                AND recipe_shares.shared_with_user_id = ?
            LEFT JOIN recipe_favorites
                ON recipe_favorites.recipe_kind = 'meal'
                AND recipe_favorites.recipe_id = recipe_complete_meals.id
                AND recipe_favorites.user_id = ?
            WHERE (
                ? IS NULL
                OR COALESCE(recipe_complete_meals.visibility, 'shared') = 'shared'
                OR recipe_complete_meals.user_id = ?
                OR recipe_shares.shared_with_user_id = ?
            )
              AND (COALESCE(recipe_complete_meals.visibility, 'shared') != 'private' OR recipe_complete_meals.user_id = ?)
            ORDER BY recipe_complete_meals.updated_at DESC, recipe_complete_meals.id DESC
            """
            ,
            (user_id, user_id, user_id, user_id, user_id, user_id, user_id),
        )
        meals = cursor.fetchall()
        self.close()
        return meals

    def get_recipe_complete_meal(self, meal_id):
        """Get one complete meal."""
        self.connect()
        cursor = self.conn.cursor()
        user_id = self._active_user_id()
        cursor.execute(
            """
            SELECT DISTINCT
                recipe_complete_meals.*,
                recipe_image_groups.label AS source_label,
                recipe_image_groups.layout AS source_layout,
                (
                    SELECT recipe_images.filename
                    FROM recipe_images
                    WHERE recipe_images.group_id = recipe_complete_meals.source_group_id
                    ORDER BY recipe_images.id DESC
                    LIMIT 1
                ) AS thumbnail_filename,
                (
                    SELECT GROUP_CONCAT(recipe_images.filename, '||')
                    FROM recipe_images
                    WHERE recipe_images.group_id = recipe_complete_meals.source_group_id
                ) AS thumbnail_candidates
                ,
                CASE WHEN recipe_complete_meals.user_id = ? THEN 1 ELSE 0 END AS is_owner,
                CASE WHEN recipe_favorites.id IS NULL THEN 0 ELSE 1 END AS is_favorite
            FROM recipe_complete_meals
            LEFT JOIN recipe_image_groups
                ON recipe_image_groups.id = recipe_complete_meals.source_group_id
            LEFT JOIN recipe_shares
                ON recipe_shares.recipe_kind = 'meal'
                AND recipe_shares.recipe_id = recipe_complete_meals.id
                AND recipe_shares.shared_with_user_id = ?
            LEFT JOIN recipe_favorites
                ON recipe_favorites.recipe_kind = 'meal'
                AND recipe_favorites.recipe_id = recipe_complete_meals.id
                AND recipe_favorites.user_id = ?
            WHERE recipe_complete_meals.id = ?
              AND (
                ? IS NULL
                OR COALESCE(recipe_complete_meals.visibility, 'shared') = 'shared'
                OR recipe_complete_meals.user_id = ?
                OR recipe_shares.shared_with_user_id = ?
              )
              AND (COALESCE(recipe_complete_meals.visibility, 'shared') != 'private' OR recipe_complete_meals.user_id = ?)
            """,
            (user_id, user_id, user_id, meal_id, user_id, user_id, user_id, user_id),
        )
        meal = cursor.fetchone()
        self.close()
        return meal

    def update_recipe_complete_meal_edits(self, meal_id, title=None, ingredients_text=None, instructions_text=None):
        """Store Dieter-edited complete meal text without overwriting original OCR text."""
        updates = []
        params = []
        if title is not None:
            updates.append("edited_title = ?")
            params.append(title)
        if ingredients_text is not None:
            updates.append("edited_ingredients_text = ?")
            params.append(ingredients_text)
        if instructions_text is not None:
            updates.append("edited_instructions_text = ?")
            params.append(instructions_text)
        if not updates:
            return
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(meal_id)
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            f"UPDATE recipe_complete_meals SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        self._commit()
        self.close()

    def create_saved_recipe_meal(self, title, ingredients_text, instructions_text, visibility="shared"):
        """Create a complete meal assembled from selected recipe components."""
        self.connect()
        cursor = self.conn.cursor()
        normalized_title = self._normalized_recipe_title(title)
        existing = cursor.execute(
            """
            SELECT id, title FROM recipe_complete_meals
            WHERE LOWER(TRIM(title)) = ?
              AND (? IS NULL OR user_id = ?)
            ORDER BY source_kind = 'saved' DESC, id ASC
            LIMIT 1
            """,
            (normalized_title, self._active_user_id(), self._active_user_id()),
        ).fetchone()
        if existing:
            self.close()
            return existing["id"]
        cursor.execute("SELECT COALESCE(MIN(source_group_id), 0) - 1 FROM recipe_complete_meals")
        source_group_id = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO recipe_complete_meals
                (source_group_id, source_kind, title, ingredients_text, instructions_text, status, quality_notes_json, updated_at, user_id, visibility)
            VALUES (?, 'saved', ?, ?, ?, 'ready', '[]', CURRENT_TIMESTAMP, ?, ?)
            """,
            (source_group_id, title, ingredients_text, instructions_text, self._active_user_id(), visibility),
        )
        meal_id = cursor.lastrowid
        self._commit()
        self.close()
        return meal_id

    def cleanup_duplicate_recipes(self):
        """Remove duplicate complete meals and reusable components."""
        self.connect()
        cursor = self.conn.cursor()

        meals = cursor.execute(
            """
            SELECT
                recipe_complete_meals.*,
                (
                    SELECT COUNT(*)
                    FROM recipe_components
                    WHERE recipe_components.source_meal_id = recipe_complete_meals.id
                ) AS component_count
            FROM recipe_complete_meals
            ORDER BY id ASC
            """
        ).fetchall()
        meal_groups = {}
        for meal in meals:
            key = self._normalized_recipe_title(meal["title"])
            if key:
                meal_groups.setdefault(key, []).append(meal)

        deleted_meals = 0
        for duplicates in meal_groups.values():
            if len(duplicates) <= 1:
                continue

            def meal_score(meal):
                return (
                    1 if meal["status"] == "ready" else 0,
                    int(meal["component_count"] or 0),
                    len(meal["instructions_text"] or ""),
                    len(meal["ingredients_text"] or ""),
                    1 if meal["source_kind"] == "saved" else 0,
                    -int(meal["id"]),
                )

            keeper = max(duplicates, key=meal_score)
            for meal in duplicates:
                if meal["id"] == keeper["id"]:
                    continue
                cursor.execute("DELETE FROM recipe_components WHERE source_meal_id = ?", (meal["id"],))
                if meal["source_kind"] == "card" and meal["source_group_id"] is not None:
                    cursor.execute("DELETE FROM recipe_extractions WHERE group_id = ?", (meal["source_group_id"],))
                cursor.execute("DELETE FROM recipe_complete_meals WHERE id = ?", (meal["id"],))
                deleted_meals += 1

        components = cursor.execute("SELECT * FROM recipe_components ORDER BY id ASC").fetchall()
        component_groups = {}
        for component in components:
            key = (
                self._normalized_recipe_title(component["title"]),
                component["component_type"] or "other",
            )
            if key[0]:
                component_groups.setdefault(key, []).append(component)

        deleted_components = 0
        for duplicates in component_groups.values():
            if len(duplicates) <= 1:
                continue

            def component_score(component):
                return (
                    len(component["structured_ingredients_json"] or "[]"),
                    len(component["instructions_text"] or ""),
                    len(component["ingredients_text"] or ""),
                    -int(component["id"]),
                )

            keeper = max(duplicates, key=component_score)
            for component in duplicates:
                if component["id"] == keeper["id"]:
                    continue
                cursor.execute("DELETE FROM recipe_components WHERE id = ?", (component["id"],))
                deleted_components += 1

        self._commit()
        self.close()
        return {
            "deleted_meals": deleted_meals,
            "deleted_components": deleted_components,
        }

    def cleanup_empty_recipe_placeholders(self):
        """Remove empty Recipe Pair placeholders from complete meals."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            DELETE FROM recipe_complete_meals
            WHERE source_kind = 'card'
              AND LOWER(TRIM(title)) LIKE 'recipe pair %'
              AND TRIM(COALESCE(ingredients_text, '')) = ''
              AND TRIM(COALESCE(instructions_text, '')) = ''
            """
        )
        deleted = cursor.rowcount
        self._commit()
        self.close()
        return deleted

    def replace_recipe_components_for_meal(self, meal_id, components):
        """Replace analyzed components for one complete meal."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM recipe_components WHERE source_meal_id = ?", (meal_id,))
        for component in components:
            cursor.execute(
                """
                INSERT INTO recipe_components
                    (source_meal_id, title, component_type, ingredients_text, structured_ingredients_json, instructions_text, status, updated_at, user_id)
                VALUES (?, ?, ?, ?, ?, ?, 'draft', CURRENT_TIMESTAMP, ?)
                """,
                (
                    meal_id,
                    component.get("title", ""),
                    component.get("component_type", "other"),
                    component.get("ingredients_text", ""),
                    json.dumps(component.get("structured_ingredients", [])),
                    component.get("instructions_text", ""),
                    self._active_user_id(),
                ),
            )
        self._commit()
        self.close()

    def get_recipe_components(self):
        """Get analyzed meal components such as sides, mains, sauces, and toppings."""
        self.connect()
        cursor = self.conn.cursor()
        user_id = self._active_user_id()
        cursor.execute(
            """
            SELECT DISTINCT
                recipe_components.*,
                recipe_complete_meals.title AS source_meal_title,
                recipe_complete_meals.ingredients_text AS source_meal_ingredients_text,
                recipe_complete_meals.instructions_text AS source_meal_instructions_text,
                recipe_complete_meals.source_group_id AS source_group_id,
                CASE WHEN recipe_components.user_id = ? THEN 1 ELSE 0 END AS is_owner,
                CASE WHEN recipe_favorites.id IS NULL THEN 0 ELSE 1 END AS is_favorite
            FROM recipe_components
            LEFT JOIN recipe_complete_meals
                ON recipe_complete_meals.id = recipe_components.source_meal_id
            LEFT JOIN recipe_shares
                ON recipe_shares.recipe_kind = 'component'
                AND recipe_shares.recipe_id = recipe_components.id
                AND recipe_shares.shared_with_user_id = ?
            LEFT JOIN recipe_favorites
                ON recipe_favorites.recipe_kind = 'component'
                AND recipe_favorites.recipe_id = recipe_components.id
                AND recipe_favorites.user_id = ?
            WHERE (
                ? IS NULL
                OR COALESCE(recipe_components.visibility, 'shared') = 'shared'
                OR recipe_components.user_id = ?
                OR recipe_shares.shared_with_user_id = ?
            )
              AND (COALESCE(recipe_components.visibility, 'shared') != 'private' OR recipe_components.user_id = ?)
            ORDER BY recipe_components.updated_at DESC, recipe_components.id DESC
            """
            ,
            (user_id, user_id, user_id, user_id, user_id, user_id, user_id),
        )
        components = cursor.fetchall()
        self.close()
        return components

    def get_recipe_component(self, component_id):
        """Get one analyzed meal component."""
        self.connect()
        cursor = self.conn.cursor()
        user_id = self._active_user_id()
        cursor.execute(
            """
            SELECT DISTINCT
                recipe_components.*,
                recipe_complete_meals.title AS source_meal_title,
                recipe_complete_meals.ingredients_text AS source_meal_ingredients_text,
                recipe_complete_meals.instructions_text AS source_meal_instructions_text,
                recipe_complete_meals.source_group_id AS source_group_id,
                CASE WHEN recipe_components.user_id = ? THEN 1 ELSE 0 END AS is_owner,
                CASE WHEN recipe_favorites.id IS NULL THEN 0 ELSE 1 END AS is_favorite
            FROM recipe_components
            LEFT JOIN recipe_complete_meals
                ON recipe_complete_meals.id = recipe_components.source_meal_id
            LEFT JOIN recipe_shares
                ON recipe_shares.recipe_kind = 'component'
                AND recipe_shares.recipe_id = recipe_components.id
                AND recipe_shares.shared_with_user_id = ?
            LEFT JOIN recipe_favorites
                ON recipe_favorites.recipe_kind = 'component'
                AND recipe_favorites.recipe_id = recipe_components.id
                AND recipe_favorites.user_id = ?
            WHERE recipe_components.id = ?
              AND (
                ? IS NULL
                OR COALESCE(recipe_components.visibility, 'shared') = 'shared'
                OR recipe_components.user_id = ?
                OR recipe_shares.shared_with_user_id = ?
              )
              AND (COALESCE(recipe_components.visibility, 'shared') != 'private' OR recipe_components.user_id = ?)
            """,
            (user_id, user_id, user_id, component_id, user_id, user_id, user_id, user_id),
        )
        component = cursor.fetchone()
        self.close()
        return component

    def update_recipe_component_edits(self, component_id, title=None, ingredients_text=None, instructions_text=None):
        """Store Dieter-edited component text without overwriting analyzed source text."""
        updates = []
        params = []
        if title is not None:
            updates.append("edited_title = ?")
            params.append(title)
        if ingredients_text is not None:
            updates.append("edited_ingredients_text = ?")
            params.append(ingredients_text)
        if instructions_text is not None:
            updates.append("edited_instructions_text = ?")
            params.append(instructions_text)
        if not updates:
            return
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(component_id)
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            f"UPDATE recipe_components SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        self._commit()
        self.close()

    def set_recipe_favorite(self, recipe_kind, recipe_id, is_favorite=True):
        """Set or clear the active user's favorite marker for a recipe."""
        user_id = self._active_user_id()
        if not user_id:
            return
        self.connect()
        cursor = self.conn.cursor()
        if is_favorite:
            cursor.execute(
                """
                INSERT OR IGNORE INTO recipe_favorites (recipe_kind, recipe_id, user_id)
                VALUES (?, ?, ?)
                """,
                (recipe_kind, recipe_id, user_id),
            )
        else:
            cursor.execute(
                "DELETE FROM recipe_favorites WHERE recipe_kind = ? AND recipe_id = ? AND user_id = ?",
                (recipe_kind, recipe_id, user_id),
            )
        self._commit()
        self.close()

    def add_recipe_variation(self, recipe_kind, recipe_id, title, ingredients_text, instructions_text, summary, threshold=2):
        """Record an edited recipe as a candidate variation."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO recipe_variations
                (recipe_kind, recipe_id, title, ingredients_text, instructions_text, summary, promotion_threshold, created_by_user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                recipe_kind,
                recipe_id,
                title,
                ingredients_text,
                instructions_text,
                summary,
                threshold,
                self._active_user_id(),
            ),
        )
        variation_id = cursor.lastrowid
        self._commit()
        self.close()
        return variation_id

    def upvote_recipe_variation(self, variation_id):
        """Upvote a recipe variation and move it to review-ready at threshold."""
        user_id = self._active_user_id()
        if not user_id:
            return None
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT OR IGNORE INTO recipe_variation_votes (variation_id, user_id, vote)
            VALUES (?, ?, 'up')
            """,
            (variation_id, user_id),
        )
        cursor.execute(
            """
            UPDATE recipe_variations
            SET upvote_count = (
                    SELECT COUNT(*)
                    FROM recipe_variation_votes
                    WHERE recipe_variation_votes.variation_id = recipe_variations.id
                      AND recipe_variation_votes.vote = 'up'
                ),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (variation_id,),
        )
        cursor.execute(
            """
            UPDATE recipe_variations
            SET review_status = 'ready_for_review'
            WHERE id = ?
              AND upvote_count >= promotion_threshold
              AND review_status = 'pending'
            """,
            (variation_id,),
        )
        variation = cursor.execute("SELECT * FROM recipe_variations WHERE id = ?", (variation_id,)).fetchone()
        self._commit()
        self.close()
        return variation

    def add_recipe_change_log(self, recipe_kind, recipe_id, user_message, summary, changed_fields, before, after, model=""):
        """Record a structured Dieter recipe edit."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO recipe_change_log
                (recipe_kind, recipe_id, user_message, summary, changed_fields_json, before_json, after_json, model, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                recipe_kind,
                recipe_id,
                user_message,
                summary,
                json.dumps(changed_fields or []),
                json.dumps(before or {}),
                json.dumps(after or {}),
                model,
                self._active_user_id(),
            ),
        )
        change_id = cursor.lastrowid
        self._commit()
        self.close()
        return change_id

    def get_recipe_change_log(self, recipe_kind, recipe_id):
        """Get Dieter edit history for a complete meal or component."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM recipe_change_log
            WHERE recipe_kind = ? AND recipe_id = ?
              AND (? IS NULL OR user_id = ?)
            ORDER BY created_at DESC, id DESC
            """,
            (recipe_kind, recipe_id, self._active_user_id(), self._active_user_id()),
        )
        rows = cursor.fetchall()
        self.close()
        return rows

    def add_recipe_meal_plan_item(self, source_kind, title, source_id=None, component_ids=None):
        """Add a complete meal or component bundle to the pending meal plan."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO recipe_meal_plan_items
                (source_kind, source_id, title, component_ids_json, status, user_id)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (
                source_kind,
                source_id,
                title,
                json.dumps(component_ids or []),
                self._active_user_id(),
            ),
        )
        item_id = cursor.lastrowid
        self._commit()
        self.close()
        return item_id

    def get_recipe_meal_plan_item(self, item_id):
        """Get one meal plan item."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM recipe_meal_plan_items WHERE id = ? AND (? IS NULL OR user_id = ?)",
            (item_id, self._active_user_id(), self._active_user_id()),
        )
        item = cursor.fetchone()
        self.close()
        return item

    def get_recipe_meal_plan_items(self, status="pending", limit=100):
        """List meal plan items, pending by default."""
        self.connect()
        cursor = self.conn.cursor()
        if status:
            cursor.execute(
                """
                SELECT * FROM recipe_meal_plan_items
                WHERE status = ?
                  AND (? IS NULL OR user_id = ?)
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (status, self._active_user_id(), self._active_user_id(), limit),
            )
        else:
            cursor.execute(
                """
                SELECT * FROM recipe_meal_plan_items
                WHERE (? IS NULL OR user_id = ?)
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (self._active_user_id(), self._active_user_id(), limit),
            )
        items = cursor.fetchall()
        self.close()
        return items

    def mark_recipe_meal_plan_item_cooked(self, item_id):
        """Move a meal plan item out of the pending list after cooking."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE recipe_meal_plan_items
            SET status = 'cooked', cooked_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (item_id,),
        )
        self._commit()
        self.close()

    def add_recipe_meal_feedback(self, meal_plan_item_id, source_kind="", source_id=None, title="", feedback=""):
        """Store cooking feedback for a planned meal."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO recipe_meal_feedback
                (meal_plan_item_id, source_kind, source_id, title, feedback, user_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (meal_plan_item_id, source_kind, source_id, title, feedback, self._active_user_id()),
        )
        self._commit()
        feedback_id = cursor.lastrowid
        self.close()
        return feedback_id

    def remove_recipe_meal_plan_item(self, item_id):
        """Move a meal plan item out of the pending list without marking it cooked."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE recipe_meal_plan_items
            SET status = 'removed'
            WHERE id = ? AND status = 'pending'
            """,
            (item_id,),
        )
        removed = cursor.rowcount
        self._commit()
        self.close()
        return removed

    def create_recipe_grocery_list(self, title, meal_plan_item_ids, items):
        """Persist a generated grocery list."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO recipe_grocery_lists
                (title, meal_plan_item_ids_json, items_json, user_id)
            VALUES (?, ?, ?, ?)
            """,
            (
                title,
                json.dumps(meal_plan_item_ids or []),
                json.dumps(items or []),
                self._active_user_id(),
            ),
        )
        list_id = cursor.lastrowid
        self._commit()
        self.close()
        return list_id

    def get_recipe_grocery_lists(self, limit=10, status="active"):
        """List recent grocery lists."""
        self.connect()
        cursor = self.conn.cursor()
        if status:
            cursor.execute(
                """
                SELECT * FROM recipe_grocery_lists
                WHERE status = ?
                  AND (? IS NULL OR user_id = ?)
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (status, self._active_user_id(), self._active_user_id(), limit),
            )
        else:
            cursor.execute(
                """
                SELECT * FROM recipe_grocery_lists
                WHERE (? IS NULL OR user_id = ?)
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (self._active_user_id(), self._active_user_id(), limit),
            )
        rows = cursor.fetchall()
        self.close()
        return rows

    def get_recipe_grocery_list(self, list_id):
        """Get one grocery list."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM recipe_grocery_lists WHERE id = ? AND (? IS NULL OR user_id = ?)",
            (list_id, self._active_user_id(), self._active_user_id()),
        )
        row = cursor.fetchone()
        self.close()
        return row

    def delete_recipe_grocery_list(self, list_id):
        """Delete one grocery list record."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM recipe_grocery_lists WHERE id = ?", (list_id,))
        deleted = cursor.rowcount
        self._commit()
        self.close()
        return deleted

    def update_recipe_grocery_list_items(self, list_id, items):
        """Replace the stored grocery items JSON for one list."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE recipe_grocery_lists
            SET items_json = ?
            WHERE id = ?
            """,
            (json.dumps(items or []), list_id),
        )
        self._commit()
        self.close()

    def update_recipe_grocery_list_status(self, list_id, status):
        """Update a grocery list lifecycle status."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE recipe_grocery_lists SET status = ? WHERE id = ?",
            (status, list_id),
        )
        self._commit()
        self.close()

    def get_recipe_images_for_group(self, group_id):
        """Get images assigned to one recipe card group."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM recipe_images
            WHERE group_id = ?
            ORDER BY CASE side WHEN 'front' THEN 1 WHEN 'back' THEN 2 ELSE 3 END, id ASC
            """,
            (group_id,),
        )
        images = cursor.fetchall()
        self.close()
        return images

    def update_recipe_component_structured_ingredients(self, component_id, structured_ingredients):
        """Store structured ingredient amounts for a component."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE recipe_components
            SET structured_ingredients_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (json.dumps(structured_ingredients), component_id),
        )
        self._commit()
        self.close()

    def get_weekly_goals(self, project_id):
        """Get weekly goals for a project."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM weekly_goals WHERE project_id = ? ORDER BY week_start DESC",
            (project_id,),
        )
        goals = cursor.fetchall()
        self.close()
        return goals

    def add_weekly_goal(self, project_id, goal):
        """Add a weekly goal."""
        self.connect()
        cursor = self.conn.cursor()
        week_start = datetime.now().strftime("%Y-%m-%d")
        cursor.execute(
            "INSERT INTO weekly_goals (project_id, goal, week_start) VALUES (?, ?, ?)",
            (project_id, goal, week_start),
        )
        self._commit()
        self.close()

    def mark_goal_complete(self, goal_id):
        """Mark a goal as complete."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("UPDATE weekly_goals SET completed = 1 WHERE id = ?", (goal_id,))
        self._commit()
        self.close()

    def delete_blocker(self, blocker_id):
        """Delete a blocker."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM blockers WHERE id = ?", (blocker_id,))
        self._commit()
        self.close()

    def add_planner_change_log(self, target_kind, target_id, user_message, summary, operations, before, after, model=""):
        """Record a structured planner edit made through Dieter."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO planner_change_log
                (target_kind, target_id, user_message, summary, operations_json, before_json, after_json, model, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target_kind,
                target_id,
                user_message,
                summary,
                json.dumps(operations or []),
                json.dumps(before or {}),
                json.dumps(after or {}),
                model,
                self._active_user_id(),
            ),
        )
        self._commit()
        change_id = cursor.lastrowid
        self.close()
        return change_id

    def get_trainer_workouts(self, workout_type=""):
        """List workouts in the Dieter Trainer catalog."""
        self.connect()
        cursor = self.conn.cursor()
        if workout_type:
            cursor.execute(
                """
                SELECT * FROM trainer_workouts
                WHERE workout_type = ?
                ORDER BY
                    CASE workout_type WHEN 'run' THEN 1 WHEN 'bike' THEN 2 WHEN 'strength' THEN 3 ELSE 4 END,
                    title COLLATE NOCASE
                """,
                (workout_type,),
            )
        else:
            cursor.execute(
                """
                SELECT * FROM trainer_workouts
                ORDER BY
                    CASE workout_type WHEN 'run' THEN 1 WHEN 'bike' THEN 2 WHEN 'strength' THEN 3 ELSE 4 END,
                    title COLLATE NOCASE
                """
            )
        rows = cursor.fetchall()
        self.close()
        return rows

    def get_trainer_workout(self, workout_id):
        """Get one Trainer catalog workout."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM trainer_workouts WHERE id = ?", (workout_id,))
        row = cursor.fetchone()
        self.close()
        return row

    def get_trainer_profile(self, user_id=None):
        """Get or create a Trainer profile for a user."""
        target_user_id = user_id or self._active_user_id()
        if not target_user_id:
            return None
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM trainer_profiles WHERE user_id = ?", (target_user_id,))
        row = cursor.fetchone()
        if not row:
            cursor.execute(
                "INSERT INTO trainer_profiles (user_id, mode) VALUES (?, 'athlete')",
                (target_user_id,),
            )
            self._commit()
            cursor.execute("SELECT * FROM trainer_profiles WHERE user_id = ?", (target_user_id,))
            row = cursor.fetchone()
        self.close()
        return row

    def update_trainer_mode(self, mode):
        """Set the active user's Trainer mode."""
        user_id = self._active_user_id()
        if not user_id:
            return
        mode = mode if mode in {"athlete", "coach"} else "athlete"
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO trainer_profiles (user_id, mode, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET mode = excluded.mode, updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, mode),
        )
        self._commit()
        self.close()

    def grant_trainer_coach(self, coach_user_id, permission="view"):
        """Allow another user to view the active athlete's Trainer data."""
        athlete_user_id = self._active_user_id()
        if not athlete_user_id or not coach_user_id or athlete_user_id == coach_user_id:
            return
        permission = permission if permission in {"view"} else "view"
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO trainer_coach_grants
                (athlete_user_id, coach_user_id, permission, status, updated_at)
            VALUES (?, ?, ?, 'active', CURRENT_TIMESTAMP)
            ON CONFLICT(athlete_user_id, coach_user_id) DO UPDATE SET
                permission = excluded.permission,
                status = 'active',
                updated_at = CURRENT_TIMESTAMP
            """,
            (athlete_user_id, coach_user_id, permission),
        )
        self._commit()
        self.close()

    def revoke_trainer_coach(self, grant_id):
        """Remove a coach grant owned by the active athlete."""
        athlete_user_id = self._active_user_id()
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE trainer_coach_grants SET status = 'revoked', updated_at = CURRENT_TIMESTAMP WHERE id = ? AND athlete_user_id = ?",
            (grant_id, athlete_user_id),
        )
        self._commit()
        self.close()

    def get_trainer_coach_grants_for_athlete(self):
        """List active coach grants for the active athlete."""
        athlete_user_id = self._active_user_id()
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT trainer_coach_grants.*, users.email, users.display_name
            FROM trainer_coach_grants
            JOIN users ON users.id = trainer_coach_grants.coach_user_id
            WHERE trainer_coach_grants.athlete_user_id = ?
              AND trainer_coach_grants.status = 'active'
            ORDER BY trainer_coach_grants.updated_at DESC
            """,
            (athlete_user_id,),
        )
        rows = cursor.fetchall()
        self.close()
        return rows

    def get_trainer_athletes_for_coach(self):
        """List athletes who granted the active user coach visibility."""
        coach_user_id = self._active_user_id()
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT trainer_coach_grants.*, users.email, users.display_name,
                   trainer_profiles.mode
            FROM trainer_coach_grants
            JOIN users ON users.id = trainer_coach_grants.athlete_user_id
            LEFT JOIN trainer_profiles ON trainer_profiles.user_id = users.id
            WHERE trainer_coach_grants.coach_user_id = ?
              AND trainer_coach_grants.status = 'active'
            ORDER BY users.display_name COLLATE NOCASE, users.email COLLATE NOCASE
            """,
            (coach_user_id,),
        )
        rows = cursor.fetchall()
        self.close()
        return rows

    def can_view_trainer_user(self, athlete_user_id):
        """Return whether the active user can view another athlete's Trainer data."""
        viewer_id = self._active_user_id()
        if not viewer_id or viewer_id == athlete_user_id:
            return True
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT 1
            FROM trainer_coach_grants
            WHERE athlete_user_id = ?
              AND coach_user_id = ?
              AND status = 'active'
            """,
            (athlete_user_id, viewer_id),
        )
        row = cursor.fetchone()
        self.close()
        return bool(row)

    def add_trainer_imported_workout(self, external_id, activity_type="", workout_category="", title="", started_at="", distance_meters=None, moving_time_seconds=None, elapsed_time_seconds=None, raw=None, user_id=None):
        """Store an imported Strava workout/activity for a user."""
        target_user_id = user_id or self._active_user_id()
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO trainer_imported_workouts
                (source, external_id, user_id, activity_type, workout_category, title, started_at,
                 distance_meters, moving_time_seconds, elapsed_time_seconds, raw_json, updated_at)
            VALUES ('strava', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(source, external_id, user_id) DO UPDATE SET
                activity_type = excluded.activity_type,
                workout_category = excluded.workout_category,
                title = excluded.title,
                started_at = excluded.started_at,
                distance_meters = excluded.distance_meters,
                moving_time_seconds = excluded.moving_time_seconds,
                elapsed_time_seconds = excluded.elapsed_time_seconds,
                raw_json = excluded.raw_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                str(external_id),
                target_user_id,
                activity_type,
                workout_category,
                title,
                started_at,
                distance_meters,
                moving_time_seconds,
                elapsed_time_seconds,
                json.dumps(raw or {}),
            ),
        )
        self._commit()
        row_id = cursor.lastrowid
        self.close()
        return row_id

    def get_trainer_imported_workouts(self, user_id=None, limit=50):
        """List imported Strava workouts visible to the active user."""
        target_user_id = user_id or self._active_user_id()
        if target_user_id and not self.can_view_trainer_user(target_user_id):
            return []
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT *
            FROM trainer_imported_workouts
            WHERE (? IS NULL OR user_id = ?)
            ORDER BY started_at DESC, id DESC
            LIMIT ?
            """,
            (target_user_id, target_user_id, limit),
        )
        rows = cursor.fetchall()
        self.close()
        return rows

    def get_trainer_suggested_workouts_by_category(self, limit_per_category=3):
        """Group catalog workouts for the Trainer home page."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT *
            FROM trainer_workouts
            ORDER BY
                CASE workout_category
                    WHEN 'run_threshold' THEN 1
                    WHEN 'run_speed' THEN 2
                    WHEN 'bike_tempo' THEN 3
                    WHEN 'strength_glutes' THEN 4
                    ELSE 9
                END,
                title COLLATE NOCASE
            """
        )
        grouped = {}
        for row in cursor.fetchall():
            category = row["workout_category"] or row["workout_type"]
            grouped.setdefault(category, [])
            if len(grouped[category]) < limit_per_category:
                grouped[category].append(row)
        self.close()
        return grouped

    def add_trainer_session(self, workout_id, scheduled_for="", notes=""):
        """Schedule a Trainer workout session."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO trainer_workout_sessions
                (workout_id, scheduled_for, notes, status, user_id, updated_at)
            VALUES (?, ?, ?, 'upcoming', ?, CURRENT_TIMESTAMP)
            """,
            (workout_id, scheduled_for, notes, self._active_user_id()),
        )
        session_id = cursor.lastrowid
        self._commit()
        self.close()
        return session_id

    def get_trainer_sessions(self, status="upcoming", limit=50, user_id=None):
        """List Trainer workout sessions for the active user."""
        target_user_id = user_id or self._active_user_id()
        if target_user_id and not self.can_view_trainer_user(target_user_id):
            return []
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT trainer_workout_sessions.*, trainer_workouts.title, trainer_workouts.workout_type,
                   trainer_workouts.focus, trainer_workouts.summary, trainer_workouts.details_json,
                   trainer_workouts.source_url
            FROM trainer_workout_sessions
            JOIN trainer_workouts ON trainer_workouts.id = trainer_workout_sessions.workout_id
            WHERE trainer_workout_sessions.status = ?
              AND (? IS NULL OR trainer_workout_sessions.user_id = ?)
            ORDER BY
                CASE WHEN ? = 'upcoming' THEN trainer_workout_sessions.scheduled_for END ASC,
                trainer_workout_sessions.created_at DESC
            LIMIT ?
            """,
            (status, target_user_id, target_user_id, status, limit),
        )
        rows = cursor.fetchall()
        self.close()
        return rows

    def get_trainer_session(self, session_id):
        """Get one Trainer workout session."""
        user_id = self._active_user_id()
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT trainer_workout_sessions.*, trainer_workouts.title, trainer_workouts.workout_type,
                   trainer_workouts.focus, trainer_workouts.summary, trainer_workouts.details_json,
                   trainer_workouts.source_url
            FROM trainer_workout_sessions
            JOIN trainer_workouts ON trainer_workouts.id = trainer_workout_sessions.workout_id
            WHERE trainer_workout_sessions.id = ?
              AND (? IS NULL OR trainer_workout_sessions.user_id = ?)
            """,
            (session_id, user_id, user_id),
        )
        row = cursor.fetchone()
        self.close()
        return row

    def complete_trainer_session(self, session_id, notes=""):
        """Mark a Trainer workout session complete."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE trainer_workout_sessions
            SET status = 'done',
                completed_at = CURRENT_TIMESTAMP,
                notes = CASE WHEN ? != '' THEN ? ELSE notes END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (notes, notes, session_id),
        )
        self._commit()
        self.close()

    def reopen_trainer_session(self, session_id):
        """Move a completed Trainer session back to upcoming."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE trainer_workout_sessions
            SET status = 'upcoming', completed_at = '', updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (session_id,),
        )
        self._commit()
        self.close()

    def delete_trainer_session(self, session_id):
        """Delete a Trainer workout session."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM trainer_workout_sessions WHERE id = ?", (session_id,))
        self._commit()
        self.close()

    def create_priority_review(self, summary, model, raw_response, instructions):
        """Persist a priority review and its pending instructions."""
        import json

        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO priority_reviews (summary, model, raw_response) VALUES (?, ?, ?)",
            (summary, model, raw_response),
        )
        review_id = cursor.lastrowid

        for instruction in instructions:
            cursor.execute(
                """
                INSERT INTO priority_review_instructions
                    (review_id, operation, project_name, payload)
                VALUES (?, ?, ?, ?)
                """,
                (
                    review_id,
                    instruction.get("operation", ""),
                    instruction.get("project", ""),
                    json.dumps(instruction),
                ),
            )

        self._commit()
        self.close()
        return review_id

    def get_priority_review(self, review_id):
        """Get a priority review."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM priority_reviews WHERE id = ?", (review_id,))
        review = cursor.fetchone()
        self.close()
        return review

    def get_priority_review_instructions(self, review_id):
        """Get instructions for a priority review."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM priority_review_instructions WHERE review_id = ? ORDER BY id",
            (review_id,),
        )
        rows = cursor.fetchall()
        self.close()
        return rows

    def get_latest_priority_review(self):
        """Get the newest priority review."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM priority_reviews ORDER BY id DESC LIMIT 1")
        review = cursor.fetchone()
        self.close()
        return review

    def list_priority_reviews(self, limit=10):
        """List recent priority reviews."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM priority_reviews ORDER BY id DESC LIMIT ?", (limit,))
        rows = cursor.fetchall()
        self.close()
        return rows

    def update_priority_review_instruction_status(self, instruction_id, status, result):
        """Update instruction status after applying it."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE priority_review_instructions
            SET status = ?, result = ?, applied_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, result, instruction_id),
        )
        self._commit()
        self.close()

    def add_app_feedback_report(
        self,
        title,
        area,
        page_url,
        page_title,
        reporter_name,
        reporter_email,
        raw_feedback,
        destination_project_id=None,
        destination_action_id=None,
    ):
        """Store a developer feedback report for Codex triage."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO app_feedback_reports
                (title, area, page_url, page_title, reporter_name, reporter_email, raw_feedback,
                 destination_project_id, destination_action_id, user_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                title,
                area,
                page_url,
                page_title,
                reporter_name,
                reporter_email,
                raw_feedback,
                destination_project_id,
                destination_action_id,
                self._active_user_id(),
            ),
        )
        report_id = cursor.lastrowid
        self._commit()
        self.close()
        return report_id

    def get_app_feedback_reports(self, status="open", limit=50):
        """List developer feedback reports for Codex."""
        self.connect()
        cursor = self.conn.cursor()
        query = """
            SELECT app_feedback_reports.*, projects.name AS project_name, recommended_actions.action AS action_title
            FROM app_feedback_reports
            LEFT JOIN projects ON projects.id = app_feedback_reports.destination_project_id
            LEFT JOIN recommended_actions ON recommended_actions.id = app_feedback_reports.destination_action_id
        """
        params = []
        if status:
            query += " WHERE app_feedback_reports.status = ?"
            params.append(status)
        query += " ORDER BY app_feedback_reports.created_at DESC, app_feedback_reports.id DESC LIMIT ?"
        params.append(limit)
        cursor.execute(query, params)
        rows = cursor.fetchall()
        self.close()
        return rows

    def update_app_feedback_report_status(self, report_id, status):
        """Update a developer feedback report status."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE app_feedback_reports
            SET status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, report_id),
        )
        self._commit()
        self.close()

    def add_chat_message(self, role, content, model=""):
        """Persist a chat message."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO chat_messages (role, content, model, user_id) VALUES (?, ?, ?, ?)",
            (role, content, model, self._active_user_id()),
        )
        self._commit()
        message_id = cursor.lastrowid
        self.close()
        return message_id

    def get_chat_messages(self, limit=50):
        """Get recent chat messages in chronological order."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT * FROM (
                SELECT * FROM chat_messages
                WHERE (? IS NULL OR user_id = ?)
                ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (self._active_user_id(), self._active_user_id(), limit),
        )
        rows = cursor.fetchall()
        self.close()
        return rows
