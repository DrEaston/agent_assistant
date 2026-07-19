import unittest

import api


class BakeModeVisibilityTests(unittest.TestCase):
    def test_bake_mode_requires_multiple_sectioned_parts(self):
        meals = api.prepare_recipe_complete_meals(
            [
                {
                    "title": "Simple Soup",
                    "ingredients_text": "Onion\nStock\nBeans",
                    "instructions_text": "Simmer everything together.",
                    "quality_notes_json": "[]",
                    "source_kind": "saved",
                    "status": "ready",
                    "created_at": "2026-07-01 00:00:00",
                },
                {
                    "title": "Overnight Cinnamon Rolls",
                    "ingredients_text": "Dough:\nFlour\nMilk\n\nFilling:\nBrown sugar\nCinnamon\n\nIcing:\nCream cheese\nPowdered sugar",
                    "instructions_text": "Make dough. Add filling. Ice after baking.",
                    "quality_notes_json": "[]",
                    "source_kind": "saved",
                    "status": "ready",
                    "created_at": "2026-07-01 00:00:00",
                },
            ]
        )

        self.assertFalse(meals[0]["show_bake_mode"])
        self.assertTrue(meals[1]["show_bake_mode"])

    def test_featured_meals_prefers_cinnamon_couscous_meat_and_one_soup(self):
        meals = [
            {"id": 1, "title": "Hearty Chicken Sausage & Kale Soup"},
            {"id": 2, "title": "Chicken Sausage and Couscous Soup"},
            {"id": 3, "title": "Greek Taverna Pork Chops with Cucumber-Tomato Couscous Salad"},
            {"id": 4, "title": "Overnight Cinnamon Rolls"},
            {"id": 5, "title": "Lemon Herb Pasta"},
        ]

        featured = api.featured_recipe_meals(meals)

        self.assertEqual(
            [
                "Overnight Cinnamon Rolls",
                "Greek Taverna Pork Chops with Cucumber-Tomato Couscous Salad",
                "Hearty Chicken Sausage & Kale Soup",
            ],
            [meal["title"] for meal in featured],
        )

    def test_icing_section_requires_actual_icing_signal(self):
        sections = api.parse_baking_ingredient_sections(
            "Flour\nMilk\nButter\nVanilla\nEggs\nBrown sugar\nCinnamon"
        )

        self.assertNotIn("icing", [section["key"] for section in sections])

    def test_explicit_icing_heading_still_displays_icing_section(self):
        sections = api.parse_baking_ingredient_sections(
            "Dough:\nFlour\nMilk\n\nIcing:\nCream cheese\nPowdered sugar"
        )

        self.assertIn("icing", [section["key"] for section in sections])

    def test_powdered_sugar_can_create_icing_section(self):
        sections = api.parse_baking_ingredient_sections(
            "Flour\nMilk\nPowdered sugar\nCinnamon"
        )

        self.assertIn("icing", [section["key"] for section in sections])


if __name__ == "__main__":
    unittest.main()
