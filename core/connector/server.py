"""The /mcp endpoint: Model Context Protocol over HTTP ("Streamable HTTP"), answered as plain JSON.

Stateless: every POST carries one JSON-RPC message (or a list), Claude's bearer token says who it's for, and no
session or event stream is kept. Supported: initialize, ping, tools/list, tools/call. Notifications get 202.
Spec: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
"""

import json
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .oauth import public_base
from .tools import ToolError

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
# The version changes with every deploy (Cloud Run sets K_REVISION), so a client that compares it knows to fetch
# tools/list again.
SERVER_INFO = {"name": "dieter", "title": "Dieter", "version": os.getenv("K_REVISION") or "1.0"}

# Claude (and other clients) keep the tool list from when the person connected and don't always fetch it again,
# so a tool added later can be invisible until they reconnect. These two are always in the list: through them a
# client reaches every tool Dieter has now, with the same checks as calling it directly.
FIND_TOOLS, USE_TOOL = "dieter_find_tools", "dieter_use_tool"
META_TOOLS = [
    {"name": FIND_TOOLS, "title": "Find Dieter tools",
     "description": "Every tool Dieter has right now, with what it does and its inputs. Use it when the tool you need "
                    "isn't in your list: your list can be older than Dieter. query narrows it (words in the name or "
                    "description).",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
     "annotations": {"readOnlyHint": True}},
    {"name": USE_TOOL, "title": "Use a Dieter tool",
     "description": "Run any Dieter tool by name (from dieter_find_tools) with its arguments. Same result and checks "
                    "as calling it directly.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"},
                                                      "arguments": {"type": "object"}},
                     "required": ["name"]}},
]

INSTRUCTIONS = """Dieter is this person's household app (askdieter.ai): Kitchen (recipes, shopping lists and the
Thanksgiving planner), Assistant (projects, to-dos, reading) and the Logbook (checked tables), with more to come. Tools act on their real data, as them.

- Look before you change: read a list or plan first, then use the item keys and ids it gives you.
- Change things directly when asked ("add milk", "check off the eggs"); say what you changed afterwards.
- Shopping: when they couldn't get something, offer to move it to a list for the next trip (kitchen_finish_shopping_list,
  or thanksgiving_finish_trip for the Thanksgiving list) rather than checking it off.
- Thanksgiving: thanksgiving_get_plan has every dish, step and time. Plan changes go through
  thanksgiving_change_plan; preview first when a change moves several steps.
- Logbook: their checked tables (race results, records, lists they keep) and the source of fact. When a question
  might be answered there, logbook_find first instead of answering from memory; say so when it isn't there. Answers,
  summaries and images use only the verified version (quote its label) and logbook_summarize's numbers, not your own
  arithmetic. Changes are proposals they approve on the Logbook page; you can't approve or verify.
- Building: Dieter's code is changed by Claude Code, not by you. When they ask you to have something built or
  changed in Dieter, send it with studio_request_change (the outcome they want and how they'll check it, not code).
- When they want to see something in Dieter (a recipe, a list, a day of the plan), give the link from dieter_open_page.
- Keep answers short; they're often reading on a phone in a store or kitchen.
- Your tool list may be older than Dieter. If a tool you need is missing, dieter_find_tools lists them all and
  dieter_use_tool runs one."""


def make_router(toolsets, authenticate, run_as, signer=lambda user: user, instructions=INSTRUCTIONS):
    """toolsets: ToolSet / RemoteToolSet list. authenticate(request) -> user or None.
    run_as(user, fn) runs fn() with the app's current-user set (fn runs in a worker thread).
    signer(user) -> the user fields remote services need."""
    router = APIRouter()

    def challenge(request, invalid):
        base = public_base(request)
        value = f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'
        if invalid:
            value += ', error="invalid_token"'
        return JSONResponse({"error": "Sign in to Dieter first."}, status_code=401,
                            headers={"WWW-Authenticate": value})

    def owner(name):
        return next((s for s in toolsets if s.owns(name)), None)

    def all_tools(user):
        tools = []
        for toolset in toolsets:
            tools.extend(toolset.list(signer(user)))
        return tools

    def find_tools(user, arguments):
        words = str(arguments.get("query") or "").lower().split()
        found = [t for t in all_tools(user)
                 if all(w in f"{t.get('name', '')} {t.get('description', '')}".lower() for w in words)]
        listed = []
        for t in found:
            entry = {"name": t["name"], "description": t.get("description") or "",
                     "inputSchema": t.get("inputSchema") or {}}
            if file_params(t):
                entry["takes_files"] = file_params(t)
                entry["call_directly"] = FILE_TOOL_NOTE
            listed.append(entry)
        return {"tools": listed}

    def tool_named(user, name):
        return next((t for t in all_tools(user) if t.get("name") == name), None)

    def call_tool(user, params):
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if name == FIND_TOOLS:
            return {"content": [{"type": "text", "text": json.dumps(find_tools(user, arguments), ensure_ascii=False,
                                                                    indent=1)}], "isError": False}
        if name == USE_TOOL:
            inner = str(arguments.get("name") or "")
            if inner in (FIND_TOOLS, USE_TOOL) or not owner(inner):
                return {"content": [{"type": "text", "text": f"Dieter has no tool called {inner!r}. "
                                                             "dieter_find_tools lists them."}], "isError": True}
            if file_params(tool_named(user, inner) or {}):
                return {"content": [{"type": "text", "text": json.dumps(
                    {"error": "needs_direct_call", "tool": inner, "message": FILE_TOOL_NOTE},
                    ensure_ascii=False)}], "isError": True}
            params = {"name": inner, "arguments": arguments.get("arguments")}
            name = inner
        toolset = owner(name)
        if not toolset:
            return None
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        try:
            result = toolset.call(name, arguments, signer(user))
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except Exception as exc:  # a bug, or a service that's down: tell Claude rather than failing the request
            print(f"Warning: connector tool {name} failed: {exc!r}")
            return {"content": [{"type": "text", "text": f"Dieter hit an error running {name}. Try again, or "
                                                          "use the website for this one."}], "isError": True}
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=1, default=str)
        return {"content": [{"type": "text", "text": text}], "isError": False}

    async def handle(message, user):
        method, msg_id = message.get("method"), message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "initialize":
            asked = params.get("protocolVersion")
            return _result(msg_id, {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                                    "capabilities": {"tools": {"listChanged": False}},
                                    "serverInfo": SERVER_INFO, "instructions": instructions})
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            listed = await run_in_threadpool(run_as, user, lambda: all_tools(user))
            return _result(msg_id, {"tools": META_TOOLS + files_first(listed)})
        if method == "tools/call":
            answer = await run_in_threadpool(run_as, user, lambda: call_tool(user, params))
            if answer is None:
                return _error(msg_id, -32602, f"Unknown tool: {params.get('name')}")
            return _result(msg_id, answer)
        if method in ("resources/list", "prompts/list"):
            return _result(msg_id, {method.split("/")[0]: []})
        return _error(msg_id, -32601, f"Method not found: {method}")

    @router.post("/mcp")
    async def mcp(request: Request):
        user = authenticate(request)
        if not user:
            return challenge(request, invalid=bool(request.headers.get("authorization")))
        try:
            payload = json.loads(await request.body() or b"null")
        except ValueError:
            return JSONResponse(_error(None, -32700, "Parse error"), status_code=400)
        messages = payload if isinstance(payload, list) else [payload]
        answers = []
        for message in messages:
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                answers.append(_error(None, -32600, "Invalid request"))
            elif "method" in message and "id" in message:
                answers.append(await handle(message, user))
            # else: a notification or a response to us; nothing to answer
        if not answers:
            return Response(status_code=202)
        return JSONResponse(answers if isinstance(payload, list) else answers[0])

    @router.get("/mcp")
    @router.delete("/mcp")
    def mcp_no_stream():
        return Response(status_code=405, headers={"Allow": "POST"})

    return router


FILE_TOOL_NOTE = ("This tool takes a file. ChatGPT only attaches a file to a tool that's in its own tool list "
                  "(it reads the tool's openai/fileParams), so it can't be run through dieter_use_tool. Ask Curtis "
                  "to refresh the Dieter connector in ChatGPT's settings (Apps & Connectors, Dieter, Refresh, or "
                  "disconnect and connect again), then call the tool directly.")


def file_params(tool):
    """The parameters a tool takes as files (ChatGPT's _meta["openai/fileParams"]), or []."""
    meta = tool.get("_meta") if isinstance(tool, dict) else None
    params = meta.get("openai/fileParams") if isinstance(meta, dict) else None
    return list(params) if isinstance(params, list) else []


def files_first(tools):
    """Tools that take a file go first. ChatGPT may not hand the model every tool in a long list, and it can only
    attach a file to a tool the model was given (dieter_use_tool can't carry one), so those mustn't fall off the end."""
    return sorted(tools, key=lambda t: not (t.get("_meta") or {}).get("openai/fileParams"))


def _result(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
