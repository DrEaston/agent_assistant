"""Claude as Tell Dieter's brain (typed, voice and hands-free), chosen per device under "This device" → Brain.

understand() keeps voice.understand()'s contract: {"reply", "question", "actions", "ignore", "end"}. Plan changes come
back as actions and go through voice.check() exactly as before (a preview to Apply, or saved right away with Undo
for check-offs and shopping-list adds), so the rest of the pipeline is unchanged. On top of that Claude can read a
dish's recipe, the Thanksgiving and Kitchen shopping lists and the recipe library, and sort out the Thanksgiving
list after a trip, using the same tools as the Claude connector (core/connector).
"""

from core.connector import agent
from core.connector.pages import open_page_tool
from core.connector.tools import ToolError

from . import connector, nav, voice
from .. import nav as kitchen_nav

_ACTIONS = voice.SYSTEM_PROMPT.split("Allowed actions", 1)[1].split("How to think:", 1)[0]
_THINKING = voice.SYSTEM_PROMPT.split("How to think:", 1)[1]

PERSONA = f"""You are Dieter, the kitchen sidekick in Curtis's household app: his Thanksgiving plan, shopping lists and
recipes. You're talking to the cook (usually Curtis) in Dieter's chat or out loud in the kitchen. You are Dieter; never
call yourself Claude or an AI model.

Personality: warm, quick, a little sassy. One light quip at most, never mean, never at the expense of being clear.
(A sass level given below for this device overrides this.)

The plan, with every dish id and step id, is in the cook's latest message. Answer from it; use tools to read a dish's
recipe, the shopping lists or the recipe library when you need more.

Changing the plan: call propose_changes with actions. Done and grocery actions are saved right away (the screen shows
Undo), so say they're done. Everything else is shown to the cook as a preview first, so say what you WILL change, never
that it's done, and don't mention Apply or saving. Actions (dish ids and step ids exactly as listed in the plan){_ACTIONS}
The Thanksgiving shopping list: thanksgiving_check_items, thanksgiving_finish_trip and thanksgiving_put_back change it
directly. When they couldn't get something at the store, suggest a swap that store is likely to have, and offer to move
it to a next-trip list (thanksgiving_finish_trip with next_trip). Kitchen shopping lists (everyday lists, not
Thanksgiving) use the kitchen_ tools.

Showing things: when they ask to see, open, show or pull up something ("pull up the pie recipe", "show me Wednesday",
"open my next-trip list"), call dieter_open_page; their screen changes right away and this chat stays open. Say in a
few words what you opened. Don't open pages they didn't ask for.

How to think:{_THINKING}

Your reply: 1-3 short sentences of plain text, no markdown or lists (it may be read aloud)."""

LOCAL_TOOLS = ("thanksgiving_get_dish", "thanksgiving_get_shopping_list", "thanksgiving_check_items",
               "thanksgiving_finish_trip", "thanksgiving_put_back")

EXTRA_TOOLS = [
    {"name": "propose_changes", "description": "Change the Thanksgiving plan (actions as described in the system "
                                               "prompt). Returns how each change reads and any problems.",
     "input_schema": {"type": "object", "properties": {"actions": {"type": "array", "items": {"type": "object"}}},
                      "required": ["actions"]}},
]
SPOKEN_TOOLS = [
    {"name": "stay_quiet", "description": "The phrase the mic heard wasn't meant for you. Call this and reply with "
                                          "nothing.", "input_schema": {"type": "object", "properties": {}}},
    {"name": "end_conversation", "description": "The cook is wrapping up. Call this, then give a short sign-off.",
     "input_schema": {"type": "object", "properties": {}}},
]


def _spoken(prompt):
    """The hands-free instructions, with its JSON answers turned into tool calls."""
    return (prompt.replace('reply with {"ignore": true} and nothing else', "call stay_quiet and say nothing")
            .replace('add "end": true', "call end_conversation"))


def understand(text, summary, history=(), sass="normal", spoken_prompt="", handoff="", plan=None, context=None,
               kitchen_tools=None, model_call=None):
    """Claude's answer to one request. plan/context: the plan and plan_context() it was asked about (to check
    proposed changes). kitchen_tools: the Kitchen ToolSet (local or remote), or None."""
    local = connector.tools()
    kitchen_schemas = []
    if kitchen_tools is not None:
        try:
            kitchen_schemas = kitchen_tools.list()
        except Exception as exc:
            print(f"Warning: Kitchen tools unavailable: {exc}")
    schemas = [s for s in local.list() if s["name"] in LOCAL_TOOLS] + kitchen_schemas
    tools = agent.api_tools(schemas) + EXTRA_TOOLS + (SPOKEN_TOOLS if spoken_prompt else [])
    state = {"actions": [], "ignore": False, "end": False, "do": []}

    def opened(url, page):
        state["do"] = [{"type": "navigate", "url": url, "title": page.title}]  # one page per answer: the last one
        return f"Opening {page.title} on their screen now."

    open_page = open_page_tool([nav.PAGES, kitchen_nav.PAGES], on_open=opened)
    tools.append(agent.api_tools([open_page.schema()])[0])

    def call_tool(name, args):
        if name == "propose_changes":
            raw = [a for a in (args.get("actions") or []) if isinstance(a, dict)]
            if not raw:
                raise ToolError("actions is empty.")
            _, changes, problems = voice.check(raw, plan, context["dishes"], context["tasks"], context["cooks"])
            state["actions"].extend(raw)
            return {"changes": changes, "problems": problems}
        if name == open_page.name:
            return open_page.handler(args)
        if name == "stay_quiet":
            state["ignore"] = True
            return "OK. Say nothing."
        if name == "end_conversation":
            state["end"] = True
            return "OK. Sign off briefly."
        if name in LOCAL_TOOLS:
            return local.call(name, args)
        if kitchen_tools is not None and kitchen_tools.owns(name):
            return kitchen_tools.call(name, args)
        raise ToolError(f"There's no tool called {name}.")

    system = agent.cached_system(PERSONA)
    if voice.SASS_LEVELS.get(sass):
        system.append({"type": "text", "text": voice.SASS_LEVELS[sass]})
    if spoken_prompt:
        system.append({"type": "text", "text": _spoken(spoken_prompt)})
    messages = [{"role": h["role"], "content": str(h["content"])[:2000]} for h in list(history)[-8:]
                if h.get("role") in ("user", "assistant") and str(h.get("content") or "").strip()]
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    note = f"\n(You're picking this up because: {handoff[:300]})" if handoff else ""
    messages.append({"role": "user", "content": f"The plan right now:\n{summary}{note}\n\n---\nThe cook says: {text[:1000]}"})
    reply = agent.run(system, messages, tools, call_tool, effort="low", model_call=model_call)
    if state["ignore"]:
        return {"reply": "", "question": "", "actions": [], "ignore": True, "end": False}
    return {"reply": reply[:800], "question": "", "actions": state["actions"][:15], "ignore": False,
            "end": bool(spoken_prompt) and state["end"], "do": state["do"]}
