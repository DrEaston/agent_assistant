"""
Strategic priority review.

The review model produces a structured plan. The local app stores that plan and
applies only known operations, which keeps database mutation predictable.
"""

import json
import os
from pathlib import Path

from llm_provider import get_llm_provider
from llm_service import LLMService


REVIEW_SYSTEM_PROMPT = """You are a senior project strategist.

Review the user's project state and produce a priority refactor plan. Return
only valid JSON with this shape:

{
  "summary": "short human-readable assessment",
  "instructions_for_mini_model": [
    {
      "operation": "update_project_priority",
      "project": "Project name",
      "priority_score": 1,
      "reason": "why"
    }
  ],
  "questions_for_user": ["question"]
}

Allowed operations:
- update_project_priority: project, priority_score 1-5, reason
- add_action: project, action, priority low|medium|high
- update_action_priority: project, action, priority low|medium|high
- add_goal: project, goal
- add_note: project, note
- add_blocker: project, description, severity low|medium|high

Do not invent projects. Prefer fewer, higher-confidence changes. The mini model
will apply these instructions exactly, so keep them concrete.
"""


class PriorityReviewService:
    def __init__(self, db, agent_service, review_model=None):
        self.db = db
        self.agent_service = agent_service
        self.review_model = review_model or os.getenv("OPENAI_REVIEW_MODEL", "gpt-5.4")
        self.provider = None
        try:
            self.provider = get_llm_provider(model=self.review_model)
        except ValueError:
            self.provider = None

    def create_review(self):
        context = self.agent_service.build_dashboard_context()
        prompt = self._build_prompt(context)
        raw_response = ""

        if self.provider:
            try:
                raw_response = self.provider.chat(
                    [{"role": "user", "content": prompt}],
                    REVIEW_SYSTEM_PROMPT,
                    max_completion_tokens=1800,
                )
                plan = self._parse_plan(raw_response)
            except Exception as exc:
                plan = self._fallback_plan(context, f"Review model failed: {exc}")
                raw_response = str(exc)
        else:
            plan = self._fallback_plan(context, "Review model is not configured.")

        instructions = self._clean_instructions(plan.get("instructions_for_mini_model", []), context)
        review_id = self.db.create_priority_review(
            summary=plan.get("summary", "Priority review complete."),
            model=self.review_model if self.provider else "local-review",
            raw_response=raw_response,
            instructions=instructions,
        )

        return self.get_review(review_id)

    def get_review(self, review_id):
        review = self.db.get_priority_review(review_id)
        if not review:
            return None

        instructions = []
        for row in self.db.get_priority_review_instructions(review_id):
            instruction = dict(row)
            instruction["payload"] = json.loads(instruction["payload"])
            instructions.append(instruction)

        data = dict(review)
        data["instructions"] = instructions
        return data

    def get_latest_review(self):
        review = self.db.get_latest_priority_review()
        return self.get_review(review["id"]) if review else None

    def apply_review(self, review_id):
        review = self.get_review(review_id)
        if not review:
            return None

        results = []
        for instruction in review["instructions"]:
            if instruction["status"] != "pending":
                results.append(
                    {
                        "instruction_id": instruction["id"],
                        "status": instruction["status"],
                        "result": instruction["result"],
                    }
                )
                continue

            try:
                result = self._apply_instruction(instruction["payload"])
                status = "applied"
            except Exception as exc:
                result = str(exc)
                status = "failed"

            self.db.update_priority_review_instruction_status(instruction["id"], status, result)
            results.append(
                {
                    "instruction_id": instruction["id"],
                    "status": status,
                    "result": result,
                }
            )

        return {
            "review_id": review_id,
            "results": results,
            "dashboard": self.agent_service.build_dashboard_context(),
        }

    def build_codex_work_packet(self, review_id=None):
        context = self.agent_service.build_dashboard_context()
        work_packet = self.agent_service.build_work_packet(context)
        review = self.get_review(review_id) if review_id else self.get_latest_review()

        markdown = self._format_codex_work_packet(work_packet, review)
        return {
            "review_id": review["id"] if review else None,
            "work_packet": work_packet,
            "markdown": markdown,
        }

    def save_codex_work_packet(self, review_id=None, path="codex_work_packet.md"):
        packet = self.build_codex_work_packet(review_id)
        output_path = Path(path).resolve()
        output_path.write_text(packet["markdown"] + "\n", encoding="utf-8")
        packet["path"] = str(output_path)
        return packet

    def _apply_instruction(self, instruction):
        operation = instruction.get("operation")
        project_name = instruction.get("project")
        project = self.db.get_project_by_name(project_name) if project_name else None
        if not project:
            raise ValueError(f"Unknown project: {project_name}")

        project = dict(project)
        project_id = project["id"]

        if operation == "update_project_priority":
            priority_score = self._clamp_priority_score(instruction.get("priority_score", 3))
            reason = instruction.get("reason", "Priority review recommendation.")
            self.db.update_project_priority(project_id, priority_score, reason)
            return f"Set {project_name} priority to {priority_score}/5."

        if operation == "add_action":
            action = instruction.get("action", "").strip()
            if not action:
                raise ValueError("Missing action")
            priority = self._clean_priority(instruction.get("priority", "medium"))
            existing = self.db.find_recommended_action(project_id, action)
            if existing:
                self.db.update_recommended_action_priority(existing["id"], priority)
                return f"Updated existing action priority: {action}"
            self.db.add_recommended_action(project_id, action, priority)
            return f"Added action: {action}"

        if operation == "update_action_priority":
            action = instruction.get("action", "").strip()
            priority = self._clean_priority(instruction.get("priority", "medium"))
            existing = self.db.find_recommended_action(project_id, action)
            if not existing:
                self.db.add_recommended_action(project_id, action, priority)
                return f"Added missing action at {priority}: {action}"
            self.db.update_recommended_action_priority(existing["id"], priority)
            return f"Set action to {priority}: {action}"

        if operation == "add_goal":
            goal = instruction.get("goal", "").strip()
            if not goal:
                raise ValueError("Missing goal")
            self.db.add_weekly_goal(project_id, goal)
            return f"Added goal: {goal}"

        if operation == "add_note":
            note = instruction.get("note", "").strip()
            if not note:
                raise ValueError("Missing note")
            self.db.add_note(project_id, note)
            return "Added note."

        if operation == "add_blocker":
            description = instruction.get("description", "").strip()
            if not description:
                raise ValueError("Missing blocker description")
            severity = self._clean_priority(instruction.get("severity", "medium"))
            self.db.add_blocker(project_id, description, severity)
            return f"Added blocker: {description}"

        raise ValueError(f"Unsupported operation: {operation}")

    @staticmethod
    def _format_codex_work_packet(work_packet, review):
        lines = [
            "# Codex Work Packet",
            "",
            "## Current Priority",
            f"Project: {work_packet.get('project') or 'None selected'}",
            f"Goal: {work_packet.get('goal')}",
            f"Next action: {work_packet.get('next_action')}",
            f"Blockers: {', '.join(work_packet.get('blockers') or []) or 'None recorded'}",
            "",
            "## Task",
            "Use the project database as the source of truth. Help implement the current next action, or inspect the relevant codebase/data if the next action is unclear.",
        ]

        if review:
            lines.extend(
                [
                    "",
                    "## Latest Priority Review",
                    f"Review id: {review['id']}",
                    f"Model: {review['model']}",
                    f"Summary: {review['summary']}",
                    "",
                    "## Review Instructions",
                ]
            )

            for instruction in review["instructions"]:
                payload = instruction["payload"]
                target = (
                    payload.get("action")
                    or payload.get("goal")
                    or payload.get("note")
                    or payload.get("description")
                    or payload.get("reason")
                    or ""
                )
                lines.append(
                    f"- [{instruction['status']}] {payload.get('operation')} "
                    f"for {payload.get('project')}: {target}"
                )

        lines.extend(
            [
                "",
                "## Expected Codex Behavior",
                "- Read the relevant files/database rows before changing code.",
                "- Keep changes scoped to the current priority.",
                "- If the work packet points to data/list changes, update the app/database logic rather than hard-coding one-off data.",
                "- Verify with the smallest useful test or local API check.",
            ]
        )

        return "\n".join(lines)

    @staticmethod
    def _build_prompt(context):
        context_text = LLMService._build_context_string(context)
        return (
            "Review this project state and propose a small set of priority-list changes.\n\n"
            f"{context_text}\n\n"
            "Return JSON only."
        )

    @staticmethod
    def _parse_plan(raw_response):
        text = raw_response.strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.startswith("json"):
                text = text[4:].strip()
        if not text.startswith("{"):
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                text = text[start:end + 1]
        return json.loads(text)

    @staticmethod
    def _clean_instructions(instructions, context):
        project_names = {project["name"] for project in context.get("projects", [])}
        allowed = {
            "update_project_priority",
            "add_action",
            "update_action_priority",
            "add_goal",
            "add_note",
            "add_blocker",
        }

        cleaned = []
        for instruction in instructions:
            if instruction.get("operation") not in allowed:
                continue
            if instruction.get("project") not in project_names:
                continue
            cleaned.append(instruction)
        return cleaned[:12]

    @staticmethod
    def _fallback_plan(context, reason):
        focus = context.get("recommended_project")
        next_action = context.get("next_action")
        instructions = []
        summary = reason

        if focus:
            summary += f" Current best focus is {focus['name']}."
            instructions.append(
                {
                    "operation": "update_project_priority",
                    "project": focus["name"],
                    "priority_score": max(int(focus.get("priority_score") or 3), 4),
                    "reason": "Local review kept the current focus near the top.",
                }
            )

        if next_action:
            instructions.append(
                {
                    "operation": "update_action_priority",
                    "project": next_action["project_name"],
                    "action": next_action["action"],
                    "priority": "high",
                }
            )

        return {
            "summary": summary,
            "instructions_for_mini_model": instructions,
            "questions_for_user": [],
        }

    @staticmethod
    def _clean_priority(value):
        value = str(value or "medium").lower()
        return value if value in {"low", "medium", "high"} else "medium"

    @staticmethod
    def _clamp_priority_score(value):
        try:
            score = int(value)
        except (TypeError, ValueError):
            score = 3
        return min(5, max(1, score))
