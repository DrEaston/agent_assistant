"""Registry for Dieter app-owned navigation and shell metadata."""

from copy import deepcopy

from apps.assistant.manifest import APP as ASSISTANT_APP
from apps.issues.manifest import APP as ISSUES_APP
from apps.kitchen.manifest import APP as KITCHEN_APP
from apps.launcher.manifest import APP as LAUNCHER_APP
from apps.music.manifest import APP as MUSIC_APP
from apps.trainer.manifest import APP as TRAINER_APP


CCT_ROADMAP_APP = {
    "id": "cct-roadmap",
    "title": "CCT Roadmap",
    "nav_label": "CCT Roadmap",
    "home_url": "/cct-roadmap",
    "exact_paths": ("/cct-roadmap", "/public/cct-interview-questions"),
    "body_class": "planner-shell-page",
    "shell_class": "app-shell-planner",
    "top_class": "",
    "actions_class": "assistant-shell-actions",
    "chat_button_class": "app-shell-chat-button",
    "show_chat_button": False,
    "show_issue_item": False,
    "menu_class": "",
    "icon_class": "",
    "menu_summary": "Open CCT roadmap navigation",
    "menu_label": "CCT roadmap navigation",
    "issue_area": "",
    "band_label": "CCT roadmap utility navigation",
    "subtitle": "Technical brief, fit notes, and interview check-ins.",
    "home_label": "Roadmap",
    "nav_items": (
        {"label": "Roadmap", "url": "/cct-roadmap"},
        {"label": "Fit Agent", "url": "#cct-fit-agent"},
        {"label": "Product Map", "url": "#cct-product-map"},
        {"label": "Questions", "url": "#cct-questions"},
        {"label": "How Built", "url": "#cct-page-agent"},
    ),
    "theme": {
        "header": "#083344",
        "color": "#164e63",
        "border": "#a5f3fc",
        "panel": "#ecfeff",
        "panel_strong": "#cffafe",
        "text": "#155e75",
        "hover": "#cffafe",
        "subtitle": "#cffafe",
    },
}

APP_MANIFESTS = [
    KITCHEN_APP,
    ISSUES_APP,
    ASSISTANT_APP,
    TRAINER_APP,
    MUSIC_APP,
    CCT_ROADMAP_APP,
    LAUNCHER_APP,
]

GLOBAL_NAV_APPS = [
    ASSISTANT_APP,
    KITCHEN_APP,
    TRAINER_APP,
    MUSIC_APP,
]


def _path_matches(path, manifest):
    prefixes = manifest.get("route_prefixes", ())
    exact_paths = manifest.get("exact_paths", ())
    return path in exact_paths or any(path.startswith(prefix) for prefix in prefixes)


def _visible_nav_items(manifest, context):
    nav_items = []
    trainer_mode = context.get("trainer_mode")
    recipe_app = context.get("recipe_app") or {}
    for item in manifest.get("nav_items", ()):
        mode_is = item.get("trainer_mode_is")
        mode_not = item.get("trainer_mode_not")
        if mode_is and trainer_mode != mode_is:
            continue
        if mode_not and trainer_mode == mode_not:
            continue
        if item.get("recipe_import_url"):
            import_url = _get_value(recipe_app, "import_url")
            if not import_url:
                continue
            resolved = dict(item)
            resolved["url"] = import_url
            nav_items.append(resolved)
            continue
        nav_items.append(item)
    return nav_items


def _get_value(value, key, default=None):
    if hasattr(value, "get"):
        return value.get(key, default)
    return getattr(value, key, default)


def app_shell_for_path(path, context=None):
    """Return the app shell manifest for a request path."""
    context = context or {}
    for manifest in APP_MANIFESTS:
        if _path_matches(path, manifest):
            shell = deepcopy(manifest)
            shell["nav_items"] = _visible_nav_items(manifest, context)
            return shell
    return None


def global_nav_apps():
    """Return stable top-level navigation items."""
    return [
        {
            "label": app["nav_label"],
            "url": app["home_url"],
        }
        for app in GLOBAL_NAV_APPS
    ]


def launcher_cards(recipe_app=None, planner=None):
    """Return cards for the /apps launcher."""
    recipe_app = recipe_app or {}
    planner = planner or {}
    cards = []
    for app in GLOBAL_NAV_APPS:
        card = deepcopy(app["launcher_card"])
        if app["id"] == "kitchen" and not recipe_app.get("project"):
            card["url"] = ""
            card["unavailable"] = True
            card["description"] = "No recipe app data is available yet."
        if app["id"] == "assistant" and planner.get("next_action"):
            card["description"] = f"Next: {planner['next_action']['action']}"
        cards.append(card)
    return cards
