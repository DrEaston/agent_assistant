"""Tools Claude can call through Dieter's connector, grouped into one ToolSet per app."""

import os
from dataclasses import dataclass, field
from typing import Callable

from fastapi import Body
from fastapi.responses import JSONResponse

from core import internal


class ToolError(Exception):
    """Something Claude should know about (not found, bad input). The message becomes the tool's answer."""


def site_url(path):
    """A full link to a Dieter page, for Claude to hand to the person."""
    return (os.getenv("DIETER_PUBLIC_URL") or "https://askdieter.ai").rstrip("/") + path


def text_list(value, what, limit=100):
    """A list of non-empty strings from a tool argument (a single string is accepted too)."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ToolError(f"{what} should be a list.")
    out = [" ".join(str(v).split())[:200] for v in value if str(v).strip()]
    if not out:
        raise ToolError(f"{what} is empty.")
    return out[:limit]


def whole_number(value, what):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{what} should be a number.") from None


STRINGS = {"type": "array", "items": {"type": "string"}}


@dataclass
class Tool:
    name: str  # "<app>_<verb>_<thing>", e.g. kitchen_add_items
    title: str
    description: str
    handler: Callable[[dict], object]  # (arguments) -> anything JSON can hold; runs as the signed-in user
    params: dict = field(default_factory=dict)  # JSON Schema "properties"
    required: tuple = ()
    read_only: bool = False
    destructive: bool = False

    def schema(self):
        annotations = {"title": self.title, "readOnlyHint": self.read_only, "openWorldHint": False}
        if not self.read_only:
            annotations["destructiveHint"] = self.destructive
        return {"name": self.name, "title": self.title, "description": self.description,
                "inputSchema": {"type": "object", "properties": self.params, "required": list(self.required)},
                "annotations": annotations}


class ToolSet:
    """One app's tools. Every name starts with `prefix` + "_", so the server knows which set to ask."""

    def __init__(self, prefix, tools):
        self.prefix = prefix
        self.tools = {t.name: t for t in tools}
        for name in self.tools:
            if not name.startswith(prefix + "_"):
                raise ValueError(f"{name} should start with {prefix}_")

    def owns(self, name):
        return name.startswith(self.prefix + "_")

    def list(self, user=None):
        return [t.schema() for t in self.tools.values()]

    def call(self, name, arguments, user=None):
        tool = self.tools.get(name)
        if not tool:
            raise ToolError(f"There's no tool called {name}.")
        return tool.handler(dict(arguments or {}))


class RemoteToolSet:
    """The ToolSet of an app that runs as its own service, reached with signed calls (core/internal.py)."""

    def __init__(self, prefix, base_url, path, call=None):
        self.prefix, self.base_url, self.path = prefix, base_url, path
        self._call = call or internal.call

    def owns(self, name):
        return name.startswith(self.prefix + "_")

    def list(self, user=None):
        try:
            return self._call(self.base_url, "GET", self.path, user, timeout=10)["tools"]
        except Exception as exc:
            print(f"Warning: {self.prefix} tools unavailable: {exc}")
            return []

    def call(self, name, arguments, user=None):
        import httpx

        try:
            data = self._call(self.base_url, "POST", f"{self.path}/{name}", user, {"arguments": arguments or {}},
                              timeout=60)
        except httpx.HTTPStatusError as exc:
            try:
                detail = exc.response.json().get("error")
            except ValueError:
                detail = None
            raise ToolError(detail or f"{self.prefix} couldn't do that ({exc.response.status_code}).") from exc
        return data.get("result")


def serve_tools(router, path, toolset):
    """Expose `toolset` on an app service's router: GET path lists the tools, POST path/{name} runs one.
    The service's own middleware has already checked the signed user."""

    @router.get(path)
    def list_tools():
        return {"tools": toolset.list()}

    @router.post(path + "/{name}")
    def call_tool(name: str, payload: dict = Body(default={})):
        try:
            return {"result": toolset.call(name, payload.get("arguments") or {})}
        except ToolError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
