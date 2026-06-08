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

Preserve wording when possible. If the card clearly contains multiple side
dishes, sauces, or components, split those into separate sections. If text is
unclear, put a short note in "uncertain" rather than inventing missing words.
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
                messages=[
                    {"role": "system", "content": RECIPE_EXTRACTION_PROMPT},
                    {"role": "user", "content": content},
                ],
            )
            raw_response = response.choices[0].message.content or ""
            payload = self._parse_json(raw_response)
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
