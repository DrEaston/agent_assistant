"""Create a sanitized demo database for local portfolio review.

The generated database intentionally avoids personal data and external-service
tokens. It is safe to delete and recreate at any time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database import Database, reset_current_user_id, set_current_user_id


PBKDF2_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def copy_sanitized_trainer_workouts(db: Database, source_db_path: Path, user_id: int, limit: int = 120) -> int:
    """Copy workout metrics from a local Dieter database without raw Strava payloads or GPS traces."""
    if not source_db_path.exists():
        return 0

    source = sqlite3.connect(source_db_path)
    source.row_factory = sqlite3.Row
    try:
        rows = source.execute(
            """
            SELECT
                external_id, activity_type, workout_category, title, started_at,
                distance_meters, moving_time_seconds, elapsed_time_seconds,
                elevation_gain_meters, average_speed_mps, max_speed_mps,
                average_heartrate, max_heartrate, average_cadence, average_watts,
                kilojoules, suffer_score, perceived_exertion, gear_name
            FROM trainer_imported_workouts
            ORDER BY started_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return 0
    finally:
        source.close()

    copied = 0
    for index, row in enumerate(reversed(rows), start=1):
        title = row["title"] or f"Demo workout {index}"
        db.add_trainer_imported_workout(
            f"demo-copy-{row['external_id'] or index}",
            activity_type=row["activity_type"] or "Run",
            workout_category=row["workout_category"] or "run",
            title=title,
            started_at=row["started_at"] or "",
            distance_meters=row["distance_meters"],
            moving_time_seconds=row["moving_time_seconds"],
            elapsed_time_seconds=row["elapsed_time_seconds"],
            elevation_gain_meters=row["elevation_gain_meters"],
            average_speed_mps=row["average_speed_mps"],
            max_speed_mps=row["max_speed_mps"],
            average_heartrate=row["average_heartrate"],
            max_heartrate=row["max_heartrate"],
            average_cadence=row["average_cadence"],
            average_watts=row["average_watts"],
            kilojoules=row["kilojoules"],
            suffer_score=row["suffer_score"],
            perceived_exertion=row["perceived_exertion"],
            gear_name=row["gear_name"] or "",
            start_latlng=[],
            end_latlng=[],
            splits_metric=[],
            laps=[],
            raw={"demo_note": "Copied from local metrics only; raw Strava payload and GPS traces omitted."},
            user_id=user_id,
        )
        copied += 1
    return copied


def copy_sanitized_scheduler_items(db: Database, source_db_path: Path, limit: int = 6) -> int:
    """Copy open scheduler cards from a local database without project/action ownership links."""
    if not source_db_path.exists():
        return 0

    source = sqlite3.connect(source_db_path)
    source.row_factory = sqlite3.Row
    try:
        rows = source.execute(
            """
            SELECT title, context_label, scheduled_for, notes, status
            FROM scheduler_items
            WHERE status = 'open'
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return 0
    finally:
        source.close()

    copied = 0
    for row in reversed(rows):
        title = (row["title"] or "").strip()
        if not title:
            continue
        db.add_scheduler_item(
            title,
            context_label=row["context_label"] or "Demo",
            scheduled_for=row["scheduled_for"] or "",
            notes=row["notes"] or "",
            source="demo-copy",
        )
        copied += 1
    return copied


def copy_sanitized_grocery_lists(db: Database, source_db_path: Path, limit: int = 2) -> int:
    """Copy recent grocery lists from a local database and attach them to the demo user."""
    if not source_db_path.exists():
        return 0

    source = sqlite3.connect(source_db_path)
    source.row_factory = sqlite3.Row
    try:
        rows = source.execute(
            """
            SELECT title, items_json
            FROM recipe_grocery_lists
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except sqlite3.Error:
        return 0
    finally:
        source.close()

    copied = 0
    for row in reversed(rows):
        try:
            items = json_loads(row["items_json"], [])
        except ValueError:
            items = []
        cleaned_items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            cleaned_items.append(
                {
                    "name": item.get("name") or "",
                    "quantities": item.get("quantities") or [],
                    "notes": item.get("notes") or [],
                    "sources": item.get("sources") or [],
                    "category": item.get("category") or "",
                    "status": item.get("status") or "needed",
                }
            )
        if cleaned_items:
            db.create_recipe_grocery_list(row["title"] or "Demo grocery list", [], cleaned_items)
            copied += 1
    return copied


def json_loads(value: str, fallback):
    if not value:
        return fallback
    return json.loads(value)


def seed_curated_scheduler_items(db: Database, project_id: int) -> None:
    db.add_scheduler_item(
        "Grocery run for cinnamon rolls",
        context_label="Kitchen",
        scheduled_for="2026-07-13",
        notes="- [ ] Flour\n- [ ] Brown sugar\n- [ ] Cinnamon\n- [ ] Cream cheese\n- [ ] Powdered sugar",
        source="demo",
        project_id=project_id,
    )
    db.add_scheduler_item(
        "Review grocery list before shopping",
        context_label="Kitchen",
        scheduled_for="2026-07-16",
        notes="- Check pantry first\n- Add breakfast items\n- Mark substitutions in the list",
        source="demo",
        project_id=project_id,
    )


def seed_curated_grocery_list(db: Database) -> None:
    db.create_recipe_grocery_list(
        "Demo grocery list: cinnamon rolls and weeknight meals",
        [],
        [
            {"name": "Flour", "quantities": ["4 cups"], "notes": [], "sources": ["Overnight Cinnamon Rolls"], "category": "baking", "status": "needed"},
            {"name": "Brown Sugar", "quantities": ["1 cup"], "notes": [], "sources": ["Overnight Cinnamon Rolls"], "category": "baking", "status": "needed"},
            {"name": "Cinnamon", "quantities": ["2 tbsp"], "notes": [], "sources": ["Overnight Cinnamon Rolls"], "category": "spices", "status": "needed"},
            {"name": "Cream Cheese", "quantities": ["4 oz"], "notes": [], "sources": ["Overnight Cinnamon Rolls"], "category": "dairy", "status": "needed"},
            {"name": "Chicken Thighs", "quantities": ["1 lb"], "notes": [], "sources": ["Cumin-Lime Chicken Rice Bowls"], "category": "meat", "status": "needed"},
            {"name": "White Beans", "quantities": ["2 cans"], "notes": [], "sources": ["White Bean Tomato Stew"], "category": "pantry", "status": "needed"},
        ],
    )


def seed_curated_trainer_workouts(db: Database, user_id: int) -> None:
    workouts = [
        {
            "external_id": "demo-run-001",
            "activity_type": "Run",
            "workout_category": "run",
            "title": "Easy neighborhood run",
            "started_at": "2026-07-10T06:45:00",
            "distance_meters": 8046.72,
            "moving_time_seconds": 2700,
            "elapsed_time_seconds": 2760,
            "elevation_gain_meters": 42,
            "average_heartrate": 142,
            "perceived_exertion": 4,
        },
        {
            "external_id": "demo-run-002",
            "activity_type": "Run",
            "workout_category": "run",
            "title": "Tempo blocks",
            "started_at": "2026-07-08T06:30:00",
            "distance_meters": 9656.06,
            "moving_time_seconds": 3180,
            "elapsed_time_seconds": 3240,
            "elevation_gain_meters": 55,
            "average_heartrate": 156,
            "perceived_exertion": 7,
        },
        {
            "external_id": "demo-bike-001",
            "activity_type": "Ride",
            "workout_category": "bike",
            "title": "Zone 2 bike spin",
            "started_at": "2026-07-06T08:10:00",
            "distance_meters": 32186.88,
            "moving_time_seconds": 4500,
            "elapsed_time_seconds": 4620,
            "elevation_gain_meters": 120,
            "average_heartrate": 128,
            "average_watts": 165,
            "perceived_exertion": 3,
        },
    ]
    for workout in workouts:
        db.add_trainer_imported_workout(user_id=user_id, **workout)


def seed_demo_data(db_path: Path, force: bool = False, source_db_path: Path | None = None) -> None:
    if db_path.exists() and not force:
        raise SystemExit(f"{db_path} already exists. Use --force to replace it.")
    if db_path.exists():
        db_path.unlink()

    db = Database(str(db_path))
    db.init()

    user_id = db.create_user(
        "demo@example.com",
        "Demo User",
        hash_password("demo-password"),
        role="admin",
        status="active",
        requested_trainer_mode="athlete",
    )

    token = set_current_user_id(user_id)
    try:
        launch_id = db.add_project(
            "Portfolio launch",
            "Prepare Dieter for public review with demo data, screenshots, and tests.",
            priority_score=5,
        )
        kitchen_id = db.add_project(
            "Kitchen workflow",
            "Plan meals, turn recipe cards into structured meals, and create grocery lists.",
            priority_score=4,
        )
        training_id = db.add_project(
            "Training dashboard",
            "Track runs, shoes, reflections, and coach-visible training plans.",
            priority_score=3,
        )

        db.add_note(launch_id, "README now explains the architecture, demo mode, and safety boundaries.")
        db.add_note(kitchen_id, "Demo recipes use invented meals and no uploaded personal images.")
        db.add_note(training_id, "External integrations are optional and disabled until credentials are configured.")

        db.add_blocker(launch_id, "Need fresh screenshots before publishing the repository.", "medium")
        demo_action_id = db.add_recommended_action(launch_id, "Run the portfolio demo review checklist", "high")
        db.add_task_step(demo_action_id, "Open the guest demo and confirm the homepage explains Dieter clearly")
        db.add_task_step(demo_action_id, "Check Kitchen, Scheduler, Trainer, and Studio for obvious broken states")
        db.add_task_step(demo_action_id, "Use the demo Issues form from an app menu and confirm it is read-only")
        db.add_task_step(demo_action_id, "Review the README links to dieter.ai and the public repository")
        first_demo_step = db.get_task_steps(demo_action_id)[0]
        db.mark_task_step_complete(first_demo_step["id"])
        db.add_recommended_action(kitchen_id, "Review generated grocery list categories", "medium")
        db.add_recommended_action(training_id, "Confirm frozen Strava demo data appears in Trainer", "medium")
        db.add_weekly_goal(launch_id, "Make the repo safe and understandable for reviewers.")

        seed_curated_scheduler_items(db, kitchen_id)

        recipe_specs = [
            {
                "title": "Overnight Cinnamon Rolls",
                "ingredients": "Dough:\n4 cups flour\n1 cup warm milk\n2 eggs\n1/3 cup butter\n1/3 cup sugar\n2 1/4 tsp yeast\n\nFilling:\n1 cup brown sugar\n2 tbsp cinnamon\n1/3 cup softened butter\n\nIcing:\n4 oz cream cheese\n1 cup powdered sugar\n2 tbsp milk",
                "instructions": "Mix and knead the dough until smooth. Let rise until doubled. Roll out, spread butter, cinnamon, and brown sugar, then roll and slice. Refrigerate overnight. Bake at 350 F until golden and finish with cream cheese icing.",
                "components": [
                    {
                        "title": "Cinnamon roll dough",
                        "component_type": "bake",
                        "ingredients_text": "4 cups flour\n1 cup warm milk\n2 eggs\n1/3 cup butter\n1/3 cup sugar\n2 1/4 tsp yeast",
                        "instructions_text": "Knead until smooth and let rise until doubled.",
                    },
                    {
                        "title": "Cream cheese icing",
                        "component_type": "topping",
                        "ingredients_text": "4 oz cream cheese\n1 cup powdered sugar\n2 tbsp milk",
                        "instructions_text": "Beat until glossy and spreadable.",
                    },
                ],
            },
            {
                "title": "Lemon Herb Pasta",
                "ingredients": "12 oz pasta\n1 lemon\n2 tbsp olive oil\n1 cup greens\nSalt and pepper",
                "instructions": "Boil pasta. Toss with lemon, olive oil, greens, salt, and pepper.",
                "components": [
                    {
                        "title": "Lemon herb sauce",
                        "component_type": "sauce",
                        "ingredients_text": "1 lemon\n2 tbsp olive oil\nSalt and pepper",
                        "instructions_text": "Whisk ingredients together and adjust seasoning.",
                    },
                    {
                        "title": "Pasta base",
                        "component_type": "main",
                        "ingredients_text": "12 oz pasta\n1 cup greens",
                        "instructions_text": "Boil pasta and fold in greens during the final minute.",
                    },
                ],
            },
            {
                "title": "Cumin-Lime Chicken Rice Bowls",
                "ingredients": "1 lb chicken thighs\n1 cup rice\n1 lime\n1 tsp cumin\n1 avocado\n1 cup salsa\nCilantro",
                "instructions": "Season chicken with cumin, salt, and lime. Sear until cooked through. Serve over rice with avocado, salsa, and cilantro.",
                "components": [],
            },
            {
                "title": "White Bean Tomato Stew",
                "ingredients": "2 cans white beans\n1 can crushed tomatoes\n3 cloves garlic\n1 onion\n2 cups broth\nFeta\nGarlic toast",
                "instructions": "Cook onion and garlic, add tomatoes, beans, and broth, then simmer until thick. Serve with feta and garlic toast.",
                "components": [],
            },
        ]

        for index, spec in enumerate(recipe_specs):
            meal_id = db.create_saved_recipe_meal(
                spec["title"],
                spec["ingredients"],
                spec["instructions"],
                visibility="shared",
            )
            if spec["components"]:
                db.replace_recipe_components_for_meal(meal_id, spec["components"])
            db.set_recipe_favorite("meal", meal_id, True)
            if index < 3:
                db.add_recipe_meal_plan_item("meal", spec["title"], source_id=meal_id)

        copied_grocery_lists = copy_sanitized_grocery_lists(db, source_db_path) if source_db_path else 0
        if copied_grocery_lists == 0:
            seed_curated_grocery_list(db)

        playlist_id = db.add_playlist_draft(
            "Focused build session",
            "Demo playlist draft with unmatched tracks ready for Spotify review.",
            is_public=False,
        )
        db.add_playlist_item(playlist_id, raw_text="Talking Heads - This Must Be the Place", title="This Must Be the Place", artist="Talking Heads")
        db.add_playlist_item(playlist_id, raw_text="Robyn - Dancing On My Own", title="Dancing On My Own", artist="Robyn")

        copied = copy_sanitized_trainer_workouts(db, source_db_path, user_id) if source_db_path else 0
        if copied == 0:
            seed_curated_trainer_workouts(db, user_id)
    finally:
        reset_current_user_id(token)
        if db.conn is not None:
            db.conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a sanitized Dieter demo database.")
    parser.add_argument("--db-path", default="demo.db", help="Output SQLite path. Defaults to demo.db.")
    parser.add_argument(
        "--source-db",
        default="",
        help="Optional local Dieter database to copy sanitized Trainer workout metrics from.",
    )
    parser.add_argument("--force", action="store_true", help="Replace the output database if it already exists.")
    args = parser.parse_args()

    db_path = Path(args.db_path)
    source_db_path = Path(args.source_db) if args.source_db else None
    seed_demo_data(db_path, force=args.force, source_db_path=source_db_path)
    print(f"Seeded {db_path}")
    print("Demo login: demo@example.com / demo-password")


if __name__ == "__main__":
    main()
