# New Dieter App Page Process

Use this when adding a new top-level app page alongside Planner and Kitchen.
For repository boundaries and shared touchpoints, also see `docs/app_boundaries.md`.

## User Input Needed

Ask for, or infer if obvious:

- App name: for example `Planner`
- Short nav label: for example `Kitchen`
- Route prefix: for example `/apps/baking`
- Theme color: one dominant dark header color plus a light panel tint
- One-line subtitle: what the app helps the user do
- Main page purpose: what the first screen should accomplish
- Ask Dieter behavior: chat only, edit records, or both

Example request:

```text
Add a new app page called Baking Lab.
Nav label: Baking.
Theme: warm rose.
Subtitle: Plan bakes, adapt fillings, and keep timing notes.
Purpose: manage baking recipes and experiments.
Ask Dieter should be able to edit baking notes later.
```

## Implementation Checklist

1. Add an app-owned folder under `apps/<slug>/` with a `manifest.py`.
2. Register that manifest in `apps/registry.py`.
3. Add a route in `api.py`, usually under `/apps/<slug>`, or extract a router if the route group is substantial.
4. Create a template in `apps/<slug>/templates/`, usually `<slug>_home.html`.
5. Add route-scoped body class, shell header, nav links, issue area, launcher card, and route-specific `page_theme` values in the app manifest.
6. Add or update only app-local styles where possible.
7. Style the app with:
   - explicit `page_theme` values
   - dark app header bar from that theme
   - light page panels from that theme
   - slightly darker inner cells
   - matching primary buttons
   - white interiors for priority/status cells when priority colors matter
8. Make Ask Dieter routing explicit:
   - app-specific edit endpoint if it can edit data
   - contextual chat fallback if it cannot edit yet
   - ask a clarification question before editing ambiguous targets
9. Restart locally:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\restart_local_server.ps1
```

10. Verify:
   - page returns `200`
   - top nav link works
   - app bar appears only on that app
   - Ask Dieter appears and receives the current URL/title
   - dashboard/planner/kitchen styling is unchanged

## App Manifest Pattern

In `apps/<slug>/manifest.py`, define the app shell and launcher metadata:

```python
APP = {
    "id": "example",
    "title": "Example App",
    "nav_label": "Example",
    "home_url": "/apps/example",
    "route_prefixes": ("/apps/example",),
    "body_class": "example-shell-page",
    "shell_class": "app-shell-example",
    "menu_summary": "Open Example navigation",
    "menu_label": "Example navigation",
    "issue_area": "Example",
    "band_label": "Example utility navigation",
    "subtitle": "Short helpful app subtitle.",
    "home_label": "Example Home",
    "nav_items": (
        {"label": "Example Home", "url": "/apps/example"},
    ),
    "theme": {
        "header": "#12343b",
        "color": "#12343b",
        "border": "#b7dfe5",
        "panel": "#eef8fa",
        "panel_strong": "#d8f0f4",
        "text": "#155e75",
        "hover": "#d8f0f4",
        "subtitle": "#d8f0f4",
    },
    "launcher_card": {
        "class": "example-launch-card",
        "kicker": "Example",
        "title": "Example App",
        "url": "/apps/example",
        "description": "Short launcher copy.",
    },
}
```

## CSS Pattern

Use app-scoped selectors for page content so themes do not leak:

```css
.example-shell-page .container .detail-section {
    background: var(--page-theme-panel);
    border: 1px solid var(--page-theme-border);
    border-left: 5px solid var(--page-theme-color);
}
```

For repeated cards or app launcher tiles, prefer the shared card variables:

```css
.example-launch-card {
    --card-theme-color: #12343b;
    --card-theme-border: #b7dfe5;
    --card-theme-panel: #eef8fa;
    --card-theme-text: #155e75;
}
```

## Done Criteria

The new app should feel like part of Dieter, but visually distinct from Planner and Kitchen. A user should be able to tell where they are from the app bar color, nav label, and first panel without reading a manual.
