"""Assistant pages Dieter can open (core/connector/pages.py)."""

from core.connector.pages import Page

PAGES = [
    Page("assistant.home", "Assistant", "To-dos (with what's due), projects and reading at a glance.", "/apps/assistant"),
    Page("assistant.projects", "Projects", "Every active project.", "/apps/assistant/projects"),
    Page("assistant.project", "Project", "One project: actions, goals, blockers, notes (ids from assistant_list_projects).",
         "/projects/{project_id}", {"project_id": "integer"}),
    Page("assistant.reading", "Reading list", "Books he wants, by author, with statuses.", "/apps/assistant/reading"),
    Page("assistant.bookstore", "Bookstore checklist", "At the bookstore: authors A-Z with the books he's looking for.",
         "/apps/assistant/reading/bookstore"),
]
