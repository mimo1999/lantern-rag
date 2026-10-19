from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class Generation:
    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class GenerationError(RuntimeError):
    """Raised when the LLM provider fails or returns an unusable response."""


class LLMProvider(Protocol):
    async def generate(self, messages: list[ChatMessage]) -> Generation: ...
