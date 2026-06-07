"""
LLM Integration - Claude API for intelligent project management
"""

import anthropic
import json
from typing import Optional

# Initialize Anthropic client
client = anthropic.Anthropic()

SYSTEM_PROMPT = """You are an intelligent project management assistant helping users track and manage technical projects.

You have access to the user's project data via APIs. You can:
1. Analyze their projects, blockers, and actions
2. Suggest next steps and help prioritize work
3. Answer questions about project status
4. Help break down goals into actionable steps
5. Identify patterns and potential bottlenecks

When the user asks about their projects, use the context provided to give specific, actionable advice.
Keep responses concise but helpful. Be proactive in suggesting optimizations or highlighting risks."""


def chat_with_projects(
    user_message: str,
    project_context: Optional[dict] = None,
    conversation_history: Optional[list] = None
) -> str:
    """
    Chat with Claude about projects using the provided context.
    
    Args:
        user_message: The user's question or request
        project_context: Dict with keys: projects, recommended_project, next_action, blockers, actions, goals, stats
        conversation_history: List of previous messages for multi-turn conversation
    
    Returns:
        Claude's response as a string
    """
    
    # Build context string from project data
    context_str = ""
    if project_context:
        context_str = _build_context_string(project_context)
    
    # Build messages list
    messages = conversation_history or []
    
    # Add current user message
    messages.append({
        "role": "user",
        "content": user_message
    })
    
    # Add project context if available
    if context_str:
        # Prepend context to the first user message or inject as system info
        if messages and messages[0]["role"] == "user" and not context_str in messages[0]["content"]:
            messages[0]["content"] = f"[Project Context]\n{context_str}\n\n{messages[0]['content']}"
    
    # Call Claude API
    response = client.messages.create(
        model="claude-3-5-sonnet-20241022",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=messages
    )
    
    assistant_message = response.content[0].text
    
    return assistant_message


def suggest_next_actions(project_context: dict) -> str:
    """Get Claude's suggestions for next actions based on current project state."""
    
    prompt = """Based on the project context below, what should the user focus on right now? 
    Please provide 2-3 specific, actionable next steps in order of priority.
    
    Be concise and practical."""
    
    return chat_with_projects(prompt, project_context)


def analyze_blockers(project_context: dict) -> str:
    """Get Claude's analysis of current blockers and suggestions to unblock."""
    
    prompt = """Analyze the blockers in my projects and suggest strategies to resolve them.
    What patterns do you see? Are there any quick wins? What needs more investigation?"""
    
    return chat_with_projects(prompt, project_context)


def plan_week(project_context: dict) -> str:
    """Get Claude's suggestions for weekly planning."""
    
    prompt = """Help me plan this week. Given my current projects, blockers, and goals, 
    what should I prioritize? What's realistic to accomplish this week?"""
    
    return chat_with_projects(prompt, project_context)


def _build_context_string(project_context: dict) -> str:
    """Convert project context dict to a readable string for Claude."""
    
    lines = []
    
    # Summary stats
    stats = project_context.get("stats", {})
    lines.append(f"📊 Current Status: {stats.get('total_projects', 0)} projects, "
                 f"{stats.get('total_blockers', 0)} blockers, "
                 f"{stats.get('total_actions', 0)} actions")
    
    # Recommended project
    if project_context.get("recommended_project"):
        proj = project_context["recommended_project"]
        lines.append(f"\n🎯 Recommended Focus: {proj.get('name')} - {proj.get('description', '')}")
    
    # Next action
    if project_context.get("next_action"):
        action = project_context["next_action"]
        lines.append(f"⚡ Next Action: [{action.get('project_name', 'Unknown')}] {action.get('title', '')}")
    
    # Active projects
    projects = project_context.get("projects", [])
    if projects:
        lines.append(f"\n📁 Projects ({len(projects)}):")
        for proj in projects:
            lines.append(f"  • {proj.get('name')}: {proj.get('description', 'No description')}")
    
    # Blockers
    blockers = project_context.get("blockers", [])
    if blockers:
        lines.append(f"\n🚧 Blockers ({len(blockers)}):")
        for blocker in blockers[:5]:  # Show top 5
            severity = blocker.get("severity", "medium").upper()
            lines.append(f"  • [{severity}] {blocker.get('project_name')}: {blocker.get('description')}")
        if len(blockers) > 5:
            lines.append(f"  ... and {len(blockers) - 5} more")
    
    # Active actions
    actions = project_context.get("actions", [])
    if actions:
        lines.append(f"\n📋 Active Actions ({len(actions)}):")
        for action in actions[:5]:  # Show top 5
            priority = action.get("priority", "medium").upper()
            lines.append(f"  • [{priority}] {action.get('project_name')}: {action.get('title')}")
        if len(actions) > 5:
            lines.append(f"  ... and {len(actions) - 5} more")
    
    # Goals
    goals = project_context.get("goals", [])
    if goals:
        completed = sum(1 for g in goals if g.get("completed"))
        lines.append(f"\n🎯 Weekly Goals: {completed}/{len(goals)} complete")
        for goal in goals:
            status = "✓" if goal.get("completed") else "○"
            lines.append(f"  {status} [{goal.get('project_name')}] {goal.get('title')}")
    
    return "\n".join(lines)
