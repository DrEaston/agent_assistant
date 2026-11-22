import unittest

from apps.kitchen.thanksgiving.handsfree import command, hear, spoken_prompt


class WakeWordTests(unittest.TestCase):
    def test_name_and_common_mishearings_at_the_start(self):
        for text in ("Dieter, next step", "hey Dieter next step", "okay deter next step", "Peter next step",
                     "the tour next step", "Dita, next step"):
            h = hear(text)
            self.assertTrue(h["addressed"], text)
            self.assertEqual("next step", h["rest"], text)

    def test_name_later_in_the_sentence_does_not_count(self):
        for text in ("I told Peter we eat at four", "can you ask Dieter about the pie", "we can do the next step later"):
            self.assertFalse(hear(text)["addressed"], text)

    def test_follow_up_window_needs_no_name(self):
        self.assertFalse(hear("yes")["addressed"])
        h = hear("yes", follow_up=True)
        self.assertTrue(h["addressed"])
        self.assertEqual("follow-up", h["via"])

    def test_near_misses_are_flagged_for_tuning(self):
        self.assertTrue(hear("Meter next step")["near_miss"])
        self.assertFalse(hear("dinner is at four")["near_miss"])
        self.assertFalse(hear("pass the salt")["near_miss"])


class QuickCommandTests(unittest.TestCase):
    def test_commands_must_be_the_whole_phrase(self):
        self.assertEqual({"type": "next"}, command("what's next"))
        self.assertEqual({"type": "done"}, command("mark it done"))
        self.assertEqual({"type": "quiet"}, command("stop"))
        self.assertEqual({"type": "yes"}, command("yes please"))
        self.assertEqual({"type": "no"}, command("never mind"))
        self.assertEqual({"type": "undo"}, command("undo that"))
        self.assertEqual({"type": "wake"}, command(""))
        self.assertIsNone(command("is the next step the gravy or the rolls"))
        self.assertIsNone(command("can the crust sit overnight"))

    def test_amounts_and_dishes(self):
        self.assertEqual({"type": "amount", "item": "butter"}, command("how much butter do I need"))
        self.assertEqual({"type": "amount", "item": "eggs"}, command("how many eggs"))
        self.assertEqual({"type": "goto", "dish": "mac and cheese"}, command("go to the mac and cheese"))

    def test_timers(self):
        cases = {
            "start a 12 minute timer": (12, ""),
            "set a timer for twenty five minutes": (25, ""),
            "timer for 12 minutes for the pie": (12, "pie"),
            "an hour timer": (60, ""),
            "half an hour timer": (30, ""),
            "set a timer for an hour and a half for the turkey": (90, "turkey"),
            "a minute timer": (1, ""),
        }
        for text, (minutes, label) in cases.items():
            self.assertEqual({"type": "timer", "minutes": minutes, "label": label}, command(text), text)
        self.assertIsNone(command("start a banana timer"))
        self.assertIsNone(command("set a timer for 900 minutes"))

    def test_spoken_prompt_depends_on_how_it_was_heard(self):
        self.assertIn("almost certainly for you", spoken_prompt("name"))
        self.assertIn("may be a reply to you", spoken_prompt("follow-up"))
        conversation = spoken_prompt("conversation")
        self.assertIn("\"end\": true", conversation)
        self.assertIn("{\"ignore\": true}", conversation)


if __name__ == "__main__":
    unittest.main()
