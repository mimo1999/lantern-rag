import json

import httpx
import pytest

from app.generation.ollama import OllamaChatProvider
from app.generation.provider import ChatMessage, GenerationError


def provider(handler) -> OllamaChatProvider:
    return OllamaChatProvider(
        "http://ollama:11434", "qwen3:4b", transport=httpx.MockTransport(handler)
    )


@pytest.mark.asyncio
async def test_generate_sends_non_streaming_chat_request_and_parses_usage() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(path=request.url.path, body=json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": "  Revenue was 81.4M. [d_0001] "},
                "prompt_eval_count": 120,
                "eval_count": 15,
            },
        )

    result = await provider(handler).generate([ChatMessage("user", "hi")])

    assert seen["path"] == "/api/chat"
    assert seen["body"]["stream"] is False and seen["body"]["model"] == "qwen3:4b"
    assert seen["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert (result.text, result.input_tokens, result.output_tokens) == (
        "Revenue was 81.4M. [d_0001]",
        120,
        15,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, json={"unexpected": True}),
        httpx.Response(200, json={"message": {"content": "   "}}),
    ],
)
async def test_generate_raises_generation_error_on_bad_responses(response) -> None:
    with pytest.raises(GenerationError):
        await provider(lambda request: response).generate([ChatMessage("user", "hi")])


@pytest.mark.asyncio
async def test_generate_wraps_transport_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(GenerationError):
        await provider(handler).generate([ChatMessage("user", "hi")])
