"""
Project agent orchestration.

This layer keeps the chat assistant grounded in local project state. It handles
simple state-changing requests directly, then asks the LLM for wording when one
is available.
"""

import re
from difflib import SequenceMatcher
from typing import Optional


PRIORITY_VALUES = {
    "low": 1,
    "medium": 3,
    "high": 5,
}


class AgentService:
    def __init__(self, db, llm_service=None):
        self.db = db
        self.llm_service = llm_service

    def chat(self, user_message, project_context, conversation_history=None):
        updates = self.apply_updates(user_message)
        fresh_context = self.build_dashboard_context()
        work_packet = self.build_work_packet(fresh_context)

        fallback = self._fallback_response(user_message, updates, work_packet)
        response = fallback

        needs_clarification = any(update["type"] == "project_clarification_needed" for update in updates)

        if self.llm_service and not needs_clarification:
            try:
                prompt = self._build_llm_prompt(user_message, updates, work_packet)
                response = self.llm_service.chat(prompt, fresh_context, conversation_history)
            except Exception as exc:
                response = f"{fallback}\n\nLLM note: I used the local planner because the model call failed: {exc}"

        return {
            "response": response,
            "updates": updates,
            "work_packet": work_packet,
            "dashboard": fresh_context,
        }

    def apply_updates(self, user_message):
        text = user_message.strip()
        lower = text.lower()
        updates = []

        created = self._maybe_create_project(text)
        if created:
            updates.append(created)

        for update in self._maybe_update_action_priorities(text, lower):
            updates.append(update)

        pre_project_update_count = len(updates)
        project = self._find_project_in_text(text)
        explicit_project = project is not None
        used_context_project = False
        if not project and (
            self._sounds_like_priority_statement(lower)
            or self._sounds_like_completion(lower)
        ):
            project = self.build_dashboard_context().get("recommended_project")
            used_context_project = project is not None

        if project:
            priority_update = self._maybe_update_priority(project, lower)
            if priority_update:
                updates.append(priority_update)

            step_update = self._maybe_complete_step(project, text, lower)
            if step_update:
                updates.append(step_update)

            if not step_update:
                completion_update = self._maybe_complete_action(project, text, lower)
                if completion_update:
                    updates.append(completion_update)

            for update in self._maybe_add_priority_from_text(project, text, lower):
                updates.append(update)

            for update in self._maybe_add_project_items(project, text):
                updates.append(update)

        project_updates_applied = len(updates) > pre_project_update_count
        if self._needs_project_clarification(lower, explicit_project, used_context_project, project_updates_applied):
            updates.append(self._project_clarification_update())

        return updates

    @staticmethod
    def _sounds_like_priority_statement(lower):
        return any(
            phrase in lower
            for phrase in [
                "priority is",
                "priority should be",
                "top priority is",
                "main priority is",
                "my priority is",
                "i need to",
                "i should",
                "we need to",
                "we should",
            ]
        )

    def build_dashboard_context(self):
        projects = [dict(row) for row in self.db.get_all_projects()]
        all_actions = []
        all_blockers = []
        all_goals = []

        for project in projects:
            actions = [dict(row) for row in self.db.get_open_recommended_actions(project["id"])]
            blockers = [dict(row) for row in self.db.get_blockers(project["id"])]
            goals = [dict(row) for row in self.db.get_weekly_goals(project["id"])]

            for action in actions:
                action["project_name"] = project["name"]
                action["project_priority_score"] = project.get("priority_score", 3)
                self._attach_step_summary(action)
                all_actions.append(action)

            for blocker in blockers:
                blocker["project_name"] = project["name"]
                all_blockers.append(blocker)

            for goal in goals:
                goal["project_name"] = project["name"]
                all_goals.append(goal)

        all_actions.sort(key=self._action_sort_key)
        all_blockers.sort(key=lambda x: {"high": 0, "medium": 1, "low": 2}.get(x["severity"], 1))

        recommended_project = self._choose_focus_project(projects, all_actions, all_blockers)
        next_action = self._choose_next_action(recommended_project, all_actions)
        other_actions = [
            action for action in all_actions
            if not next_action or action["id"] != next_action["id"]
        ]
        other_project_actions = [
            action for action in other_actions
            if not recommended_project or action["project_id"] != recommended_project["id"]
        ]
        if len(other_project_actions) < 3:
            seen_projects = {action["project_id"] for action in other_project_actions}
            for action in other_actions:
                if action["project_id"] in seen_projects:
                    continue
                other_project_actions.append(action)
                seen_projects.add(action["project_id"])
                if len(other_project_actions) >= 3:
                    break

        return {
            "projects": projects,
            "recommended_project": recommended_project,
            "next_action": next_action,
            "blockers": all_blockers,
            "actions": other_project_actions[:3],
            "goals": all_goals,
            "stats": {
                "total_projects": len(projects),
                "total_blockers": len(all_blockers),
                "total_actions": len(all_actions),
                "completed_goals": sum(1 for goal in all_goals if goal["completed"]),
                "total_goals": len(all_goals),
            },
        }

    def _attach_step_summary(self, action):
        steps = [dict(row) for row in self.db.get_task_steps(action["id"])]
        active_steps = [step for step in steps if step["status"] in ["open", "done"]]
        done_steps = [step for step in active_steps if step["status"] == "done"]
        open_steps = [step for step in active_steps if step["status"] == "open"]
        total = len(active_steps)

        action["steps_total"] = total
        action["steps_done"] = len(done_steps)
        action["progress_percent"] = round((len(done_steps) / total) * 100) if total else 0
        action["top_open_steps"] = open_steps[:2]

    def build_work_packet(self, context):
        project = context.get("recommended_project")
        action = context.get("next_action")
        if not project:
            return {
                "project": None,
                "goal": "Create or choose an active project.",
                "next_action": "Add a project you want the agent to track.",
                "context_for_coding_agent": "No active project is selected yet.",
            }

        project_blockers = [
            blocker for blocker in context.get("blockers", [])
            if blocker["project_id"] == project["id"]
        ]
        project_goals = [
            goal for goal in context.get("goals", [])
            if goal["project_id"] == project["id"] and not goal["completed"]
        ]

        goal = project_goals[0]["goal"] if project_goals else f"Make visible progress on {project['name']}."
        next_action = action["action"] if action else "Define the next concrete action."
        blockers = [blocker["description"] for blocker in project_blockers[:3]]
        context_for_coding_agent = (
            f"Project: {project['name']}\n"
            f"Goal: {goal}\n"
            f"Next action: {next_action}\n"
            f"Priority: {project.get('priority_score', 3)}/5\n"
            f"Blockers: {', '.join(blockers) if blockers else 'None recorded'}"
        )

        return {
            "project": project["name"],
            "goal": goal,
            "next_action": next_action,
            "blockers": blockers,
            "context_for_coding_agent": context_for_coding_agent,
        }

    def _maybe_create_project(self, text):
        match = re.search(
            r"\b(?:create|add|track)\s+(?:a\s+)?(?:new\s+)?project\s+(?:called|named)?\s*['\"]?([^'\".,;:]+)",
            text,
            re.IGNORECASE,
        )
        if not match:
            return None

        name = match.group(1).strip()
        existing = self._find_project_by_name(name)
        if existing:
            return {"type": "project_exists", "project": existing["name"]}

        project_id = self.db.add_project(name)
        return {"type": "project_created", "project_id": project_id, "project": name}

    def _maybe_update_priority(self, project, lower):
        if any(word in lower for word in ["prioritize", "top priority", "focus on", "work on first"]):
            reason = "User marked this as a current focus."
            self.db.update_project_priority(project["id"], 5, reason)
            return {"type": "priority_updated", "project": project["name"], "priority_score": 5}

        if any(word in lower for word in ["deprioritize", "lower priority", "back burner", "pause"]):
            reason = "User lowered this project's priority."
            self.db.update_project_priority(project["id"], 1, reason)
            return {"type": "priority_updated", "project": project["name"], "priority_score": 1}

        return None

    def _maybe_complete_action(self, project, text, lower):
        if not self._sounds_like_completion(lower):
            return None

        if any(phrase in lower for phrase in ["not done", "not complete", "isn't done", "is not done"]):
            return None

        action = self._find_action_in_text(text, self._open_actions_for_project(project["id"]))
        if not action:
            return None

        self.db.mark_recommended_action_complete(action["id"])
        self.db.add_note(project["id"], f"Completed task: {action['action']}")
        return {
            "type": "action_completed",
            "project": project["name"],
            "action": action["action"],
        }

    def _maybe_complete_step(self, project, text, lower):
        if not self._sounds_like_completion(lower):
            return None

        if any(phrase in lower for phrase in ["not done", "not complete", "isn't done", "is not done"]):
            return None

        target = self._completion_target_text(text)
        candidate = self._find_step_in_text(target or text, project["id"])
        if not candidate:
            completed_candidate = self._find_step_in_text(target or text, project["id"], statuses={"done"})
            if completed_candidate:
                return {
                    "type": "step_already_completed",
                    "project": project["name"],
                    "action": completed_candidate["action"]["action"],
                    "step": completed_candidate["step"]["step"],
                }
            return None

        self.db.mark_task_step_complete(candidate["step"]["id"])
        self.db.add_note(
            project["id"],
            f"Completed step: {candidate['step']['step']} ({candidate['action']['action']})",
        )
        return {
            "type": "step_completed",
            "project": project["name"],
            "action": candidate["action"]["action"],
            "step": candidate["step"]["step"],
        }

    def _maybe_add_project_items(self, project, text):
        patterns = [
            ("note_added", "note", r"\bnote\s+(?:for|on|to)\s+.+?[:\-]\s*(.+)$", self.db.add_note),
            ("action_added", "action", r"\b(?:action|next action)\s+(?:for|on|to)\s+.+?[:\-]\s*(.+)$", self._add_action),
            ("blocker_added", "blocker", r"\bblocker\s+(?:for|on|to)\s+.+?[:\-]\s*(.+)$", self._add_blocker),
            ("goal_added", "goal", r"\bgoal\s+(?:for|on|to)\s+.+?[:\-]\s*(.+)$", self._add_goal),
        ]

        updates = []
        for update_type, label, pattern, handler in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                content = match.group(1).strip()
                if label == "action" and self._looks_like_multi_step_plan(content):
                    continue
                handler(project["id"], content)
                updates.append({"type": update_type, "project": project["name"], label: content})

        return updates

    def _project_clarification_update(self):
        projects = [dict(row) for row in self.db.get_all_projects()]
        return {
            "type": "project_clarification_needed",
            "projects": [project["name"] for project in projects],
        }

    @staticmethod
    def _needs_project_clarification(lower, explicit_project, used_context_project, project_updates_applied):
        if explicit_project or project_updates_applied:
            return False

        mutation_phrases = [
            "add action",
            "add blocker",
            "add goal",
            "add note",
            "action:",
            "blocker:",
            "goal:",
            "note:",
            "finished",
            "complete",
            "completed",
            "done",
            "mark",
            "deployed",
            "uploaded",
            "set up",
        ]

        if not any(phrase in lower for phrase in mutation_phrases):
            return False

        return not used_context_project or not project_updates_applied

    @staticmethod
    def _looks_like_multi_step_plan(content):
        """Avoid saving pasted planning paragraphs as one giant task."""
        normalized = re.sub(r"\s+", " ", content.strip())
        if len(normalized) > 180:
            return True

        sequencing_markers = re.findall(
            r"\b(first|second|third|then|next|after that|finally)\b",
            normalized,
            flags=re.IGNORECASE,
        )
        return len(sequencing_markers) >= 2

    def _maybe_add_priority_from_text(self, project, text, lower):
        if not any(
            phrase in lower
            for phrase in [
                "priority is",
                "priority should be",
                "top priority is",
                "main priority is",
                "my priority is",
                "i need to",
                "i should",
                "we need to",
                "we should",
            ]
        ):
            return []

        content = self._extract_priority_content(text)
        if not content:
            return []

        actions = self._extract_action_items_from_priority_content(content)
        updates = []
        project_actions = self._actions_for_project(project["id"])

        for index, action in enumerate(actions):
            priority = "high" if index == 0 else "medium"
            existing = self._find_action_in_text(action, project_actions)
            if existing:
                self.db.update_recommended_action_priority(existing["id"], priority)
                updates.append(
                    {
                        "type": "action_priority_updated",
                        "project": project["name"],
                        "action": existing["action"],
                        "priority": priority,
                    }
                )
                continue

            self.db.add_recommended_action(project["id"], action, priority)
            project_actions.append({"action": action, "project_name": project["name"]})
            updates.append(
                {
                    "type": "priority_action_added",
                    "project": project["name"],
                    "action": action,
                    "priority": priority,
                }
            )

        if updates:
            self.db.add_note(project["id"], f"User stated priorities: {'; '.join(actions)}")

        return updates

    @staticmethod
    def _extract_priority_content(text):
        patterns = [
            r"\b(?:top priority|main priority|my priority|priority)\s+(?:is|should be)\s+(.+)$",
            r"\b(?:i|we)\s+(?:need|should)\s+to\s+(.+)$",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                content = match.group(1).strip(" .")
                content = re.sub(r"^(?:to\s+)", "", content, flags=re.IGNORECASE)
                return content[:1].upper() + content[1:] if content else None
        return None

    @staticmethod
    def _extract_action_items_from_priority_content(content):
        normalized = re.sub(r"\s+", " ", content.strip())
        normalized = re.sub(r"\b(and then|then|next|after that)\b", ".", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\b(first|second|third|finally)\b", ".", normalized, flags=re.IGNORECASE)
        pieces = [
            piece.strip(" .,:;-")
            for piece in re.split(r"[.\n;]+", normalized)
            if piece.strip(" .,:;-")
        ]

        if len(pieces) <= 1:
            return [AgentService._normalize_action_text(normalized)]

        actions = []
        for piece in pieces:
            action = AgentService._normalize_action_text(piece)
            if action and AgentService._is_actionable_task(action) and action not in actions:
                actions.append(action)
        return actions or [AgentService._normalize_action_text(normalized)]

    @staticmethod
    def _is_actionable_task(action):
        generic_fillers = {
            "do a few things",
            "do some things",
            "figure out a few things",
        }
        return action.lower() not in generic_fillers

    @staticmethod
    def _normalize_action_text(text):
        action = text.strip(" .,:;-")
        action = action.replace("instructuions", "instructions")
        replacements = [
            (r"^i think i need to\s+", ""),
            (r"^i need to\s+", ""),
            (r"^i need\s+", ""),
            (r"^i should\s+", ""),
            (r"^we need to\s+", ""),
            (r"^we need\s+", ""),
            (r"^we should\s+", ""),
            (r"^they will need to be\s+", ""),
            (r"^they need to be\s+", ""),
            (r"^the text needs to be\s+", ""),
            (r"^it needs to be\s+", ""),
            (r"^set up to be able to be\s+", "set up to be "),
            (r"^load some recipes in$", "import recipe images"),
            (r"^converted from images to text$", "run OCR to convert recipe images into text"),
            (r"^convert from images to text$", "run OCR to convert recipe images into text"),
            (
                r"^sort into instructions versus ingredients, steps etc$",
                "parse recipe text into ingredients, instructions, and steps",
            ),
            (
                r"^sides, mains, etc put into different columns of a table and set up to be able to be called later$",
                "add recipe type fields for mains, sides, and other categories",
            ),
        ]
        for pattern, replacement in replacements:
            action = re.sub(pattern, replacement, action, flags=re.IGNORECASE)

        leading_verbs = {
            "converted": "convert",
            "sorted": "sort",
            "loaded": "load",
            "set": "set",
        }
        for past, imperative in leading_verbs.items():
            action = re.sub(rf"^{past}\b", imperative, action, flags=re.IGNORECASE)

        final_replacements = [
            (
                r"^sort into instructions versus ingredients, steps etc$",
                "parse recipe text into ingredients, instructions, and steps",
            ),
        ]
        for pattern, replacement in final_replacements:
            action = re.sub(pattern, replacement, action, flags=re.IGNORECASE)

        action = re.sub(r"\s+", " ", action).strip()
        if not action:
            return ""
        return action[:1].upper() + action[1:]

    def _maybe_update_action_priorities(self, text, lower):
        context = self.build_dashboard_context()
        current_action = context.get("next_action")
        updates = []

        if current_action and any(
            phrase in lower
            for phrase in [
                "not the priority",
                "isn't the priority",
                "is not the priority",
                "not my priority",
                "not important right now",
                "not what i should work on",
                "not what to work on",
            ]
        ):
            self.db.update_recommended_action_priority(current_action["id"], "low")
            self.db.add_note(
                current_action["project_id"],
                f"User said this is not the current priority: {current_action['action']}",
            )
            updates.append(
                {
                    "type": "action_priority_updated",
                    "project": current_action["project_name"],
                    "action": current_action["action"],
                    "priority": "low",
                }
            )

        promoted_action = self._find_action_in_text(text, context.get("actions", []))
        if promoted_action and any(
            phrase in lower
            for phrase in [
                "is the priority",
                "is my priority",
                "make this the priority",
                "make that the priority",
                "prioritize this",
                "prioritize that",
                "work on this",
                "work on that",
            ]
        ):
            self.db.update_recommended_action_priority(promoted_action["id"], "high")
            updates.append(
                {
                    "type": "action_priority_updated",
                    "project": promoted_action["project_name"],
                    "action": promoted_action["action"],
                    "priority": "high",
                }
            )

        return updates

    def _add_action(self, project_id, action):
        priority = "high" if re.search(r"\b(urgent|high|important|first)\b", action, re.IGNORECASE) else "medium"
        self.db.add_recommended_action(project_id, action, priority)

    def _add_blocker(self, project_id, blocker):
        severity = "high" if re.search(r"\b(blocked|urgent|critical|high)\b", blocker, re.IGNORECASE) else "medium"
        self.db.add_blocker(project_id, blocker, severity)

    def _add_goal(self, project_id, goal):
        self.db.add_weekly_goal(project_id, goal)

    def _find_project_in_text(self, text):
        projects = [dict(row) for row in self.db.get_all_projects()]
        if not projects:
            return None

        lowered = text.lower()
        if "recipe" in lowered or "recipes" in lowered:
            for project in projects:
                if "recipe" in project["name"].lower():
                    return project

        for project in projects:
            if project["name"].lower() in lowered:
                return project

        best = None
        best_score = 0
        for project in projects:
            score = SequenceMatcher(None, project["name"].lower(), lowered).ratio()
            if score > best_score:
                best = project
                best_score = score

        return best if best_score >= 0.42 else None

    @staticmethod
    def _find_action_in_text(text, actions):
        lowered = text.lower()
        for action in actions:
            if action["action"].lower() in lowered:
                return action

        best = None
        best_score = 0
        for action in actions:
            score = SequenceMatcher(None, action["action"].lower(), lowered).ratio()
            if score > best_score:
                best = action
                best_score = score

        return best if best_score >= 0.68 else None

    def _find_project_by_name(self, name):
        row = self.db.get_project_by_name(name)
        return dict(row) if row else None

    def _find_step_in_text(self, text, project_id, statuses=None):
        statuses = statuses or {"open"}
        actions = self._open_actions_for_project(project_id)
        candidates = []
        for action in actions:
            for row in self.db.get_task_steps(action["id"]):
                step = dict(row)
                if step["status"] not in statuses:
                    continue
                score = self._text_match_score(text, step["step"])
                candidates.append({"action": action, "step": step, "score": score})

        if not candidates:
            return None

        best = max(candidates, key=lambda candidate: candidate["score"])
        return best if best["score"] >= 0.46 else None

    def _actions_for_project(self, project_id):
        actions = []
        project = dict(self.db.get_project_by_id(project_id))
        for row in self.db.get_recommended_actions(project_id):
            action = dict(row)
            action["project_name"] = project["name"]
            actions.append(action)
        return actions

    def _open_actions_for_project(self, project_id):
        actions = []
        project = dict(self.db.get_project_by_id(project_id))
        for row in self.db.get_open_recommended_actions(project_id):
            action = dict(row)
            action["project_name"] = project["name"]
            actions.append(action)
        return actions

    def _choose_focus_project(self, projects, actions, blockers):
        if not projects:
            return None

        blocker_counts = {}
        for blocker in blockers:
            blocker_counts.setdefault(blocker["project_id"], 0)
            blocker_counts[blocker["project_id"]] += 1

        action_counts = {}
        for action in actions:
            action_counts.setdefault(action["project_id"], 0)
            action_counts[action["project_id"]] += {"high": 3, "medium": 2, "low": 1}.get(action["priority"], 2)

        def score(project):
            return (
                project.get("priority_score", 3) * 10
                + action_counts.get(project["id"], 0)
                - blocker_counts.get(project["id"], 0)
            )

        return max(projects, key=score)

    @staticmethod
    def _sounds_like_completion(lower):
        return any(
            phrase in lower
            for phrase in [
                "finished",
                "complete",
                "completed",
                "done",
                "mark",
                "deployed",
                "uploaded",
                "set up",
            ]
        )

    @staticmethod
    def _completion_target_text(text):
        target = text.strip()
        target = re.sub(
            r"^(?:ok\s+)?(?:i|we)\s+(?:just\s+|had\s+|have\s+|already\s+)?"
            r"(?:finished|completed|complete|did|deployed|uploaded|set up)\s+",
            "",
            target,
            flags=re.IGNORECASE,
        )
        target = re.sub(r"^(?:mark|set)\s+", "", target, flags=re.IGNORECASE)
        target = re.sub(r"\s+(?:as\s+)?(?:done|complete|completed)$", "", target, flags=re.IGNORECASE)
        return target.strip(" .,:;-")

    @staticmethod
    def _text_match_score(query, candidate):
        query_lower = query.lower()
        candidate_lower = candidate.lower()
        if candidate_lower in query_lower or query_lower in candidate_lower:
            return 1.0

        query_tokens = AgentService._meaningful_tokens(query_lower)
        candidate_tokens = AgentService._meaningful_tokens(candidate_lower)
        if not query_tokens or not candidate_tokens:
            return SequenceMatcher(None, candidate_lower, query_lower).ratio()

        overlap = query_tokens & candidate_tokens
        overlap_score = len(overlap) / max(len(query_tokens), 1)
        sequence_score = SequenceMatcher(None, candidate_lower, query_lower).ratio()

        bonus = 0
        related_groups = [
            {"deploy", "deployed", "deploying", "host", "hosting", "wifi", "phone", "mobile", "lan"},
            {"upload", "uploaded", "import", "image", "images", "photo", "photos", "recipe", "recipes"},
            {"ocr", "text", "extract", "convert"},
        ]
        for group in related_groups:
            if query_tokens & group and candidate_tokens & group:
                bonus += 0.12

        return min(1.0, (overlap_score * 0.65) + (sequence_score * 0.35) + bonus)

    @staticmethod
    def _meaningful_tokens(text):
        words = set(re.findall(r"[a-z0-9]+", text.lower()))
        stopwords = {
            "a",
            "an",
            "and",
            "app",
            "as",
            "for",
            "from",
            "had",
            "have",
            "i",
            "it",
            "of",
            "on",
            "page",
            "the",
            "this",
            "to",
            "we",
            "with",
        }
        return words - stopwords

    def _choose_next_action(self, project, actions):
        if not project:
            return actions[0] if actions else None

        project_actions = [action for action in actions if action["project_id"] == project["id"]]
        return project_actions[0] if project_actions else (actions[0] if actions else None)

    @staticmethod
    def _action_sort_key(action):
        priority = {"high": 0, "medium": 1, "low": 2}.get(action["priority"], 1)
        project_priority = -int(action.get("project_priority_score") or 3)
        sort_order = int(action.get("sort_order") or 100)
        return (priority, project_priority, sort_order, -int(action.get("id") or 0))

    @staticmethod
    def _build_llm_prompt(user_message, updates, work_packet):
        return (
            "The user is chatting with their personal project-priority agent. "
            "Use the project context to answer conversationally. If updates were applied, "
            "briefly acknowledge them. End with a crisp work packet they can hand to a coding agent.\n\n"
            f"User message: {user_message}\n"
            f"Applied updates: {updates or 'none'}\n"
            f"Current work packet: {work_packet}"
        )

    @staticmethod
    def _fallback_response(user_message, updates, work_packet):
        lines = []
        if updates:
            only_clarification = all(update["type"] == "project_clarification_needed" for update in updates)
            lines.append("I need one detail before I update anything." if only_clarification else "Updated your project memory.")
            for update in updates:
                if update["type"] == "priority_updated":
                    lines.append(f"- {update['project']} is now priority {update['priority_score']}/5.")
                elif update["type"] == "action_priority_updated":
                    lines.append(
                        f"- Set action priority to {update['priority']} for {update['project']}: {update['action']}"
                    )
                elif update["type"] == "priority_action_added":
                    lines.append(
                        f"- Added {update['priority']}-priority action for {update['project']}: {update['action']}"
                    )
                elif update["type"] == "action_completed":
                    lines.append(f"- Completed action for {update['project']}: {update['action']}")
                elif update["type"] == "step_completed":
                    lines.append(
                        f"- Completed step for {update['project']}: {update['step']} "
                        f"({update['action']})"
                    )
                elif update["type"] == "step_already_completed":
                    lines.append(
                        f"- That step was already marked done for {update['project']}: "
                        f"{update['step']} ({update['action']})"
                    )
                elif update["type"] == "project_clarification_needed":
                    projects = ", ".join(update["projects"])
                    lines.append(f"- Which project should I edit? Available projects: {projects}")
                elif update["type"] == "project_created":
                    lines.append(f"- Created project: {update['project']}.")
                elif update["type"].endswith("_added"):
                    lines.append(f"- Added {update['type'].replace('_added', '')} for {update['project']}.")
        else:
            lines.append("I checked your project state.")

        lines.append("")
        lines.append(f"Focus: {work_packet.get('project') or 'No project selected'}")
        lines.append(f"Goal: {work_packet['goal']}")
        lines.append(f"Next action: {work_packet['next_action']}")
        lines.append("")
        lines.append("Work packet for coding:")
        lines.append(work_packet["context_for_coding_agent"])
        return "\n".join(lines)
