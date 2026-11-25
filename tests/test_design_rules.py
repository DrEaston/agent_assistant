"""Guards that keep page formatting from drifting.

New-style app templates (those built on core/shell/layout.html) may only use the shared look:
no <style> blocks, no style="" attributes, no hex colors. App CSS files may only use var(--…) colors.
App folders may not import each other.
"""

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APPS = ROOT / "apps"
CSS = ROOT / "core" / "ui" / "static" / "dieter.css"

HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
STYLE_BLOCK = re.compile(r"<style\b", re.I)
STYLE_ATTR = re.compile(r"\sstyle\s*=", re.I)


def new_style_templates():
    # Apps' pages, and shared pages that live in core/ (the Logbook, the connector's sign-in).
    for path in [*APPS.rglob("templates/*.html"), *(ROOT / "core").rglob("templates/*.html")]:
        text = path.read_text(encoding="utf-8")
        if '{% extends "layout.html" %}' in text:
            yield path, text


def without_scripts(text):
    return re.sub(r"<script\b.*?</script>", "", text, flags=re.S | re.I)


class DesignRuleTests(unittest.TestCase):
    def test_shopping_lists_use_the_shared_layout(self):
        names = {path.name for path, _ in new_style_templates()}
        self.assertTrue({"shop_index.html", "shop_new.html", "shop_list.html"} <= names)

    def test_app_templates_have_no_local_styling(self):
        problems = []
        for path, text in new_style_templates():
            markup = without_scripts(text)
            rel = path.relative_to(ROOT)
            if STYLE_BLOCK.search(markup):
                problems.append(f"{rel}: <style> block")
            if STYLE_ATTR.search(markup):
                problems.append(f"{rel}: style=\"\" attribute")
            for match in HEX.finditer(markup):
                problems.append(f"{rel}: hex color {match.group()}")
        self.assertEqual([], problems, "Use classes from core/ui/static/dieter.css instead.")

    def test_app_css_uses_only_tokens(self):
        problems = []
        for path in APPS.rglob("*.css"):
            for match in HEX.finditer(path.read_text(encoding="utf-8")):
                problems.append(f"{path.relative_to(ROOT)}: {match.group()}")
        self.assertEqual([], problems, "App CSS may only use var(--…) colors.")

    def test_every_app_accent_is_defined(self):
        css = CSS.read_text(encoding="utf-8")
        for manifest in APPS.glob("*/manifest.py"):
            text = manifest.read_text(encoding="utf-8")
            found = re.search(r'"accent":\s*"([a-z]+)"', text)
            if found:
                self.assertIn(f'[data-accent="{found.group(1)}"]', css, manifest)

    def test_apps_do_not_import_each_other(self):
        app_names = {p.name for p in APPS.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
        problems = []
        for path in APPS.rglob("*.py"):
            owner = path.relative_to(APPS).parts[0]
            if owner not in app_names:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    modules = [node.module]
                for module in modules:
                    parts = module.split(".")
                    if parts[0] == "apps" and len(parts) > 1 and parts[1] in app_names and parts[1] != owner:
                        problems.append(f"{path.relative_to(ROOT)} imports {module}")
        self.assertEqual([], problems)


if __name__ == "__main__":
    unittest.main()
