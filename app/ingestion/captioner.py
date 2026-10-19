import base64
from typing import Protocol

import httpx

_PROMPT = (
    "Describe this image from a document so it can be found by search. Say what kind of image "
    "it is (chart, diagram, photo, screenshot, scan). State the title, labels, axis names, "
    "numbers and any text you can read. Be factual and brief; do not guess beyond what is shown."
)


class CaptionError(RuntimeError):
    """The vision model could not describe an image."""


class Captioner(Protocol):
    async def caption(self, png: bytes) -> str: ...


class OllamaCaptioner:
    """Describe an image with a vision-capable Ollama model (non-streaming /api/chat)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_seconds: float = 180.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = httpx.Timeout(timeout_seconds, connect=5.0)
        self._transport = transport

    async def caption(self, png: bytes) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": _PROMPT,
                    "images": [base64.b64encode(png).decode()],
                }
            ],
            "stream": False,
            "think": False,
            "options": {"temperature": 0, "num_predict": 300},
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post("/api/chat", json=payload)
                response.raise_for_status()
            text = response.json()["message"]["content"]
        except httpx.HTTPError as error:
            raise CaptionError("Vision model request failed") from error
        except (KeyError, TypeError, ValueError) as error:
            raise CaptionError("Vision model response did not contain a caption") from error
        if not isinstance(text, str) or not text.strip():
            raise CaptionError("Vision model returned an empty caption")
        return text.strip()
