"""Claude working through Dieter's tools, for Dieter's own chat (the same ToolSets the connector serves).

run() is a plain tool loop on the Messages API: Claude answers or calls tools; tool results go back until it
answers. Settings: ANTHROPIC_API_KEY, DIETER_CLAUDE_MODEL (default claude-opus-5-5).
"""

import json
import os

from .tools import ToolError

FALLBACK_BETA = "server-side-fallback-2026-07-01"
_client = None


def model_name():
    return os.getenv("DIETER_CLAUDE_MODEL") or "claude-opus-5-5"


def available():
    return bool((os.getenv("ANTHROPIC_API_KEY") or "").strip())


def _anthropic():
    global _client
    if _client is None:
        import anthropic

        # One client, reused: a fresh connection costs a noticeable moment on every request.
        _client = anthropic.Anthropic(api_key=(os.getenv("ANTHROPIC_API_KEY") or "").strip(), timeout=60,
                                      max_retries=1)
    return _client


def create(system, messages, tools, effort="low"):
    """One Messages API call. If a safety check declines, Anthropic re-runs it on its recommended fallback model."""
    return _anthropic().beta.messages.create(
        model=model_name(), max_tokens=16000, system=system, messages=messages, tools=tools,
        output_config={"effort": effort}, betas=[FALLBACK_BETA], fallbacks="default")


def api_tools(schemas):
    """MCP-style tool schemas (ToolSet.list()) as Messages API tool definitions."""
    return [{"name": s["name"], "description": s["description"], "input_schema": s["inputSchema"]} for s in schemas]


def cached_system(text):
    """The system prompt as a cached block: it (and the tools before it) is the same on every request."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def run(system, messages, tools, call_tool, effort="low", max_rounds=6, model_call=None):
    """Loop until Claude answers. call_tool(name, input) -> result (str or JSON-able); raise ToolError for a
    problem Claude should see. Returns the final text ("" if Claude only used tools)."""
    model_call = model_call or create
    messages = list(messages)
    for _ in range(max_rounds):
        response = model_call(system=system, messages=messages, tools=tools, effort=effort)
        if response.stop_reason == "refusal":
            return "I can't help with that one."
        messages.append({"role": "assistant", "content": response.content})
        if response.stop_reason == "pause_turn":
            continue
        uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not uses:
            return "\n".join(b.text for b in response.content if b.type == "text").strip()
        results = []
        for use in uses:  # every result goes back in one message
            try:
                out = call_tool(use.name, use.input if isinstance(use.input, dict) else {})
                text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)
                results.append({"type": "tool_result", "tool_use_id": use.id, "content": text or "Done."})
            except ToolError as exc:
                results.append({"type": "tool_result", "tool_use_id": use.id, "content": str(exc), "is_error": True})
            except Exception as exc:
                print(f"Warning: Dieter tool {use.name} failed: {exc!r}")
                results.append({"type": "tool_result", "tool_use_id": use.id, "is_error": True,
                                "content": f"{use.name} hit an error. Carry on without it, and say so if it matters."})
        messages.append({"role": "user", "content": results})
    return "That took more steps than I expected. Try asking it a simpler way?"
