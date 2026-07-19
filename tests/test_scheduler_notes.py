import unittest

import api


class SchedulerNoteChecklistTests(unittest.TestCase):
    def test_make_checklist_preserves_single_level_items(self):
        notes = "- Brush teeth\n- Take vitamins"

        self.assertEqual(
            "- [ ] Brush teeth\n- [ ] Take vitamins",
            api.make_scheduler_notes_checklist(notes),
        )

    def test_make_checklist_uses_only_nested_leaf_bullets(self):
        notes = "\n".join(
            [
                "- Morning",
                "  - Brush teeth",
                "  - Take vitamins",
                "- Evening",
                "  - Charge phone",
                "- Standalone",
            ]
        )

        self.assertEqual(
            "\n".join(
                [
                    "- [ ] Brush teeth",
                    "- [ ] Take vitamins",
                    "- [ ] Charge phone",
                    "- [ ] Standalone",
                ]
            ),
            api.make_scheduler_notes_checklist(notes),
        )

    def test_make_checklist_uses_deepest_leaf_bullets(self):
        notes = "\n".join(
            [
                "- House",
                "  - Kitchen",
                "    - Wipe counters",
                "    - Empty dishwasher",
                "  - Trash",
                "- Errands",
                "  - Buy stamps",
            ]
        )

        self.assertEqual(
            "\n".join(
                [
                    "- [ ] Wipe counters",
                    "- [ ] Empty dishwasher",
                    "- [ ] Trash",
                    "- [ ] Buy stamps",
                ]
            ),
            api.make_scheduler_notes_checklist(notes),
        )

    def test_scheduler_synthesis_flattens_nested_checkbox_notes_to_leaves(self):
        operation = {
            "title": "Home checklist",
            "context_label": "Home",
            "scheduled_for": "",
            "notes": "- [ ] Kitchen\n  - [ ] Wipe counters\n  - [ ] Empty dishwasher\n- [ ] Take trash out",
        }

        result = api.synthesize_scheduler_operation("make a checklist for home", operation)

        self.assertEqual(
            "- [ ] Wipe counters\n- [ ] Empty dishwasher\n- [ ] Take trash out",
            result["notes"],
        )

    def test_quick_add_appends_plain_list_item(self):
        item = {"title": "Home improvement", "notes": "- paint"}

        result = api.append_scheduler_quick_add_note(item, "spackle")

        self.assertTrue(result["added"])
        self.assertEqual("- paint\n- spackle", result["notes"])
        self.assertEqual(
            {"text": "spackle", "line_index": 1, "checkable": False, "checked": False},
            result["note"],
        )

    def test_quick_add_normalizes_short_add_request(self):
        item = {"title": "Home improvement", "notes": "- paint"}

        result = api.append_scheduler_quick_add_note(item, "add spackle to this list")

        self.assertTrue(result["added"])
        self.assertEqual("- paint\n- spackle", result["notes"])

    def test_quick_add_blocks_empty_item(self):
        item = {"title": "Home improvement", "notes": "- paint"}

        result = api.append_scheduler_quick_add_note(item, "   ")

        self.assertFalse(result["added"])
        self.assertEqual("- paint", result["notes"])
        self.assertEqual({}, result["note"])

    def test_quick_add_dedupes_repeated_item(self):
        item = {"title": "Home improvement", "notes": "- paint\n- spackle"}

        result = api.append_scheduler_quick_add_note(item, "Spackle")

        self.assertFalse(result["added"])
        self.assertEqual("- paint\n- spackle", result["notes"])
        self.assertEqual("Spackle", result["note"]["text"])

    def test_quick_add_preserves_checklist_cards(self):
        item = {"title": "Home checklist", "notes": "- [ ] paint"}

        result = api.append_scheduler_quick_add_note(item, "spackle")

        self.assertTrue(result["added"])
        self.assertEqual("- [ ] paint\n- [ ] spackle", result["notes"])
        self.assertTrue(result["note"]["checkable"])
        self.assertEqual("spackle", result["note"]["text"])


if __name__ == "__main__":
    unittest.main()
