import unittest

from apps.kitchen.ingredients import combine, is_not_bought, parse_line, split_line


def total(*lines, buy=True):
    return combine([parse_line(p) for line in lines for p in split_line(line)], buy=buy)


class ParseTests(unittest.TestCase):
    def test_amounts_units_and_items(self):
        p = parse_line("1 1/2 sticks butter")
        self.assertEqual((p["qty"], p["unit"], p["item"]), (1.5, "stick", "butter"))
        p = parse_line("4 to 6 tbsp ice water")
        self.assertEqual((p["qty"], p["unit"]), (6, "tbsp"))  # shop for the top of a range
        p = parse_line("1 package (12 to 14 oz) Little Smokies")
        self.assertEqual((p["qty"], p["unit"], p["item"]), (1, "package", "smokies"))
        p = parse_line("2 large onions")
        self.assertEqual((p["qty"], p["unit"], p["item"]), (2, "each", "onion"))
        p = parse_line("4 celery ribs")
        self.assertEqual((p["unit"], p["item"]), ("rib", "celery"))
        self.assertEqual(parse_line("1½ cups milk")["qty"], 1.5)
        self.assertIsNone(parse_line("kosher salt")["qty"])

    def test_specific_items_win(self):
        self.assertEqual(parse_line("12 oz can evaporated milk")["item"], "evaporated-milk")
        self.assertEqual(parse_line("2 oz cream cheese")["item"], "cream-cheese")
        self.assertEqual(parse_line("1/2 tsp garlic powder")["item"], "garlic-powder")
        self.assertEqual(parse_line("2 tsp pumpkin pie spice")["item"], "pumpkin-spice")
        self.assertEqual(parse_line("1 1/3 cups crispy fried onions")["item"], "fried-onions")
        self.assertEqual(parse_line("1 1/2 cups low-sodium chicken or turkey broth")["item"], "broth")

    def test_split_and_skip(self):
        self.assertEqual(split_line("1/2 onion and 1/2 lemon for the cavity"), ["1/2 onion", "1/2 lemon for the cavity"])
        self.assertEqual(len(split_line("kosher salt and black pepper")), 1)
        self.assertTrue(is_not_bought("turkey pan drippings"))
        self.assertTrue(is_not_bought("90 g active starter"))
        self.assertTrue(is_not_bought("350 g water"))
        self.assertFalse(is_not_bought("low-sodium broth to make 3 to 4 cups liquid with the drippings"))
        self.assertFalse(is_not_bought("1 cup watercress"))


class CombineTests(unittest.TestCase):
    def test_butter_in_any_unit(self):
        self.assertEqual(total("4 tbsp butter", "1 stick (113 g) butter", "1 1/2 sticks butter"), "3 sticks")
        self.assertEqual(total("1 lb butter", "113 g butter"), "5 sticks (about 1 1/4 lb)")
        self.assertEqual(total("2 tbsp butter"), "2 tbsp")

    def test_volume_and_weight(self):
        self.assertEqual(total("1 1/2 cups broth", "2 1/2 cups broth"), "1 quart (4 cups)")
        self.assertEqual(total("1 tsp sugar", "2 tsp sugar"), "1 tbsp")
        self.assertEqual(total("8 oz cheddar", "12 oz cheddar"), "1 1/4 lb")
        self.assertEqual(total("440 g flour", "1/4 cup flour"), "470 g (about 1 lb)")

    def test_mixed_and_unmeasured(self):
        self.assertEqual(total("1 can broth", "2 cups broth"), "1 can + 2 cups")
        self.assertEqual(total("2 cups broth", "broth to taste"), "2 cups + some")
        self.assertEqual(total("whipped cream"), "")
        self.assertEqual(total("1/2 onion", "1 onion"), "2")
        self.assertEqual(total("1/2 onion", "1 onion", buy=False), "1 1/2")


if __name__ == "__main__":
    unittest.main()


class RecipeCardTests(unittest.TestCase):
    def test_amount_at_the_end_uses_the_two_serving_amount(self):
        p = parse_line("Potatoes (12 oz | 24 oz)")
        self.assertEqual((p["qty"], p["unit"], p["item"]), (12, "oz", "potato"))
        p = parse_line("Garlic (2 cloves | 4 cloves)")
        self.assertEqual((p["qty"], p["unit"], p["item"]), (2, "clove", "garlic"))
        self.assertEqual(parse_line("Butter (1 TBSP | 2 TBSP)")["unit"], "tbsp")
        self.assertEqual(parse_line("Broccoli (1 | 2)")["qty"], 1)
        self.assertIsNone(parse_line("Kosher Salt")["qty"])
        # A note in parentheses is not an amount, and leading amounts win.
        self.assertIsNone(parse_line("Lemon (for serving)")["qty"])
        self.assertEqual(parse_line("1 stick (113 g) butter")["unit"], "stick")

    def test_card_notes_are_skipped(self):
        self.assertTrue(is_not_bought("Chicken Breasts can be substituted for pork chops."))
        self.assertFalse(is_not_bought("Chicken Stock Concentrate (1 | 2)"))
        self.assertEqual(total("Potatoes (12 oz | 24 oz)", "Potatoes (12 oz | 24 oz)"), "1 1/2 lb")
