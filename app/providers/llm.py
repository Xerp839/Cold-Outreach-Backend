"""LLM providers behind one interface, tried in order: Gemini first, Groq when Gemini is rate limited / out of quota."""

import json
import re
import threading
import time

import httpx

from app.config import get_settings
from app.tracking import record_usage


class ProviderExhausted(Exception):
    """Quota / credits gone for this run - stop calling this provider."""


class ProviderError(Exception):
    """Transient or bad-response failure - try the next provider for this call only."""


class AllProvidersFailed(Exception):
    pass


MAX_INLINE_WAIT_S = 65  # wait out per-minute limits; anything longer is treated as exhausted


def parse_json(text: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ProviderError(f"no JSON object in response: {text[:200]}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise ProviderError(f"invalid JSON from model: {e}") from e


class LLMProvider:
    name = "base"

    def __init__(self, api_key: str, model: str, rpm: int):
        self.api_key = api_key
        self.model = model
        self.min_interval = 60.0 / max(rpm, 1)
        self._last_call = 0.0
        self._lock = threading.Lock()
        self.exhausted = False

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and not self.exhausted

    def _throttle(self) -> None:
        with self._lock:
            wait = self._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

    def complete_json(self, system: str, user: str) -> dict:
        for attempt in range(3):
            self._throttle()
            try:
                resp = self._request(system, user)
            except httpx.HTTPError as e:
                record_usage(self.name, ok=False, error=str(e))
                raise ProviderError(f"{self.name} network error: {e}") from e

            if resp.status_code == 429:
                delay = self._retry_delay(resp)
                daily = self._is_daily_quota(resp)
                if daily or delay is None or delay > MAX_INLINE_WAIT_S or attempt == 2:
                    self.exhausted = True
                    record_usage(self.name, ok=False, exhausted=True, error=resp.text[:500])
                    raise ProviderExhausted(f"{self.name} quota exhausted: {resp.text[:300]}")
                record_usage(self.name, ok=False, error="429 rate limited, retrying")
                time.sleep(delay)
                continue
            if resp.status_code in (401, 403):
                self.exhausted = True
                record_usage(self.name, ok=False, exhausted=True, error=resp.text[:500])
                raise ProviderExhausted(f"{self.name} auth/permission error {resp.status_code}: {resp.text[:300]}")
            if resp.status_code >= 400:
                record_usage(self.name, ok=False, error=resp.text[:500])
                raise ProviderError(f"{self.name} HTTP {resp.status_code}: {resp.text[:300]}")

            try:
                data = parse_json(self._extract_text(resp.json()))
            except (ProviderError, KeyError, IndexError, ValueError) as e:
                record_usage(self.name, ok=False, error=str(e))
                raise ProviderError(f"{self.name} bad response: {e}") from e
            record_usage(self.name, ok=True)
            return data
        raise ProviderError(f"{self.name}: retries exhausted")

    # --- provider specifics ---
    def _request(self, system: str, user: str) -> httpx.Response:
        raise NotImplementedError

    def _extract_text(self, payload: dict) -> str:
        raise NotImplementedError

    def _retry_delay(self, resp: httpx.Response) -> float | None:
        header = resp.headers.get("retry-after")
        if header:
            try:
                return float(header)
            except ValueError:
                return None
        return 20.0

    def _is_daily_quota(self, resp: httpx.Response) -> bool:
        return False


class GeminiProvider(LLMProvider):
    name = "gemini"

    def _request(self, system: str, user: str) -> httpx.Response:
        return httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            headers={"x-goog-api-key": self.api_key},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4},
            },
            timeout=90,
        )

    def _extract_text(self, payload: dict) -> str:
        parts = payload["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts if not p.get("thought"))

    def _retry_delay(self, resp: httpx.Response) -> float | None:
        # Gemini puts RetryInfo {"retryDelay": "37s"} in the error details.
        m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', resp.text)
        return float(m.group(1)) + 1 if m else 30.0

    def _is_daily_quota(self, resp: httpx.Response) -> bool:
        return "PerDay" in resp.text or "per_day" in resp.text.lower()


class GroqProvider(LLMProvider):
    name = "groq"

    def _request(self, system: str, user: str) -> httpx.Response:
        return httpx.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_object"},
                "temperature": 0.4,
            },
            timeout=90,
        )

    def _extract_text(self, payload: dict) -> str:
        return payload["choices"][0]["message"]["content"]


class LLMChain:
    def __init__(self, providers: list[LLMProvider]):
        self.providers = providers

    @classmethod
    def from_settings(cls) -> "LLMChain":
        s = get_settings()
        return cls([
            GeminiProvider(s.gemini_api_key, s.gemini_model, s.gemini_rpm),
            GroqProvider(s.groq_api_key, s.groq_model, s.groq_rpm),
        ])

    def complete_json(self, system: str, user: str) -> tuple[dict, str]:
        errors = []
        for provider in self.providers:
            if not provider.enabled:
                continue
            try:
                return provider.complete_json(system, user), provider.name
            except (ProviderExhausted, ProviderError) as e:
                errors.append(str(e))
        if not any(p.enabled for p in self.providers) and not errors:
            errors.append("no LLM provider configured or all are exhausted")
        raise AllProvidersFailed(" | ".join(errors))

    @property
    def any_available(self) -> bool:
        return any(p.enabled for p in self.providers)
