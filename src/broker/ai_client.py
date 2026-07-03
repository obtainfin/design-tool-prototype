"""OpenAI Responses API client.

Reads ``OPENAI_API_KEY`` from a local ``.env`` file only (never from a
system-wide environment unless the user put it there themselves). The key
is never logged or echoed, and is scrubbed from any error message. Setting
``BROKER_AI_DISABLED=1`` forces the whole app into local-only, rule-based
mode regardless of whether a key is present. With no key configured, every
feature still works via offline fallbacks and the UI shows a visible
"AI not connected" badge.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_MODEL = "gpt-4.1-mini"
RESPONSES_URL = "https://api.openai.com/v1/responses"
REQUEST_TIMEOUT_SECONDS = 30

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_PATH = _PROJECT_ROOT / ".env"


class AIRequestError(RuntimeError):
    """Raised when a call to the AI provider fails. Never contains the key."""


def _redact(message: str, api_key: Optional[str]) -> str:
    if api_key and api_key in message:
        message = message.replace(api_key, "[REDACTED]")
    return message


def load_dotenv(path: Path = _ENV_PATH) -> Dict[str, str]:
    """Parse a simple KEY=VALUE .env file. Missing file -> empty dict."""
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


class AIClient:
    def __init__(self, env: Optional[Dict[str, str]] = None):
        env = env if env is not None else load_dotenv()
        self._api_key = env.get("OPENAI_API_KEY") or None
        self.model = env.get("OPENAI_MODEL") or DEFAULT_MODEL
        disabled_flag = env.get("BROKER_AI_DISABLED") or os.environ.get("BROKER_AI_DISABLED")
        self._force_disabled = disabled_flag == "1"

    @property
    def enabled(self) -> bool:
        return bool(self._api_key) and not self._force_disabled

    def status_label(self) -> str:
        if self._force_disabled:
            return "AI not connected (disabled by BROKER_AI_DISABLED)"
        if not self._api_key:
            return "AI not connected (no API key configured)"
        return f"AI connected ({self.model})"

    def respond(self, instructions: str, user_content: str) -> str:
        """Call the Responses API and return the raw assistant text output."""
        if not self.enabled:
            raise AIRequestError("AI is not enabled (no API key or BROKER_AI_DISABLED=1).")

        body = json.dumps(
            {
                "model": self.model,
                "instructions": instructions,
                "input": user_content,
            }
        ).encode("utf-8")

        request = urllib.request.Request(
            RESPONSES_URL,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
            raise AIRequestError(
                _redact(f"AI request failed (HTTP {exc.code}): {detail[:300]}", self._api_key)
            ) from None
        except urllib.error.URLError as exc:
            raise AIRequestError(_redact(f"AI request failed: {exc.reason}", self._api_key)) from None
        except Exception as exc:  # noqa: BLE001
            raise AIRequestError(_redact(f"AI request failed: {exc}", self._api_key)) from None

        return _extract_output_text(payload)


def _extract_output_text(payload: Dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    texts: List[str] = []
    for item in payload.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if content.get("type") in ("output_text", "text") and isinstance(content.get("text"), str):
                texts.append(content["text"])
    return "\n".join(texts)
