"""Typed configuration for sparse LLM decision support.

The LLM is deliberately outside the deterministic signal and risk config. In
the shipped shadow mode it records a counterfactual veto/score but cannot create,
block, resize, or place an order.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from ..config import CONFIG_DIR


class JudgeConfig(BaseModel):
    enabled: bool = True
    tiers: list[str] = Field(default_factory=lambda: ["large", "mid", "micro"])
    required_tiers: list[str] = Field(default_factory=lambda: ["mid", "micro"])
    min_catalyst_score: float = 0.55
    cache_ttl_minutes: int = 60
    max_social_posts: int = 12
    max_post_chars: int = 500
    state_file: str = "./data/llm_judge_cache.json"

    @field_validator("min_catalyst_score")
    @classmethod
    def _score_range(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("min_catalyst_score must be within 0..1")
        return value


class ReflectionConfig(BaseModel):
    enabled: bool = True
    state_file: str = "./data/weekly_reflections.jsonl"
    max_trades: int = 80
    deliver_telegram: bool = True


class LLMConfig(BaseModel):
    enabled: bool = True
    provider: str = "cursor"
    model_family: str = "sonnet"
    max_calls_per_month: int = 250
    request_timeout_seconds: int = 120
    budget_state_file: str = "./data/llm_budget.json"
    sandbox_dir: str = "./data/llm_sandbox"
    judge: JudgeConfig = Field(default_factory=JudgeConfig)
    reflection: ReflectionConfig = Field(default_factory=ReflectionConfig)

    @field_validator("provider")
    @classmethod
    def _provider_supported(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized != "cursor":
            raise ValueError("provider must be 'cursor'")
        return normalized


@lru_cache(maxsize=1)
def get_llm() -> LLMConfig:
    path = CONFIG_DIR / "llm.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return LLMConfig(**(data or {}))


def ensure_llm_paths(cfg: LLMConfig) -> None:
    """Create only the local state directories required by LLM support."""
    for raw in (
        cfg.budget_state_file,
        cfg.judge.state_file,
        cfg.reflection.state_file,
    ):
        Path(raw).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.sandbox_dir).mkdir(parents=True, exist_ok=True)
