"""
Menu module for interactive CLI navigation.
"""


class Menu:
    def __init__(self, db, dashboard):
        self.db = db
        self.dashboard = dashboard

    def show_main_menu(self):
        """Show main menu options."""
        print("\n📌 MENU OPTIONS")
        print("-" * 80)
        print("  1. View project details")
        print("  2. Add note to project")
        print("  3. Add blocker to project")
        print("  4. Add recommended action")
        print("  5. Add weekly goal")
        print("  6. Mark goal as complete")
        print("  7. Remove blocker")
        print("  8. Refresh dashboard")
        print("  9. Exit")
        print("-" * 80)

        choice = input("\nEnter choice (1-9): ").strip()

        if choice == "1":
            self.view_project_details()
        elif choice == "2":
            self.add_note()
        elif choice == "3":
            self.add_blocker()
        elif choice == "4":
            self.add_action()
        elif choice == "5":
            self.add_goal()
        elif choice == "6":
            self.mark_goal_complete()
        elif choice == "7":
            self.remove_blocker()
        elif choice == "8":
            pass  # Will refresh on next loop
        elif choice == "9":
            print("\n👋 Goodbye!\n")
            exit(0)
        else:
            print("\n❌ Invalid choice. Please try again.")

    def view_project_details(self):
        """View details of a specific project."""
        projects = self.db.get_all_projects()
        if not projects:
            print("\n❌ No projects available.")
            return

        print("\n📂 SELECT PROJECT")
        for idx, project in enumerate(projects, 1):
            print(f"  {idx}. {project['name']}")

        try:
            choice = int(input("\nEnter project number: ").strip())
            if 1 <= choice <= len(projects):
                self.dashboard.show_project_details(projects[choice - 1]["id"])
                input("\nPress Enter to continue...")
            else:
                print("\n❌ Invalid selection.")
        except ValueError:
            print("\n❌ Please enter a valid number.")

    def add_note(self):
        """Add a note to a project."""
        projects = self.db.get_all_projects()
        if not projects:
            print("\n❌ No projects available.")
            return

        print("\n📂 SELECT PROJECT")
        for idx, project in enumerate(projects, 1):
            print(f"  {idx}. {project['name']}")

        try:
            choice = int(input("\nEnter project number: ").strip())
            if 1 <= choice <= len(projects):
                project_id = projects[choice - 1]["id"]
                note = input("Enter note: ").strip()
                if note:
                    self.db.add_note(project_id, note)
                    print("\n✅ Note added successfully.")
                else:
                    print("\n❌ Note cannot be empty.")
            else:
                print("\n❌ Invalid selection.")
        except ValueError:
            print("\n❌ Please enter a valid number.")

    def add_blocker(self):
        """Add a blocker to a project."""
        projects = self.db.get_all_projects()
        if not projects:
            print("\n❌ No projects available.")
            return

        print("\n📂 SELECT PROJECT")
        for idx, project in enumerate(projects, 1):
            print(f"  {idx}. {project['name']}")

        try:
            choice = int(input("\nEnter project number: ").strip())
            if 1 <= choice <= len(projects):
                project_id = projects[choice - 1]["id"]
                blocker = input("Enter blocker description: ").strip()
                print("\nSeverity: (1) low, (2) medium, (3) high")
                severity_choice = input("Enter severity (1-3, default 2): ").strip() or "2"
                severity_map = {"1": "low", "2": "medium", "3": "high"}
                severity = severity_map.get(severity_choice, "medium")

                if blocker:
                    self.db.add_blocker(project_id, blocker, severity)
                    print("\n✅ Blocker added successfully.")
                else:
                    print("\n❌ Blocker cannot be empty.")
            else:
                print("\n❌ Invalid selection.")
        except ValueError:
            print("\n❌ Please enter a valid input.")

    def add_action(self):
        """Add a recommended action."""
        projects = self.db.get_all_projects()
        if not projects:
            print("\n❌ No projects available.")
            return

        print("\n📂 SELECT PROJECT")
        for idx, project in enumerate(projects, 1):
            print(f"  {idx}. {project['name']}")

        try:
            choice = int(input("\nEnter project number: ").strip())
            if 1 <= choice <= len(projects):
                project_id = projects[choice - 1]["id"]
                action = input("Enter recommended action: ").strip()
                print("\nPriority: (1) low, (2) medium, (3) high")
                priority_choice = input("Enter priority (1-3, default 2): ").strip() or "2"
                priority_map = {"1": "low", "2": "medium", "3": "high"}
                priority = priority_map.get(priority_choice, "medium")

                if action:
                    self.db.add_recommended_action(project_id, action, priority)
                    print("\n✅ Action added successfully.")
                else:
                    print("\n❌ Action cannot be empty.")
            else:
                print("\n❌ Invalid selection.")
        except ValueError:
            print("\n❌ Please enter a valid input.")

    def add_goal(self):
        """Add a weekly goal."""
        projects = self.db.get_all_projects()
        if not projects:
            print("\n❌ No projects available.")
            return

        print("\n📂 SELECT PROJECT")
        for idx, project in enumerate(projects, 1):
            print(f"  {idx}. {project['name']}")

        try:
            choice = int(input("\nEnter project number: ").strip())
            if 1 <= choice <= len(projects):
                project_id = projects[choice - 1]["id"]
                goal = input("Enter weekly goal: ").strip()

                if goal:
                    self.db.add_weekly_goal(project_id, goal)
                    print("\n✅ Goal added successfully.")
                else:
                    print("\n❌ Goal cannot be empty.")
            else:
                print("\n❌ Invalid selection.")
        except ValueError:
            print("\n❌ Please enter a valid number.")

    def mark_goal_complete(self):
        """Mark a goal as complete."""
        projects = self.db.get_all_projects()
        if not projects:
            print("\n❌ No projects available.")
            return

        print("\n📂 SELECT PROJECT")
        for idx, project in enumerate(projects, 1):
            print(f"  {idx}. {project['name']}")

        try:
            choice = int(input("\nEnter project number: ").strip())
            if 1 <= choice <= len(projects):
                project_id = projects[choice - 1]["id"]
                goals = self.db.get_weekly_goals(project_id)

                if not goals:
                    print("\n❌ No goals for this project.")
                    return

                print("\n📋 GOALS")
                for idx, goal in enumerate(goals, 1):
                    status = "✓" if goal["completed"] else "○"
                    print(f"  {idx}. [{status}] {goal['goal']}")

                goal_choice = int(input("\nEnter goal number to mark complete: ").strip())
                if 1 <= goal_choice <= len(goals):
                    self.db.mark_goal_complete(goals[goal_choice - 1]["id"])
                    print("\n✅ Goal marked as complete.")
                else:
                    print("\n❌ Invalid selection.")
            else:
                print("\n❌ Invalid selection.")
        except ValueError:
            print("\n❌ Please enter a valid number.")

    def remove_blocker(self):
        """Remove a blocker."""
        projects = self.db.get_all_projects()
        if not projects:
            print("\n❌ No projects available.")
            return

        print("\n📂 SELECT PROJECT")
        for idx, project in enumerate(projects, 1):
            print(f"  {idx}. {project['name']}")

        try:
            choice = int(input("\nEnter project number: ").strip())
            if 1 <= choice <= len(projects):
                project_id = projects[choice - 1]["id"]
                blockers = self.db.get_blockers(project_id)

                if not blockers:
                    print("\n❌ No blockers for this project.")
                    return

                print("\n🚫 BLOCKERS")
                for idx, blocker in enumerate(blockers, 1):
                    print(f"  {idx}. {blocker['description']}")

                blocker_choice = int(input("\nEnter blocker number to remove: ").strip())
                if 1 <= blocker_choice <= len(blockers):
                    self.db.delete_blocker(blockers[blocker_choice - 1]["id"])
                    print("\n✅ Blocker removed.")
                else:
                    print("\n❌ Invalid selection.")
            else:
                print("\n❌ Invalid selection.")
        except ValueError:
            print("\n❌ Please enter a valid number.")
