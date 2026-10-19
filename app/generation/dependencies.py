from functools import lru_cache

from fastapi import Depends

from app.config import Settings, get_settings
from app.dependencies import get_retrieval_service
from app.generation.ollama import OllamaChatProvider
from app.generation.provider import LLMProvider
from app.generation.service import AnswerService
from app.retrieval.service import RetrievalService


@lru_cache
def get_llm_provider() -> LLMProvider:
    settings = get_settings()
    return OllamaChatProvider(settings.ollama_url, settings.llm_model)


def get_answer_service(
    retrieval: RetrievalService = Depends(get_retrieval_service),
    provider: LLMProvider = Depends(get_llm_provider),
    settings: Settings = Depends(get_settings),
) -> AnswerService:
    return AnswerService(retrieval, provider, settings.max_context_chars)
