import json
import logging
import time
from typing import Optional, Dict, Any, List
import requests
from backend.app.config import settings
from backend.app.providers.base import LLMProvider, LLMResponse

logger = logging.getLogger(__name__)

class GeminiProvider(LLMProvider):
    """
    Dedicated Google Gemini LLM Provider using the Generative Language API.
    Strictly uses the user's Gemini API key with verified models.
    """
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None
    ):
        self.api_key = api_key or settings.llm_api_key
        self.model = model or settings.llm_model or "gemini-3.5-flash"
        if self.model.startswith("models/"):
            self.model = self.model.replace("models/", "", 1)
        self.base_url = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")

    def generate(self, prompt: str, system_prompt: Optional[str] = None, temperature: float = 0.0) -> LLMResponse:
        url = f"{self.base_url}/models/{self.model}:generateContent?key={self.api_key}"
        headers = {"Content-Type": "application/json"}

        payload: Dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}]
                }
            ],
            "generationConfig": {
                "temperature": temperature
            }
        }

        if system_prompt:
            payload["systemInstruction"] = {
                "parts": [{"text": system_prompt}]
            }

        last_error = None
        max_attempts = 3
        for attempt in range(max_attempts):
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=60)
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if not candidates:
                        raise ValueError(f"Gemini returned empty candidates: {data}")
                    content = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                    usage = data.get("usageMetadata", {})
                    return LLMResponse(
                        content=content,
                        model=self.model,
                        prompt_tokens=usage.get("promptTokenCount", 0),
                        completion_tokens=usage.get("candidatesTokenCount", 0),
                        raw_response=data
                    )
                elif resp.status_code in (429, 503):
                    wait_time = (attempt + 1) * 2.0
                    logger.warning(
                        f"Gemini API ({self.model}) received HTTP {resp.status_code}. "
                        f"Attempt {attempt + 1}/{max_attempts}."
                    )
                    last_error = RuntimeError(f"Gemini HTTP {resp.status_code}: {resp.text}")
                    if ("limit: 20" in resp.text or "RESOURCE_EXHAUSTED" in resp.text) and self.model != "gemini-3.5-flash-lite":
                        logger.info(f"Model {self.model} reached daily free tier quota. Switching to active gemini-3.5-flash-lite...")
                        self.model = "gemini-3.5-flash-lite"
                        url = f"{self.base_url}/models/{self.model}:generateContent?key={self.api_key}"
                        continue
                    time.sleep(wait_time)
                    continue
                else:
                    resp.raise_for_status()
            except Exception as e:
                logger.warning(f"Gemini API attempt {attempt + 1}/{max_attempts} error: {e}")
                last_error = e
                if attempt < max_attempts - 1:
                    time.sleep((attempt + 1) * 1.5)

        logger.error(f"Gemini generation failed for model {self.model}: {last_error}")
        raise last_error or RuntimeError(f"Gemini generation failed for model {self.model}")
