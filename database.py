"""
Database module for project management.
Handles SQLite operations for projects, notes, blockers, and goals.
"""

import sqlite3
from datetime import datetime


class Database:
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = None

    def connect(self):
        """Establish database connection."""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        return self.conn

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()

    def init(self):
        """Initialize database schema."""
        self.connect()
        cursor = self.conn.cursor()

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
        self._ensure_column(cursor, "recommended_actions", "sort_order", "INTEGER DEFAULT 100")
        self._ensure_column(cursor, "recommended_actions", "status", "TEXT DEFAULT 'open'")
        self._ensure_column(cursor, "recommended_actions", "completed_at", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "recipe_images", "group_id", "INTEGER")
        self._ensure_column(cursor, "recipe_images", "side", "TEXT DEFAULT ''")
        self._ensure_column(cursor, "recipe_image_groups", "layout", "TEXT DEFAULT 'front_back'")
        self._repair_sample_data_links(cursor)
        self._deprioritize_overlong_actions(cursor)
        self._ensure_recipe_import_steps(cursor)
        self._ensure_recipe_image_groups(cursor)

        self.conn.commit()
        self.close()

    def _ensure_column(self, cursor, table, column, definition):
        """Add a column when opening an older database file."""
        cursor.execute(f"PRAGMA table_info({table})")
        columns = [row["name"] for row in cursor.fetchall()]
        if column not in columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

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

        self.conn.commit()

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

        self.conn.commit()
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
            INSERT INTO projects (name, description, priority_score, status, updated_at)
            VALUES (?, ?, ?, 'active', CURRENT_TIMESTAMP)
            """,
            (name, description, priority_score),
        )
        self.conn.commit()
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
        self.conn.commit()
        self.close()

    def update_project_status(self, project_id, status):
        """Update project status."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE projects SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, project_id),
        )
        self.conn.commit()
        self.close()

    def get_project_by_id(self, project_id):
        """Get a specific project."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
        project = cursor.fetchone()
        self.close()
        return project

    def get_project_by_name(self, name):
        """Get project by name."""
        self.connect()
        cursor = self.conn.cursor()
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
        self.conn.commit()
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
        self.conn.commit()
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
        self.conn.commit()
        self.close()

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
        self.conn.commit()
        self.close()

    def update_recommended_action_order(self, action_id, sort_order):
        """Update a recommended action's ordering within its priority bucket."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE recommended_actions SET sort_order = ? WHERE id = ?",
            (sort_order, action_id),
        )
        self.conn.commit()
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
        self.conn.commit()
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
        self.conn.commit()
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
        cursor.execute(
            """
            UPDATE recommended_actions
            SET status = 'open', completed_at = ''
            WHERE id = ?
            """,
            (action_id,),
        )
        self.conn.commit()
        self.close()

    def update_task_step_text(self, step_id, step):
        """Update the wording for a checklist step."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE task_steps SET step = ? WHERE id = ?",
            (step, step_id),
        )
        self.conn.commit()
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
        self.conn.commit()
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
        self.conn.commit()
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
        self.conn.commit()
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
        self.conn.commit()
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
            ORDER BY recipe_image_groups.id DESC
            """,
            (action_id,),
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
            INSERT INTO recipe_image_groups (project_id, action_id, layout, label)
            VALUES (?, ?, ?, ?)
            """,
            (project_id, action_id, layout, label),
        )
        self.conn.commit()
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
                (project_id, action_id, group_id, side, filename, original_filename, content_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (project_id, action_id, group_id, side, filename, original_filename, content_type),
        )
        self.conn.commit()
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
        self.conn.commit()
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
                (group_id, status, ingredients_text, instructions_text, sections_json, raw_response, error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(group_id) DO UPDATE SET
                status = excluded.status,
                ingredients_text = excluded.ingredients_text,
                instructions_text = excluded.instructions_text,
                sections_json = excluded.sections_json,
                raw_response = excluded.raw_response,
                error = excluded.error,
                updated_at = CURRENT_TIMESTAMP
            """,
            (group_id, status, ingredients_text, instructions_text, sections_json, raw_response, error),
        )
        self.conn.commit()
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
        self.conn.commit()
        self.close()

    def mark_goal_complete(self, goal_id):
        """Mark a goal as complete."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("UPDATE weekly_goals SET completed = 1 WHERE id = ?", (goal_id,))
        self.conn.commit()
        self.close()

    def delete_blocker(self, blocker_id):
        """Delete a blocker."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM blockers WHERE id = ?", (blocker_id,))
        self.conn.commit()
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

        self.conn.commit()
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
        self.conn.commit()
        self.close()

    def add_chat_message(self, role, content, model=""):
        """Persist a chat message."""
        self.connect()
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO chat_messages (role, content, model) VALUES (?, ?, ?)",
            (role, content, model),
        )
        self.conn.commit()
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
                SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (limit,),
        )
        rows = cursor.fetchall()
        self.close()
        return rows
