# New Dieter App Page Process

Use this when adding a new top-level app page alongside Planner and Kitchen.

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

1. Add a route in `api.py`, usually under `/apps/<slug>`.
2. Create a template in `templates/`, usually `<slug>_home.html`.
3. Add a route-scoped body class in `templates/base.html`.
4. Add the standard `app-shell` header and route-specific `page_theme` values.
5. Add or update the top nav link.
6. Style the app with:
   - explicit `page_theme` values
   - dark app header bar from that theme
   - light page panels from that theme
   - slightly darker inner cells
   - matching primary buttons
   - white interiors for priority/status cells when priority colors matter
7. Make Ask Dieter routing explicit:
   - app-specific edit endpoint if it can edit data
   - contextual chat fallback if it cannot edit yet
   - ask a clarification question before editing ambiguous targets
8. Restart locally:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\restart_local_server.ps1
```

9. Verify:
   - page returns `200`
   - top nav link works
   - app bar appears only on that app
   - Ask Dieter appears and receives the current URL/title
   - dashboard/planner/kitchen styling is unchanged

## Base Template Pattern

In `templates/base.html`, add a route flag:

```jinja2
{% set show_example_shell = request is defined and request.url.path.startswith("/apps/example") %}
```

Add the body class:

```jinja2
<body class="{% if show_example_shell %} example-shell-page{% endif %}">
```

Add explicit `page_theme` values near the existing route theme block. The body writes these values into shared CSS variables, and the shell, navigation band, cards, buttons, and page panels inherit from them.

```jinja2
{% set page_theme = {
    "color": "#12343b",
    "border": "#b7dfe5",
    "panel": "#eef8fa",
    "panel_strong": "#d8f0f4",
    "text": "#155e75",
    "hover": "#d8f0f4",
    "subtitle": "#d8f0f4"
} %}
```

Add the app bar:

```jinja2
{% if show_example_shell %}
    <section class="app-shell app-shell-example">
        <div class="app-shell-top">
            <div>
                <h1><a href="/">Example App</a></h1>
                <p>Short helpful app subtitle.</p>
            </div>
            <div class="app-shell-actions">
                <button type="button" data-recipe-chat-open>Ask Dieter</button>
                <details class="app-menu">
                    <summary aria-label="Open Example navigation">
                        <span class="app-menu-icon" aria-hidden="true">
                            <span></span>
                            <span></span>
                            <span></span>
                        </span>
                    </summary>
                    <div class="app-nav" aria-label="Example navigation">
                        <a href="/apps/example">Example Home</a>
                    </div>
                </details>
            </div>
        </div>
        <div class="app-shell-band app-nav">
            <a href="/">Home</a>
            <form method="post" action="/logout" class="inline-form">
                <button type="submit">Log Out</button>
            </form>
        </div>
    </section>
{% endif %}
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
