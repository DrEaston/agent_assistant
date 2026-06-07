"""
Dashboard module for displaying project information.
"""


class Dashboard:
    def __init__(self, db):
        self.db = db

    def show_main_dashboard(self):
        """Display main dashboard with all key information."""
        self.clear_screen()
        print("\n" + "=" * 80)
        print("📊 PERSONAL PROJECT AGENT - DASHBOARD")
        print("=" * 80 + "\n")

        projects = self.db.get_all_projects()

        if not projects:
            print("No active projects.")
            return

        # 1. Active Projects
        print("🎯 ACTIVE PROJECTS")
        print("-" * 80)
        for idx, project in enumerate(projects, 1):
            print(f"  {idx}. {project['name']}")
        print()

        # 2. Recommended Next Actions (across all projects)
        print("⚡ RECOMMENDED NEXT ACTIONS")
        print("-" * 80)
        all_actions = []
        for project in projects:
            actions = self.db.get_recommended_actions(project["id"])
            for action in actions:
                all_actions.append((project["name"], action))

        if all_actions:
            for project_name, action in all_actions[:5]:  # Show top 5
                priority = action["priority"]
                emoji = "🔴" if priority == "high" else "🟡"
                print(f"  {emoji} [{project_name}] {action['action']}")
        else:
            print("  No recommended actions.")
        print()

        # 3. Project Notes
        print("📝 PROJECT NOTES (Latest)")
        print("-" * 80)
        for project in projects:
            notes = self.db.get_notes(project["id"])
            if notes:
                print(f"  {project['name']}:")
                for note in notes[:2]:  # Show latest 2
                    print(f"    • {note['content']}")
        print()

        # 4. Blockers
        print("🚫 BLOCKERS")
        print("-" * 80)
        all_blockers = []
        for project in projects:
            blockers = self.db.get_blockers(project["id"])
            for blocker in blockers:
                all_blockers.append((project["name"], blocker))

        if all_blockers:
            for project_name, blocker in all_blockers:
                severity = blocker["severity"]
                emoji = "🔴" if severity == "high" else "🟡"
                print(f"  {emoji} [{project_name}] {blocker['description']}")
        else:
            print("  No blockers.")
        print()

        # 5. Weekly Goals
        print("📋 WEEKLY GOALS")
        print("-" * 80)
        for project in projects:
            goals = self.db.get_weekly_goals(project["id"])
            if goals:
                print(f"  {project['name']}:")
                for goal in goals[:2]:  # Show latest 2
                    status = "✓" if goal["completed"] else "○"
                    print(f"    [{status}] {goal['goal']}")
        print()
        print("=" * 80)

    def show_project_details(self, project_id):
        """Display detailed view of a specific project."""
        self.clear_screen()
        project = self.db.get_project_by_id(project_id)

        if not project:
            print("Project not found.")
            return

        print("\n" + "=" * 80)
        print(f"📋 {project['name'].upper()}")
        print("=" * 80 + "\n")

        # Notes
        print("📝 NOTES")
        print("-" * 80)
        notes = self.db.get_notes(project_id)
        if notes:
            for idx, note in enumerate(notes, 1):
                print(f"  {idx}. {note['content']}")
                print(f"     Created: {note['created_at']}")
        else:
            print("  No notes.")
        print()

        # Recommended Actions
        print("⚡ RECOMMENDED ACTIONS")
        print("-" * 80)
        actions = self.db.get_recommended_actions(project_id)
        if actions:
            for idx, action in enumerate(actions, 1):
                priority = action["priority"]
                emoji = "🔴" if priority == "high" else "🟡"
                print(f"  {idx}. {emoji} [{priority.upper()}] {action['action']}")
        else:
            print("  No recommended actions.")
        print()

        # Blockers
        print("🚫 BLOCKERS")
        print("-" * 80)
        blockers = self.db.get_blockers(project_id)
        if blockers:
            for idx, blocker in enumerate(blockers, 1):
                severity = blocker["severity"]
                emoji = "🔴" if severity == "high" else "🟡"
                print(f"  {idx}. {emoji} [{severity.upper()}] {blocker['description']}")
        else:
            print("  No blockers.")
        print()

        # Weekly Goals
        print("📋 WEEKLY GOALS")
        print("-" * 80)
        goals = self.db.get_weekly_goals(project_id)
        if goals:
            for idx, goal in enumerate(goals, 1):
                status = "✓" if goal["completed"] else "○"
                print(f"  {idx}. [{status}] {goal['goal']} (Week: {goal['week_start']})")
        else:
            print("  No weekly goals.")
        print()
        print("=" * 80)

    @staticmethod
    def clear_screen():
        """Clear terminal screen."""
        import os
        os.system("cls" if os.name == "nt" else "clear")
