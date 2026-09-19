from __future__ import annotations

from google import genai
from google.genai import types

from ..config import Settings
from ..models import AIExplanation, CameraObservation


class GeminiService:
    def __init__(self, settings: Settings):
        self.settings = settings
        if settings.google_genai_use_vertexai:
            self.client = genai.Client(
                vertexai=True,
                project=settings.google_cloud_project,
                location=settings.google_cloud_location,
            )
        else:
            self.client = genai.Client(api_key=settings.gemini_api_key)

    def inspect_camera(self, image_bytes: bytes, mime_type: str = "image/jpeg") -> tuple[CameraObservation, str]:
        prompt = """
You are observing ONE still frame from a Transport for London JamCam for noise evaluation by picture.
Extract only visually supported facts. Do NOT infer vehicles/hour, traffic flow per hour, road identity, or legal noise levels.
Count visible vehicles by coarse class where possible. Classify congestion and apparent speed conservatively.
If image quality, occlusion, or camera angle makes a value uncertain, use null/unknown and reduce confidence.
""".strip()
        response = self.client.models.generate_content(
            model=self.settings.gemini_model,
            contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime_type), prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CameraObservation,
                temperature=0.1,
            ),
        )
        parsed = CameraObservation.model_validate_json(response.text)
        return parsed, response.text

    def explain_result(self, facts: dict) -> tuple[AIExplanation, str]:
        prompt = f"""
Explain this acoustic simulation result using only the supplied JSON facts.
Do not invent measurements. Clearly distinguish measured/public-source inputs, AI observations, and modelling assumptions.
Keep the explanation useful to a London resident and include one caveat.

FACTS:
{facts}
"""
        response = self.client.models.generate_content(
            model=self.settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AIExplanation,
                temperature=0.2,
            ),
        )
        parsed = AIExplanation.model_validate_json(response.text)
        return parsed, response.text
