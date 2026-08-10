"""Sparse, advisory LLM support behind deterministic trading gates."""

from .config import LLMConfig, get_llm
from .coordinator import LLMCoordinator
from .judge import JudgeDecision, SparseL3Judge, safe_judge_context
from .provider import CursorJSONProvider, LLMBudgetExhausted, LLMUnavailable
from .reflection import WeeklyReflection, WeeklyReflector, build_reflection_payload

__all__ = [
    "CursorJSONProvider",
    "JudgeDecision",
    "LLMBudgetExhausted",
    "LLMConfig",
    "LLMCoordinator",
    "LLMUnavailable",
    "SparseL3Judge",
    "WeeklyReflection",
    "WeeklyReflector",
    "build_reflection_payload",
    "get_llm",
    "safe_judge_context",
]
