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
        self._repair_sample_data_links(cursor)
        self._deprioritize_overlong_actions(cursor)

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
