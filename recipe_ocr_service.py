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
      "component_type": "meat|carb|vegetable|sauce|other",
      "ingredients": [],
      "instructions": []
    }
  ]
}

Rules:
- Components can be meat, carb, vegetable, sauce, or other.
- Extract every distinct component that could stand alone as a side/main/sauce.
- Keep ingredients needed for that component.
- Keep only instructions directly related to that component.
- Do not include unrelated complete-meal assembly instructions unless the
  component requires them.
- Use specific titles like "Crispy Potatoes", "Broccoli", "Fig Sauce", or
  "Pork Chops", not generic titles like "Side" or "Main".
- If a complete meal has no separable component, return the main prepared item
  as one component with the best type.
"""


class RecipeOCRService:
    """Use a configured vision model to extract structured recipe text."""

    def __init__(self, uploads_dir):
        self.uploads_dir = Path(uploads_dir)
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.model = os.getenv("RECIPE_OCR_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini"))

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
            return {
                "status": "extracted",
                "error": "",
                "ingredients_text": payload.get("ingredients_text", ""),
                "instructions_text": payload.get("instructions_text", ""),
                "sections_json": json.dumps(payload.get("sections", [])),
                "raw_response": raw_response,
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

    @staticmethod
    def _build_component_prompt(meal):
        return (
            f"Complete meal title: {meal.get('title') or 'Untitled meal'}\n\n"
            f"Ingredients:\n{meal.get('ingredients_text') or ''}\n\n"
            f"Steps:\n{meal.get('instructions_text') or ''}\n"
        )

    @staticmethod
    def _normalize_components(components):
        allowed_types = {"meat", "carb", "vegetable", "sauce", "other"}
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
        return (
            f"Extract structured recipe text from {label}.\n"
            f"Uploaded image roles: {roles}.\n"
            "Use the front image primarily for ingredients and the back image "
            "primarily for steps, while incorporating extra pages when present."
        )

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
