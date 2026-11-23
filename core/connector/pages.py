"""Pages Dieter can open for the person, listed by each app next to its tools.

An app describes its pages once (apps/<app>/pages.py). open_page_tool() turns every app's list into one tool:
- in Dieter's own chat it moves the person's screen there (the answer carries a "navigate" action, which the shared
  script core/ui/static/dieter-actions.js carries out);
- through the Claude connector it returns a link to tap, since Claude can't steer their browser.
"""

import re
from dataclasses import dataclass, field

from .tools import Tool, ToolError, site_url

# What a page parameter may be. Values are checked before they go into an address.
KINDS = {
    "integer": (re.compile(r"\d{1,9}"), "a whole number"),
    "date": (re.compile(r"\d{4}-\d{2}-\d{2}"), "a date like 2026-11-26"),
    "slug": (re.compile(r"[a-z0-9][a-z0-9-]{0,60}"), "a short id like dough"),
}


@dataclass
class Page:
    name: str  # "<app>.<page>", e.g. "thanksgiving.dish"
    title: str
    description: str
    path: str  # "/thanksgiving/menu/{dish_id}": every {param} in the path is required
    params: dict = field(default_factory=dict)  # name -> kind (KINDS)
    fragments: tuple = ()  # optional "#..." targets, first whose params were all given wins: ("task-{dish_id}-{step_id}",)

    def url(self, values):
        values = {k: str(v).strip().lower() for k, v in (values or {}).items() if str(v).strip()}
        for name, value in values.items():
            kind = self.params.get(name)
            if kind is None:
                raise ToolError(f"{self.name} doesn't take {name}. It takes: {', '.join(self.params) or 'nothing'}.")
            pattern, what = KINDS[kind]
            if not pattern.fullmatch(value):
                raise ToolError(f"{name} should be {what}.")
        needed = re.findall(r"{(\w+)}", self.path)
        missing = [n for n in needed if n not in values]
        if missing:
            raise ToolError(f"{self.name} needs {', '.join(missing)}.")
        url = self.path.format(**values)
        for fragment in self.fragments:
            wanted = re.findall(r"{(\w+)}", fragment)
            if all(n in values for n in wanted):
                return url + "#" + fragment.format(**values)
        return url

    def match(self, path):
        """The params if `path` is this page (e.g. {"recipe_id": "12"}), else None."""
        names = re.findall(r"{(\w+)}", self.path)
        pattern = re.escape(self.path)
        for name in names:
            pattern = pattern.replace(re.escape("{" + name + "}"), f"(?P<{name}>[^/]+)")
        found = re.fullmatch(pattern, (path or "").rstrip("/") or "/")
        return dict(found.groupdict()) if found else None

    def line(self):
        params = ", ".join(f"{n} ({KINDS[k][1]})" for n, k in self.params.items())
        return f"- {self.name}: {self.description}" + (f" Params: {params}." if params else "")


def open_page_tool(page_lists, on_open=None, name="dieter_open_page"):
    """One tool over every app's pages. on_open(url, page) -> what the tool returns (Dieter's chat records a
    navigate action there); without it the tool returns a full link for the person to tap."""
    pages = {p.name: p for pages in page_lists for p in pages}

    def handler(args):
        page = pages.get(str(args.get("page") or ""))
        if not page:
            raise ToolError(f"There's no page called {args.get('page')!r}. Pages: {', '.join(pages)}.")
        params = args.get("params") if isinstance(args.get("params"), dict) else {}
        url = page.url(params)
        if on_open:
            return on_open(url, page)
        return {"title": page.title, "link": site_url(url)}

    what = ("Move the person's screen to this page right away" if on_open else "Get a link to this page")
    description = (f"{what}. Use it when they ask to see, open, show or pull up something; not otherwise. "
                   "Pages:\n" + "\n".join(p.line() for p in pages.values()))
    return Tool(name, "Open a page", description, handler,
                {"page": {"type": "string", "enum": list(pages)},
                 "params": {"type": "object", "description": "The page's params, e.g. {\"dish_id\": 7}."}},
                ("page",), read_only=True)
