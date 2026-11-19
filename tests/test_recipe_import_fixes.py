import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from starlette.datastructures import Headers, UploadFile

import api
from apps.kitchen import import_images
from apps.kitchen.import_images import UnsupportedRecipeImage, normalize_recipe_image
from database import Database, reset_current_user_id, set_current_user_id
from recipe_ocr_service import RecipeOCRService


def png_bytes(size=(3000, 1500), color="orange"):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


class TempDbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.tmp.name) / "test.db"))
        self.db.init()

    def tearDown(self):
        if self.db.conn is not None:
            self.db.conn.close()
        self.tmp.cleanup()

    def insert_meal(self, title, ingredients, instructions, user_id=1, group_id=None):
        cursor = self.db.conn.cursor()
        if group_id is None:
            group_id = -(cursor.execute("SELECT COUNT(*) FROM recipe_complete_meals").fetchone()[0] + 1)
        cursor.execute(
            """INSERT INTO recipe_complete_meals
               (source_group_id, source_kind, title, ingredients_text, instructions_text, status, user_id)
               VALUES (?, 'saved', ?, ?, ?, 'ready', ?)""",
            (group_id, title, ingredients, instructions, user_id),
        )
        self.db.conn.commit()
        return cursor.lastrowid

    def meal_ids(self):
        return [row[0] for row in self.db.conn.execute("SELECT id FROM recipe_complete_meals ORDER BY id")]


class ImageNormalizationTests(unittest.TestCase):
    def test_large_image_is_resized_to_jpeg(self):
        data, ext = normalize_recipe_image(png_bytes(), "card.png", "image/png")
        self.assertEqual(ext, ".jpg")
        image = Image.open(io.BytesIO(data))
        self.assertEqual(image.format, "JPEG")
        self.assertLessEqual(max(image.size), import_images.MAX_EDGE)

    def test_unreadable_file_is_rejected_with_message(self):
        with self.assertRaises(UnsupportedRecipeImage):
            normalize_recipe_image(b"not an image", "notes.jpg", "image/jpeg")

    def test_heic_without_decoder_gets_clear_message(self):
        with patch.object(import_images, "HEIC_SUPPORTED", False):
            with self.assertRaises(UnsupportedRecipeImage) as ctx:
                normalize_recipe_image(b"...", "IMG_0001.HEIC", "image/heic")
        self.assertIn("JPEG", str(ctx.exception))


class ExtractionDetailsTests(unittest.TestCase):
    def test_details_are_parsed_and_bounded(self):
        details = RecipeOCRService._normalize_details({
            "servings": "8 to 10",
            "prep_minutes": "20 minutes",
            "cook_minutes": 75,
            "oven_temp_f": "350°F",
            "total_minutes": None,
            "make_ahead": "Can be made 2 days ahead.",
        })
        self.assertEqual(details["servings"], "8 to 10")
        self.assertEqual(details["prep_minutes"], 20)
        self.assertEqual(details["cook_minutes"], 75)
        self.assertEqual(details["oven_temp_f"], 350)
        self.assertIsNone(details["total_minutes"])

    def test_implausible_values_are_dropped(self):
        details = RecipeOCRService._normalize_details({"oven_temp_f": 20, "cook_minutes": True})
        self.assertIsNone(details["oven_temp_f"])
        self.assertIsNone(details["cook_minutes"])

    def test_missing_details_become_empty(self):
        payload = RecipeOCRService._normalize_payload({"title": "Stuffing", "sections": []})
        self.assertIsNone(payload["details"]["cook_minutes"])
        self.assertEqual(payload["details"]["servings"], "")

    def test_single_photo_prompt_does_not_assume_card(self):
        prompt = RecipeOCRService._build_prompt({"label": "Grandma pie", "layout": "single", "images": []})
        self.assertIn("ONE recipe", prompt)
        self.assertNotIn("back image", prompt)


class QualityNotesTests(unittest.TestCase):
    def group(self, layout):
        return {
            "layout": layout,
            "extraction_status": "extracted",
            "extraction_error": "",
            "ingredients_text": "1 cup flour",
            "instructions_text": "Mix.",
        }

    def test_single_photo_recipe_is_ready(self):
        self.assertEqual(Database._build_complete_meal_quality_notes(self.group("single"), {"front"}), [])
        self.assertEqual(Database._build_complete_meal_quality_notes(self.group("pages"), {"front", "extra"}), [])

    def test_card_without_back_still_needs_review(self):
        notes = Database._build_complete_meal_quality_notes(self.group("front_back"), {"front"})
        self.assertTrue(any("back" in note for note in notes))


class DuplicateCleanupTests(TempDbTestCase):
    def test_same_title_different_recipes_are_both_kept(self):
        self.insert_meal("Stuffing", "bread\nsage", "Bake.")
        self.insert_meal("Stuffing", "cornbread\nsausage", "Bake longer.")
        result = self.db.cleanup_duplicate_recipes()
        self.assertEqual(result["deleted_meals"], 0)
        self.assertEqual(len(self.meal_ids()), 2)

    def test_exact_copy_is_removed(self):
        self.insert_meal("Stuffing", "bread\nsage", "Bake.")
        self.insert_meal("stuffing", "bread  sage", "Bake")
        result = self.db.cleanup_duplicate_recipes()
        self.assertEqual(result["deleted_meals"], 1)
        self.assertEqual(len(self.meal_ids()), 1)

    def test_copies_owned_by_different_users_are_kept(self):
        self.insert_meal("Stuffing", "bread\nsage", "Bake.", user_id=1)
        self.insert_meal("Stuffing", "bread\nsage", "Bake.", user_id=2)
        self.assertEqual(self.db.cleanup_duplicate_recipes()["deleted_meals"], 0)


class UploadModeTests(TempDbTestCase):
    def setUp(self):
        super().setUp()
        self.token = set_current_user_id(None)
        self.project_id = self.db.add_project("Kitchen")
        self.action_id = self.db.add_recommended_action(self.project_id, "Import the first batch of recipe images")
        self.uploads = Path(self.tmp.name) / "uploads"
        self.uploads.mkdir()
        self.patches = [
            patch.object(api, "db", self.db),
            patch.object(api, "recipe_uploads_dir", self.uploads),
            patch.object(api.cloud_persistence, "sync_upload_file", lambda *_: None),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        reset_current_user_id(self.token)
        super().tearDown()

    def upload(self, mode, files):
        uploads = [
            UploadFile(io.BytesIO(data), filename=name, headers=Headers({"content-type": ctype}))
            for name, data, ctype in files
        ]
        return asyncio.run(api.upload_recipe_images_form(self.project_id, self.action_id, uploads, mode))

    def groups(self):
        return self.db.get_recipe_image_groups(self.action_id)

    def test_single_mode_makes_one_recipe_per_photo(self):
        self.upload("single", [("pie.png", png_bytes(), "image/png"), ("rolls.png", png_bytes(), "image/png")])
        groups = self.groups()
        self.assertEqual(len(groups), 2)
        self.assertEqual({g["layout"] for g in groups}, {"single"})
        self.assertEqual({g["label"] for g in groups}, {"pie", "rolls"})

    def test_pages_mode_makes_one_recipe(self):
        self.upload("pages", [("p1.png", png_bytes(), "image/png"), ("p2.png", png_bytes(), "image/png")])
        groups = self.groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual([img["side"] for img in groups[0]["images"]], ["front", "extra"])

    def test_cards_mode_pairs_front_and_back(self):
        self.upload("cards", [("a.png", png_bytes(), "image/png"), ("b.png", png_bytes(), "image/png")])
        groups = self.groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(sorted(img["side"] for img in groups[0]["images"]), ["back", "front"])

    def test_bad_file_is_reported_and_creates_no_recipe(self):
        response = self.upload("single", [("broken.jpg", b"nope", "image/jpeg")])
        self.assertEqual(self.groups(), [])
        self.assertIn("upload_error=", response.headers["location"])

    def test_stored_photo_is_downsized_jpeg(self):
        self.upload("single", [("big.png", png_bytes((5000, 4000)), "image/png")])
        stored = list(self.uploads.iterdir())
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].suffix, ".jpg")
        with Image.open(stored[0]) as image:
            self.assertLessEqual(max(image.size), import_images.MAX_EDGE)


class ExtractionStorageTests(TempDbTestCase):
    def test_details_json_round_trips(self):
        project_id = self.db.add_project("Kitchen")
        action_id = self.db.add_recommended_action(project_id, "Import")
        group_id = self.db.create_recipe_image_group(project_id, action_id, "Pie", layout="single")
        details = {"cook_minutes": 55, "oven_temp_f": 350}
        self.db.upsert_recipe_extraction(group_id, "extracted", "a", "b", "[]", "", "", json.dumps(details))
        group = self.db.get_recipe_image_groups(action_id)[0]
        self.assertEqual(json.loads(group["details_json"]), details)


if __name__ == "__main__":
    unittest.main()
