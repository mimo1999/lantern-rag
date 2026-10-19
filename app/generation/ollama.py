import httpx

from app.generation.provider import ChatMessage, Generation, GenerationError


class OllamaChatProvider:
    """LLMProvider backed by Ollama's non-streaming /api/chat endpoint."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        temperature: float = 0.1,
        max_output_tokens: int = 1_024,
        timeout_seconds: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._timeout = httpx.Timeout(timeout_seconds, connect=5.0)
        self._transport = transport

    async def generate(self, messages: list[ChatMessage]) -> Generation:
        payload = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "think": False,
            "options": {"temperature": self._temperature, "num_predict": self._max_output_tokens},
        }
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post("/api/chat", json=payload)
                response.raise_for_status()
            body = response.json()
            text = body["message"]["content"]
        except httpx.HTTPError as error:
            raise GenerationError("LLM request failed") from error
        except (KeyError, TypeError, ValueError) as error:
            raise GenerationError("LLM response did not contain a message") from error
        if not isinstance(text, str) or not text.strip():
            raise GenerationError("LLM returned an empty answer")
        return Generation(
            text=text.strip(),
            model=self._model,
            input_tokens=body.get("prompt_eval_count"),
            output_tokens=body.get("eval_count"),
        )
