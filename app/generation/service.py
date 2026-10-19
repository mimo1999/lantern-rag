import time
from dataclasses import dataclass
from typing import Literal

from app.auth.models import UserContext
from app.generation.prompts import build_messages, select_context
from app.generation.provider import LLMProvider
from app.retrieval.service import RetrievalService, RetrievedChunk

NO_ACCESSIBLE_INFORMATION = "No accessible information was found."


@dataclass(frozen=True, slots=True)
class AnswerResult:
    answer: str
    chunks_used: list[RetrievedChunk]
    outcome: Literal["answered", "no_results"]
    llm_model: str | None
    input_tokens: int | None
    output_tokens: int | None
    retrieval_ms: float
    generation_ms: float | None


class AnswerService:
    """Retrieve authorized chunks, then generate an answer grounded only in those chunks."""

    def __init__(
        self, retrieval: RetrievalService, provider: LLMProvider, max_context_chars: int
    ) -> None:
        self._retrieval = retrieval
        self._provider = provider
        self._max_context_chars = max_context_chars

    async def answer(self, question: str, user: UserContext) -> AnswerResult:
        started = time.perf_counter()
        retrieved = await self._retrieval.retrieve(question, user)
        retrieval_ms = (time.perf_counter() - started) * 1000
        if not retrieved:
            return AnswerResult(
                NO_ACCESSIBLE_INFORMATION, [], "no_results", None, None, None, retrieval_ms, None
            )

        context = select_context(retrieved, self._max_context_chars)
        started = time.perf_counter()
        generation = await self._provider.generate(build_messages(question, context))
        generation_ms = (time.perf_counter() - started) * 1000
        return AnswerResult(
            generation.text,
            context,
            "answered",
            generation.model,
            generation.input_tokens,
            generation.output_tokens,
            retrieval_ms,
            generation_ms,
        )
