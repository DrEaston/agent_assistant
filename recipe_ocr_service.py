"""Recipe image OCR and structured extraction service."""

import base64
import json
import mimetypes
import os
from pathlib import Path


RECIPE_EXTRACTION_PROMPT = """You are extracting recipes from uploaded recipe card images.

The front image usually contains ingredients. The back image usually contains
instructions. Extra pages may contain overflow, side dishes, notes, or variants.

Return only JSON with this shape:
{
  "title": "",
  "ingredients_text": "",
  "instructions_text": "",
  "sections": [
    {
      "title": "",
      "type": "main|side|sauce|component|note|unknown",
      "ingredients": [],
      "instructions": []
    }
  ],
  "uncertain": []
}

Title rules:
- "title" must be the visible or best inferred recipe name, not a category.
- Good titles look like "Jammin' Fig Pork Chops with Broccoli and Crispy Potatoes".
- Bad titles are generic labels like "Main", "Side", "Recipe", "Dinner", or "Dish".
- If no exact title is visible, infer a concise title from the main ingredients
  and add an uncertainty note. Do not leave it as "Main".

Section rules:
- The primary/main section title should usually match the recipe title.
- If the card contains side dishes, sauces, toppings, or components, split those
  into separate sections with specific names like "Crispy Potatoes", not "Side".
- Keep section "type" categorical, but keep section "title" human-readable.

Text rules:
- Preserve wording when possible.
- The front image usually contains ingredients.
- The back image usually contains instructions.
- If text is unclear, put a short note in "uncertain" rather than inventing
  exact wording.
"""

PDF_RECIPE_EXTRACTION_NOTE = """This import came from a recipe PDF.

Use the extracted PDF text as the primary source for title, ingredients, and
instructions. Use the accompanying image/photo only as visual context, thumbnail
context, or to resolve ambiguity. Do not assume front/back recipe card layout.
"""

GENERIC_RECIPE_TITLES = {
    "",
    "main",
    "side",
    "sides",
    "recipe",
    "recipes",
    "dish",
    "dinner",
    "meal",
    "entree",
    "entrée",
    "component",
    "unknown",
}

RECIPE_COMPONENT_PROMPT = """You split complete meal recipes into reusable meal components.

Input will include a complete meal title, full ingredients text, and full steps.

Return only JSON with this shape:
{
  "components": [
    {
      "title": "",
      "component_type": "meat|carb|vegetable|sauce|soup|other",
      "ingredients": [],
      "instructions": []
    }
  ]
}

Rules:
- Components can be meat, carb, vegetable, sauce, soup, or other.
- Use soup for brothy, creamy, blended, or stew-like components that are served
  in a bowl as a soup rather than as a sauce or plated side.
- Extract every distinct component that could stand alone as a side/main/sauce/soup.
- Keep ingredients needed for that component.
- Keep only instructions directly related to preparing that component.
- Rewrite/reduce instructions so each component can be followed on its own.
- Exclude unrelated prep, cooking, plating, and assembly steps for other
  components. For example, sauce instructions must not mention preparing pork,
  potatoes, vegetables, or the finished plate unless that action is truly part
  of making the sauce.
- If a source step combines several components, split it and keep only the
  clause or sentence needed by this component.
- Prefer a short, incomplete-looking component instruction over a complete
  meal instruction polluted with unrelated work.
- Use specific titles like "Crispy Potatoes", "Broccoli", "Fig Sauce", or
  "Pork Chops", not generic titles like "Side" or "Main".
- If a complete meal has no separable component, return the main prepared item
  as one component with the best type.
"""

RECIPE_BAKE_MODE_CLEANUP_PROMPT = """You clean up extracted recipe text for a kitchen Bake Mode interface.

Input is already OCR-extracted recipe JSON. Do one careful editorial pass.

Return only JSON with this shape:
{
  "title": "",
  "ingredients_text": "",
  "instructions_text": "",
  "sections": [
    {
      "title": "",
      "type": "main|side|sauce|component|note|unknown",
      "ingredients": [],
      "instructions": []
    }
  ],
  "uncertain": []
}

Rules:
- Do not invent ingredients, amounts, times, or temperatures.
- Preserve the recipe's meaning and visible wording when possible.
- Fix obvious OCR errors only when context is clear.
- Break ingredients into practical prep sections when the recipe supports it.
  Examples: Dough, Filling, Icing; Crust, Pesto, Chicken, Toppings; Sauce,
  Potatoes, Pork Chops, Vegetables.
- Use recipe-specific section names. Do not force everything into dough,
  filling, and icing.
- Format ingredients_text with section headings when sections exist:
  "Crust:\n- ...\n\nChicken:\n- ..."
- Format instructions_text as concise steps. If a recipe has clear component
  phases, use matching headings so later Bake Mode parsing can attach the right
  steps to the right ingredient section.
- If a step combines multiple components, split it only when the source clearly
  supports the split.
- Keep uncertain notes short and explicit.
"""

RECIPE_INGREDIENT_AMOUNT_PROMPT = """You convert recipe component ingredients into measurable grocery quantities.

You may receive:
- The selected component title and type.
- The component's current ingredient lines.
- The complete meal's original extracted ingredients and steps.
- Original recipe card images, when available.

Return only JSON with this shape:
{
  "ingredients": [
    {
      "name": "",
      "amount": "",
      "unit": "",
      "preparation": "",
      "source_text": "",
      "purchase_note": "",
      "confidence": "high|medium|low"
    }
  ],
  "notes": []
}

Rules:
- Prefer exact amounts visible in the original recipe card images or extracted
  full meal ingredients.
- Convert vague component lines into measurable units when the source supports
  it, such as "2 cloves" garlic or "1 TBSP" mustard.
- Never return an amount with an empty unit for liquids, condiments, jams,
  sauces, vinegars, stock concentrates, or packet-style sauce ingredients. If
  the card only shows a count like "(1|2)", infer a practical kitchen measure.
- For typical two-serving sauce packet counts, use tablespoon estimates unless
  the card gives a better amount. Examples: "Balsamic Vinegar (1|2)" -> amount
  "1", unit "Tbsp"; "Cherry Jam (1|2)" -> amount "1", unit "Tbsp"; "Soy Sauce
  (1|2)" -> amount "1", unit "Tbsp"; "Chicken Stock Concentrate (1|2)" ->
  amount "1", unit "Tbsp".
- Use grocery-friendly units. If the amount is one whole onion, return
  amount "1", unit "onion", preparation "diced"; do not use unit "piece".
- For whole produce, use the produce item as the unit when appropriate, like
  "1 lemon" or "1 zucchini".
- For concentrated stock portions, convert to a practical kitchen measure when
  possible. For example, three veggie stock concentrate portions for a
  two-serving recipe should usually become amount "3", unit "Tbsp", name
  "vegetable stock concentrate"; do not use unit "concentrates".
- When the source is ambiguous but culinary context strongly suggests a
  reasonable amount, provide a practical estimate instead of leaving the amount
  blank. Mark confidence as "medium" or "low". For example, if a sauce ingredient says "1 or 2 balsamic
  vinegar", infer a likely tablespoon/teaspoon/ounce amount from the visible
  card context, serving size, and sauce instructions.
- If the source says a packet, preserve that in source_text and estimate a
  practical kitchen quantity only when the card gives enough context.
- If a packet or package cannot be converted honestly, keep unit as "packet" or
  "package".
- Do not put card provenance, serving comparisons, or source explanations in
  purchase_note. Avoid text like "the card shows", "for 2 servings", "for 4
  servings", or "Source:".
- Use purchase_note only for short shopping guidance that changes what the user
  should buy, such as "can size not specified" or "brand packet size may vary".
- Keep names grocery-friendly and singular where reasonable, like "garlic",
  "Dijon mustard", "pork chops", or "broccoli".
- Include only ingredients used by this component, not every ingredient in the
  complete meal.
"""


class RecipeOCRService:
    """Use a configured vision model to extract structured recipe text."""

    def __init__(self, uploads_dir):
        self.uploads_dir = Path(uploads_dir)
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.model = os.getenv("RECIPE_OCR_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
        self.cleanup_model = os.getenv("RECIPE_CLEANUP_MODEL", os.getenv("OPENAI_REVIEW_MODEL", "")).strip()

    @property
    def available(self):
        return bool(self.api_key)

    def extract_group(self, group):
        """Extract recipe text from a grouped set of uploaded images."""
        if not self.available:
            return {
                "status": "error",
                "error": "OPENAI_API_KEY is not configured, so OCR cannot run yet.",
                "ingredients_text": "",
                "instructions_text": "",
                "sections_json": "[]",
                "raw_response": "",
            }

        content = [{"type": "text", "text": self._build_prompt(group)}]
        for image in group.get("images", []):
            image_path = self.uploads_dir / image["filename"]
            if not image_path.exists():
                continue
            content_type = image.get("content_type") or mimetypes.guess_type(str(image_path))[0] or ""
            if content_type == "application/pdf" or image_path.suffix.lower() == ".pdf":
                pdf_text = self._pdf_text(image_path)
                if pdf_text:
                    content.append({
                        "type": "text",
                        "text": f"Extracted PDF text from {image.get('original_filename', 'recipe.pdf')}:\n{pdf_text}",
                    })
                continue
            content.append({"type": "text", "text": f"Image role: {image.get('side') or 'extra'}"})
            content.append({
                "type": "image_url",
                "image_url": {"url": self._image_data_url(image_path)},
            })

        if len(content) == 1:
            return {
                "status": "error",
                "error": "No image files were found for this recipe group.",
                "ingredients_text": "",
                "instructions_text": "",
                "sections_json": "[]",
                "raw_response": "",
            }

        try:
            from openai import OpenAI

            client = OpenAI(api_key=self.api_key)
            response = client.chat.completions.create(
                model=self.model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": RECIPE_EXTRACTION_PROMPT},
                    {"role": "user", "content": content},
                ],
            )
            raw_response = response.choices[0].message.content or ""
            payload = self._parse_json(raw_response)
            payload = self._normalize_payload(payload)
            cleanup_raw_response = ""
            cleaned_payload = self._cleanup_payload_for_bake_mode(client, payload)
            if cleaned_payload:
                cleanup_raw_response = cleaned_payload.pop("_raw_response", "")
                payload = self._normalize_payload(cleaned_payload)
            stored_raw_response = raw_response
            if cleanup_raw_response:
                stored_raw_response = json.dumps({
                    "ocr_model": self.model,
                    "ocr_response": raw_response,
                    "cleanup_model": self.cleanup_model,
                    "cleanup_response": cleanup_raw_response,
                })
            return {
                "status": "extracted",
                "error": "",
                "ingredients_text": payload.get("ingredients_text", ""),
                "instructions_text": payload.get("instructions_text", ""),
                "sections_json": json.dumps(payload.get("sections", [])),
                "raw_response": stored_raw_response,
            }
        except Exception as exc:
            return {
                "status": "error",
                "error": f"OCR failed: {exc}",
                "ingredients_text": "",
                "instructions_text": "",
                "sections_json": "[]",
                "raw_response": "",
            }

    def _cleanup_payload_for_bake_mode(self, client, payload):
        """Run one higher-quality text cleanup pass after OCR extraction."""
        if not self.cleanup_model or self.cleanup_model.lower() in {"0", "false", "off", "none"}:
            return None
        try:
            response = client.chat.completions.create(
                model=self.cleanup_model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": RECIPE_BAKE_MODE_CLEANUP_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
            )
            raw_response = response.choices[0].message.content or ""
            cleaned_payload = self._parse_json(raw_response)
            cleaned_payload["_raw_response"] = raw_response
            return cleaned_payload
        except Exception:
            return None

    def analyze_components(self, meal):
        """Split a complete meal into reusable components."""
        if not self.available:
            return {
                "status": "error",
                "error": "OPENAI_API_KEY is not configured, so component analysis cannot run yet.",
                "components": [],
            }

        try:
            from openai import OpenAI

            client = OpenAI(api_key=self.api_key)
            response = client.chat.completions.create(
                model=self.model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": RECIPE_COMPONENT_PROMPT},
                    {"role": "user", "content": self._build_component_prompt(meal)},
                ],
            )
            raw_response = response.choices[0].message.content or ""
            payload = self._parse_json(raw_response)
            components = self._normalize_components(payload.get("components", []))
            return {"status": "analyzed", "error": "", "components": components}
        except Exception as exc:
            return {"status": "error", "error": f"Component analysis failed: {exc}", "components": []}

    def analyze_component_ingredient_amounts(self, component, images=None):
        """Estimate structured, measurable ingredient amounts for one component."""
        if not self.available:
            return {
                "status": "error",
                "error": "OPENAI_API_KEY is not configured, so ingredient amount analysis cannot run yet.",
                "ingredients": [],
            }

        content = [{"type": "text", "text": self._build_amount_prompt(component)}]
        for image in images or []:
            image_path = self.uploads_dir / image["filename"]
            if not image_path.exists():
                continue
            content.append({"type": "text", "text": f"Original card image role: {image.get('side') or 'extra'}"})
            content.append({
                "type": "image_url",
                "image_url": {"url": self._image_data_url(image_path)},
            })

        try:
            from openai import OpenAI

            client = OpenAI(api_key=self.api_key)
            response = client.chat.completions.create(
                model=self.model,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": RECIPE_INGREDIENT_AMOUNT_PROMPT},
                    {"role": "user", "content": content},
                ],
            )
            raw_response = response.choices[0].message.content or ""
            payload = self._parse_json(raw_response)
            ingredients = self._normalize_structured_ingredients(payload.get("ingredients", []))
            return {"status": "analyzed", "error": "", "ingredients": ingredients}
        except Exception as exc:
            return {"status": "error", "error": f"Ingredient amount analysis failed: {exc}", "ingredients": []}

    @staticmethod
    def _build_component_prompt(meal):
        return (
            f"Complete meal title: {meal.get('title') or 'Untitled meal'}\n\n"
            f"Ingredients:\n{meal.get('ingredients_text') or ''}\n\n"
            f"Steps:\n{meal.get('instructions_text') or ''}\n"
        )

    @staticmethod
    def _build_amount_prompt(component):
        return (
            f"Component title: {component.get('title') or 'Untitled component'}\n"
            f"Component type: {component.get('component_type') or 'other'}\n"
            f"Source complete meal: {component.get('source_meal_title') or 'Unknown meal'}\n\n"
            f"Current component ingredients:\n{component.get('ingredients_text') or ''}\n\n"
            f"Current component steps:\n{component.get('instructions_text') or ''}\n\n"
            f"Full meal ingredients from OCR:\n{component.get('source_meal_ingredients_text') or ''}\n\n"
            f"Full meal steps from OCR:\n{component.get('source_meal_instructions_text') or ''}\n"
        )

    @staticmethod
    def _normalize_components(components):
        allowed_types = {"meat", "carb", "vegetable", "sauce", "soup", "other"}
        normalized = []
        for component in components:
            title = str(component.get("title") or "").strip()
            component_type = str(component.get("component_type") or "other").strip().lower()
            if component_type not in allowed_types:
                component_type = "other"
            ingredients = component.get("ingredients") or []
            instructions = component.get("instructions") or []
            if not title:
                title = RecipeOCRService._fallback_section_title(
                    {"ingredients": ingredients},
                    component_type,
                )
            normalized.append({
                "title": title,
                "component_type": component_type,
                "ingredients_text": "\n".join(str(item).strip() for item in ingredients if str(item).strip()),
                "instructions_text": "\n".join(str(item).strip() for item in instructions if str(item).strip()),
            })
        return normalized

    @staticmethod
    def _normalize_structured_ingredients(ingredients):
        normalized = []
        one_count_amounts = {"", "1", "1.0", "one"}
        spoon_ingredients = {
            "balsamic vinegar",
            "cherry jam",
            "soy sauce",
            "mustard",
            "dijon mustard",
            "honey",
            "mayonnaise",
            "sour cream",
            "cream cheese",
            "tomato paste",
        }
        provenance_terms = (
            "source:",
            "card shows",
            "the card",
            "2-serving",
            "4-serving",
            "for 2 servings",
            "for 4 servings",
            "using the ",
            "estimated as",
            "based on the card",
        )
        for ingredient in ingredients:
            name = str(ingredient.get("name") or "").strip()
            if not name:
                continue
            amount = str(ingredient.get("amount") or "").strip()
            unit = str(ingredient.get("unit") or "").strip()
            preparation = str(ingredient.get("preparation") or "").strip()
            source_text = str(ingredient.get("source_text") or "").strip()
            purchase_note = str(ingredient.get("purchase_note") or "").strip()
            confidence = str(ingredient.get("confidence") or "low").strip().lower()
            if confidence not in {"high", "medium", "low"}:
                confidence = "low"
            lower_name = name.lower()
            lower_unit = unit.lower()
            combined = " ".join([lower_name, lower_unit, source_text.lower()])
            if "onion" in lower_name and (not unit or lower_unit in {"piece", "pieces"}):
                unit = "onion" if amount in one_count_amounts else "onions"
            if "onion" in lower_name and not preparation and "diced" in source_text.lower():
                preparation = "diced"
            if lower_name == "lemon" and not unit:
                unit = "lemon" if amount in one_count_amounts else "lemons"
            if lower_name == "zucchini" and not unit:
                unit = "zucchini"
            if "stock concentrate" in combined or "stock concentrates" in combined:
                if "veggie" in combined or "vegetable" in combined:
                    name = "vegetable stock concentrate"
                if not unit or lower_unit in {"concentrate", "concentrates", "portion", "portions", "packet", "packets"}:
                    unit = "Tbsp"
                    if not amount:
                        amount = "1"
            if amount in one_count_amounts and not unit:
                if lower_name in spoon_ingredients or any(term in lower_name for term in ("vinegar", "jam", "sauce")):
                    unit = "Tbsp"
            if any(term in purchase_note.lower() for term in provenance_terms):
                purchase_note = ""
            normalized.append({
                "name": name,
                "amount": amount,
                "unit": unit,
                "preparation": preparation,
                "source_text": source_text,
                "purchase_note": purchase_note,
                "confidence": confidence,
            })
        return normalized

    @staticmethod
    def _normalize_payload(payload):
        """Polish common extraction issues without inventing recipe text."""
        title = str(payload.get("title") or "").strip()
        normalized_title = RecipeOCRService._normalize_title_key(title)
        sections = payload.get("sections") or []

        if normalized_title in GENERIC_RECIPE_TITLES:
            title = RecipeOCRService._title_from_sections(sections) or title

        for section in sections:
            section_title = str(section.get("title") or "").strip()
            if RecipeOCRService._normalize_title_key(section_title) in GENERIC_RECIPE_TITLES:
                section_type = str(section.get("type") or "").strip().lower()
                if section_type == "main" and RecipeOCRService._normalize_title_key(title) not in GENERIC_RECIPE_TITLES:
                    section["title"] = title
                else:
                    section["title"] = RecipeOCRService._fallback_section_title(section, section_type)

        payload["title"] = title
        payload["sections"] = sections
        return payload

    @staticmethod
    def _title_from_sections(sections):
        for section in sections:
            title = str(section.get("title") or "").strip()
            if RecipeOCRService._normalize_title_key(title) not in GENERIC_RECIPE_TITLES:
                return title
        return ""

    @staticmethod
    def _fallback_section_title(section, section_type):
        ingredients = section.get("ingredients") or []
        if ingredients:
            first_ingredient = str(ingredients[0]).strip()
            if first_ingredient:
                return f"{section_type.title() if section_type else 'Recipe'} with {first_ingredient.split(',')[0]}"
        return section_type.title() if section_type and section_type not in GENERIC_RECIPE_TITLES else "Untitled recipe section"

    @staticmethod
    def _normalize_title_key(title):
        return " ".join(title.lower().replace(":", "").strip().split())

    @staticmethod
    def _build_prompt(group):
        label = group.get("label") or "Recipe image group"
        roles = ", ".join(
            f"{image.get('original_filename', 'image')} as {image.get('side') or 'extra'}"
            for image in group.get("images", [])
        )
        if group.get("layout") == "pdf":
            return (
                f"Extract structured recipe text from PDF recipe import {label}.\n"
                f"Uploaded assets: {roles}.\n"
                f"{PDF_RECIPE_EXTRACTION_NOTE}"
            )
        return (
            f"Extract structured recipe text from {label}.\n"
            f"Uploaded image roles: {roles}.\n"
            "Use the front image primarily for ingredients and the back image "
            "primarily for steps, while incorporating extra pages when present."
        )

    @staticmethod
    def _pdf_text(pdf_path):
        try:
            import fitz
        except ImportError:
            return ""
        try:
            document = fitz.open(str(pdf_path))
            pages = []
            for page in document:
                page_text = page.get_text("text").strip()
                if page_text:
                    pages.append(page_text)
            text = "\n\n".join(pages)
            return text[:16000]
        except Exception:
            return ""
        finally:
            if "document" in locals():
                document.close()

    @staticmethod
    def _parse_json(raw_response):
        text = raw_response.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        return json.loads(text)

    @staticmethod
    def _image_data_url(image_path):
        content_type = mimetypes.guess_type(str(image_path))[0] or "image/jpeg"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        return f"data:{content_type};base64,{encoded}"
