"""
LLM service for conversational project planning.
"""

from typing import List, Optional

from llm_provider import get_llm_provider


SYSTEM_PROMPT = """You are a personal project-priority assistant.

The user already has a coding agent in VS Code. Your job is to maintain project
memory, help choose what deserves attention, and produce crisp work packets that
a coding agent can act on.

Be concise, specific, and grounded in the provided project context. When project
state was updated, acknowledge it briefly. End with the recommended next action.
"""


class LLMService:
    """High-level interface for LLM interactions with project context."""

    def __init__(self):
        self.provider = get_llm_provider()

    def chat(
        self,
        user_message: str,
        project_context: Optional[dict] = None,
        conversation_history: Optional[List[dict]] = None,
    ) -> str:
        messages = list(conversation_history or [])

        if project_context:
            context = self._build_context_string(project_context)
            user_message = f"[Project Context]\n{context}\n\n{user_message}"

        messages.append({"role": "user", "content": user_message})
        return self.provider.chat(messages, SYSTEM_PROMPT)

    @staticmethod
    def _build_context_string(project_context: dict) -> str:
        lines = []
        stats = project_context.get("stats", {})
        lines.append(
            "Status: "
            f"{stats.get('total_projects', 0)} projects, "
            f"{stats.get('total_blockers', 0)} blockers, "
            f"{stats.get('total_actions', 0)} actions"
        )

        focus = project_context.get("recommended_project")
        if focus:
            lines.append(
                "\nRecommended focus: "
                f"{focus.get('name')} "
                f"(priority {focus.get('priority_score', 3)}/5)"
            )
            if focus.get("description"):
                lines.append(f"Description: {focus['description']}")

        next_action = project_context.get("next_action")
        if next_action:
            lines.append(
                "Next action: "
                f"[{next_action.get('project_name', 'Unknown')}] "
                f"{next_action.get('action', '')}"
            )

        projects = project_context.get("projects", [])
        if projects:
            lines.append(f"\nProjects ({len(projects)}):")
            for project in projects:
                lines.append(
                    f"- {project.get('name')}: priority {project.get('priority_score', 3)}/5; "
                    f"{project.get('description') or 'No description'}"
                )

        blockers = project_context.get("blockers", [])
        if blockers:
            lines.append(f"\nBlockers ({len(blockers)}):")
            for blocker in blockers[:5]:
                severity = blocker.get("severity", "medium").upper()
                lines.append(f"- [{severity}] {blocker.get('project_name')}: {blocker.get('description')}")

        actions = project_context.get("actions", [])
        if actions:
            lines.append(f"\nActions ({len(actions)} shown):")
            for action in actions[:5]:
                priority = action.get("priority", "medium").upper()
                lines.append(f"- [{priority}] {action.get('project_name')}: {action.get('action')}")

        goals = project_context.get("goals", [])
        if goals:
            completed = sum(1 for goal in goals if goal.get("completed"))
            lines.append(f"\nWeekly goals: {completed}/{len(goals)} complete")
            for goal in goals:
                status = "done" if goal.get("completed") else "open"
                lines.append(f"- {status}: [{goal.get('project_name')}] {goal.get('goal')}")

        return "\n".join(lines)
