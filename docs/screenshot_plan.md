# Dieter Screenshot Plan

Use the local demo database and capture these screenshots on desktop.

## Start Demo

```powershell
python scripts\seed_demo_data.py --force --source-db projects.db
$env:DB_PATH="demo.db"
$env:DEMO_MODE="1"
python -m uvicorn api:app --reload
```

Open `http://localhost:8000/login`, then use guest mode.

## Capture List

1. `launcher.png`
   - URL: `http://localhost:8000/apps`
   - Show Dieter as a modular multi-app platform.

2. `kitchen-recipes.png`
   - URL: `http://localhost:8000/apps/recipes`
   - Show the recipe library, ideally with Overnight Cinnamon Rolls visible.

3. `kitchen-grocery-list.png`
   - URL: `http://localhost:8000/apps/recipes/grocery-lists`
   - Open a grocery list if one looks better than the list index.

4. `scheduler.png`
   - URL: `http://localhost:8000/apps/assistant/scheduler`
   - Show checklist-style agenda items such as the mechanic card.

5. `studio-pipeline.png`
   - URL: `http://localhost:8000/apps/issues`
   - Show Studio as the feedback-to-Codex development console.

6. `trainer.png`
   - URL: `http://localhost:8000/apps/trainer`
   - Show the Trainer direction with demo workout data.

## Notes

- Prefer desktop screenshots.
- Keep the demo banner visible; it reassures reviewers that the public preview is read-only.
- Avoid screenshots from your private `projects.db` unless the content is safe to publish.
- After dropping screenshots into `docs/screenshots/`, update the README to embed them.
