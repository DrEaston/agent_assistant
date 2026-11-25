"""Dieter's chat, for any app: Claude with Dieter's persona, every app's tools, page opening and proposals.

An app joins by handing over (see docs/connector.md):
- its ToolSet (apps/<app>/connector.py) and a few lines of CHAT_NOTES on how to use it;
- its pages (apps/<app>/nav.py), which Dieter can open and also uses to say which page the person is on;
- for changes that wait for an OK (core/connector/proposals.py): a saver per proposal kind and a pending() list.

answer() returns {"reply": str, "do": [actions]} where the actions are what core/ui/static/dieter-actions.js
carries out: navigate to a page, show a proposal card, reload.
"""

import contextvars
from dataclasses import dataclass, field
from typing import Callable

from . import agent
from .pages import open_page_tool
from .proposals import is_card, said_save
from .tools import ToolError

# What the person said, while their message is being answered: a tool that makes a proposal records it with the change.
REQUEST = contextvars.ContextVar("dieter_chat_request", default="")

PERSONA = """You are Dieter, the assistant in Curtis's household app (askdieter.ai): Kitchen (recipes, meal plan,
shopping lists), the Thanksgiving planner, and more. You're talking to the person in Dieter's chat on the page
they're looking at. You are Dieter; never call yourself Claude or an AI model.

Personality: warm, quick, a little sassy. One light quip at most, never at the expense of being clear.

Use the tools to look things up rather than guessing, and to make the changes they ask for.

Showing things: when they ask to see, open, show or pull up something ("pull up the chili recipe", "open my
shopping list"), call dieter_open_page; their screen changes right away and this chat stays open. Say in a few words
what you opened. Don't open pages they didn't ask for.

Changes that wait for an OK: some tools (their names have "propose") don't change anything; they put a review card
on the person's screen. Say in a sentence what you're proposing and ask them to look it over. Call save_change only
when their latest message plainly says to save it ("save it", "yes, save the change"). A bare "yes", "ok" or "sure"
is not enough: ask them to tap Save or say "save it".

Your reply: 1-3 short sentences of plain text, no markdown or lists (it may be read aloud)."""

# Asked for something no tool can do: say so kindly and log it (a Studio report) instead of pretending or dropping it.
# Any app's chat can use these (Thanksgiving's brain.py does too).
LOG_TOOL = {"name": "log_for_later",
            "description": "Log something they asked for that you can't do with your tools, so it gets reviewed "
                           "later. One short line on what they wanted.",
            "input_schema": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}
LOG_NOTE = """Things you can't do: if they ask for something none of your tools can do (a change no tool makes, a new
feature, something about how the site itself works), don't pretend and don't just refuse. Call log_for_later with one
short line on what they wanted, then tell them kindly that you can't do that yet and you've logged it to review later.
Don't log questions you answered or things you did."""


def log_reply(text):
    """The tool's answer to Claude."""
    return "Logged for later review. Tell them kindly you can't do that yet and that you've logged it."


SAVE_TOOL = {"name": "save_change",
             "description": "Save a change waiting for an OK (listed under 'Waiting for their OK'). Only when the "
                            "person's latest message plainly says to save it.",
             "input_schema": {"type": "object", "properties": {"proposal_id": {"type": "string"}},
                              "required": ["proposal_id"]}}


@dataclass
class ChatSetup:
    """Everything one site's chat can do. Build it once; answer() is called per message."""
    toolsets: list  # ToolSet / RemoteToolSet
    page_lists: list  # [nav.PAGES, ...]
    notes: list = field(default_factory=list)  # each app's CHAT_NOTES
    savers: dict = field(default_factory=dict)  # proposal kind -> save(proposal_id) -> card
    pending: Callable[[], list] = lambda: []  # cards waiting for an OK (this person's)
    offer: Callable[[dict], bool] = lambda schema: True  # which tools (schemas) this chat offers
    log_for_later: Callable = None  # (summary, what they said, page_url) -> None: files it for review (LOG_TOOL)
    persona: str = PERSONA

    def pages(self):
        return {p.name: p for pages in self.page_lists for p in pages}

    def where(self, page_url, page_title=""):
        """Which page the person is on, as Dieter's page names and params where it's one of them."""
        path = (page_url or "").split("#")[0].split("?")[0]
        for page in self.pages().values():
            params = page.match(path)
            if params is not None:
                shown = ", ".join(f"{k}={v}" for k, v in params.items())
                return f"{page.name} ({page.title}{': ' + shown if shown else ''}), titled {page_title!r}"
        return f"{path or '/'} titled {page_title!r}"


def _schemas(setup):
    schemas = []
    for toolset in setup.toolsets:
        try:
            schemas += [s for s in toolset.list() if setup.offer(s)]
        except Exception as exc:
            print(f"Warning: {getattr(toolset, 'prefix', 'some')} tools unavailable: {exc}")
    return schemas


def _system(setup):
    notes = list(setup.notes) + ([LOG_NOTE] if setup.log_for_later else [])
    return agent.cached_system(setup.persona + "".join("\n\n" + n.strip() for n in notes if n))


def _tools(setup, schemas):
    open_page = open_page_tool(setup.page_lists, on_open=lambda url, page: "")
    return (agent.api_tools(schemas + [open_page.schema()]) + ([SAVE_TOOL] if setup.savers else [])
            + ([LOG_TOOL] if setup.log_for_later else []))


def prime(setup):
    """When the chat opens: have Claude read and cache the instructions and tools, so the first answer is quick."""
    agent.prime(_system(setup), _tools(setup, _schemas(setup)))


def answer(setup, text, history=(), page_url="", page_title="", model_call=None):
    text = str(text or "").strip()[:1000]
    state = {"do": []}

    def opened(url, page):
        state["do"] = [a for a in state["do"] if a.get("type") != "navigate"]
        state["do"].append({"type": "navigate", "url": url, "title": page.title})  # one page per answer: the last
        return f"Opening {page.title} on their screen now."

    open_page = open_page_tool(setup.page_lists, on_open=opened)
    schemas = _schemas(setup)
    names = {s["name"] for s in schemas}
    tools = _tools(setup, schemas)
    pending = []
    try:
        pending = list(setup.pending() or [])
    except Exception as exc:
        print(f"Warning: pending proposals unavailable: {exc}")

    def call_tool(name, args):
        if name == open_page.name:
            return open_page.handler(args)
        if name == "save_change":
            if not said_save(text):
                raise ToolError("Not saved: they haven't plainly said to save it. Ask them to tap Save on the card "
                                "or say \"save it\".")
            proposal_id = str(args.get("proposal_id") or "")
            card = next((c for c in pending if c.get("id") == proposal_id), None)
            saver = setup.savers.get(card.get("kind")) if card else None
            if not saver:
                raise ToolError("That change isn't waiting any more.")
            saved = saver(proposal_id)
            state["do"] = [a for a in state["do"] if a.get("type") != "reload"] + [
                {"type": "decided", "id": proposal_id, "status": "saved"}, {"type": "reload"}]
            return {"saved": True, "title": saved.get("title", "")}
        if name == "log_for_later" and setup.log_for_later:
            setup.log_for_later(str(args.get("summary") or "").strip()[:200] or text[:200], text, page_url)
            return log_reply(text)
        if name not in names:
            raise ToolError(f"There's no tool called {name}.")
        toolset = next(t for t in setup.toolsets if t.owns(name))
        out = toolset.call(name, args)
        if is_card(out):
            state["do"] = [a for a in state["do"] if a.get("type") != "proposal"] + [{"type": "proposal", "card": out}]
            here = (page_url or "").split("#")[0]
            if out.get("open_url") and out["open_url"] != here and not any(a["type"] == "navigate" for a in state["do"]):
                state["do"].append({"type": "navigate", "url": out["open_url"], "title": out.get("title", "")})
        return out

    system = _system(setup)
    messages = [{"role": h["role"], "content": str(h["content"])[:2000]} for h in list(history)[-8:]
                if isinstance(h, dict) and h.get("role") in ("user", "assistant") and str(h.get("content") or "").strip()]
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    while messages and messages[-1]["role"] == "user":  # an unanswered message (e.g. it failed): drop it
        messages.pop()
    context = f"They're looking at: {setup.where(page_url, page_title)}."
    if pending:
        context += "\nWaiting for their OK: " + "; ".join(f"{c['title']} (proposal_id {c['id']})" for c in pending[:5])
    messages.append({"role": "user", "content": f"{context}\n\n---\nThey say: {text}"})
    token = REQUEST.set(text)
    try:
        reply = agent.run(system, messages, tools, call_tool, effort="low", model_call=model_call)
    finally:
        REQUEST.reset(token)
    return {"reply": (reply or "Done.")[:1200], "do": state["do"]}
