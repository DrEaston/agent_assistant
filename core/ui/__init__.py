"""Dieter design system: one stylesheet, shared Jinja macros, and the template setup apps use."""

import hashlib
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

UI_DIR = Path(__file__).parent
STATIC_DIR = UI_DIR / "static"
SHELL_DIR = UI_DIR.parent / "shell"
STATIC_URL = "/static/ui"


def _css_url():
    # The content hash in the URL lets browsers cache the file forever and still pick up changes.
    digest = hashlib.sha256((STATIC_DIR / "dieter.css").read_bytes()).hexdigest()[:10]
    return f"{STATIC_URL}/dieter.css?v={digest}"


def templates(*app_template_dirs):
    """Template environment for an app: its own folders first, then the shell and shared components."""
    env = Environment(
        loader=FileSystemLoader([str(path) for path in app_template_dirs] + [str(SHELL_DIR), str(UI_DIR)]),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["css_url"] = _css_url()
    return env
