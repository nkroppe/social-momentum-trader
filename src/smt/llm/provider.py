"""Cursor SDK provider with no tools and a hard monthly call budget."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from .config import LLMConfig, ensure_llm_paths

log = get_logger("smt.llm.provider")

_JSON_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class LLMUnavailable(RuntimeError):
    """The configured model cannot be called safely right now."""


class LLMBudgetExhausted(LLMUnavailable):
    """The local monthly invocation cap has been reached."""


class MonthlyCallBudget:
    """Small persisted guardrail independent of provider-side billing."""

    def __init__(self, path: str, limit: int):
        self.path = Path(path)
        self.limit = max(limit, 0)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @staticmethod
    def _month() -> str:
        return datetime.now(UTC).strftime("%Y-%m")

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"month": self._month(), "calls": 0}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise LLMBudgetExhausted(
                f"LLM budget state is unreadable; refusing calls: {exc}"
            ) from exc
        if data.get("month") != self._month():
            return {"month": self._month(), "calls": 0}
        return data

    @contextmanager
    def _file_lock(self):
        lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")
        deadline = time.monotonic() + 5.0
        fd: int | None = None
        while fd is None:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    if time.time() - lock_path.stat().st_mtime > 60:
                        lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise LLMBudgetExhausted("LLM budget lock is unavailable") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            os.close(fd)
            lock_path.unlink(missing_ok=True)

    def _save(self, data: dict[str, Any]) -> None:
        temp = self.path.with_suffix(f"{self.path.suffix}.{os.getpid()}.tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp.replace(self.path)

    @property
    def calls(self) -> int:
        with self._lock, self._file_lock():
            return int(self._load().get("calls", 0))

    def reserve(self) -> None:
        """Atomically reserve one call before sending it to Cursor."""
        with self._lock, self._file_lock():
            data = self._load()
            calls = int(data.get("calls", 0))
            if calls >= self.limit:
                raise LLMBudgetExhausted(
                    f"LLM monthly call budget exhausted ({calls}/{self.limit})"
                )
            data["calls"] = calls + 1
            self._save(data)


def parse_json_response(text: str) -> dict[str, Any]:
    """Extract one JSON object from a text-only model response."""
    cleaned = _JSON_FENCE.sub("", text.strip()).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise LLMUnavailable("LLM response did not contain a JSON object") from None
        try:
            parsed = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMUnavailable(f"LLM returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LLMUnavailable("LLM response must be a JSON object")
    return parsed


class CursorJSONProvider:
    """One-shot Sonnet analysis through Cursor, with all tools disabled.

    `tools=[]` is a security boundary: the agent can only return text. It
    cannot inspect the VPS, execute shell commands, read secrets, edit files,
    or place trades. Only the explicitly supplied market/social context reaches
    the model.
    """

    def __init__(self, cfg: LLMConfig, api_key: str | None = None):
        self.cfg = cfg
        self.api_key = (api_key or os.environ.get("CURSOR_API_KEY", "")).strip()
        ensure_llm_paths(cfg)
        self.budget = MonthlyCallBudget(cfg.budget_state_file, cfg.max_calls_per_month)
        self._model_id: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.cfg.enabled and self.api_key)

    def _resolve_model(self) -> str:
        if self._model_id is not None:
            return self._model_id
        if not self.api_key:
            raise LLMUnavailable("set CURSOR_API_KEY to enable the LLM judge")
        try:
            from cursor_sdk import Cursor

            models = Cursor.models.list(api_key=self.api_key)
        except Exception as exc:  # noqa: BLE001
            raise LLMUnavailable(f"Cursor model discovery failed: {exc}") from exc

        family = self.cfg.model_family.lower()
        matches = [m.id for m in models if family in m.id.lower()]
        if not matches:
            available = ", ".join(m.id for m in models)
            raise LLMUnavailable(
                f"no Cursor model matching {family!r}; account models: {available}"
            )
        # Prefer the newest/highest plain Sonnet id over fast/legacy variants.
        plain = [
            model
            for model in matches
            if not any(tag in model.lower() for tag in ("fast", "thinking", "legacy"))
        ] or [model for model in matches if "fast" not in model.lower()] or matches

        def rank(model: str) -> tuple[int, ...]:
            return tuple(int(part) for part in re.findall(r"\d+", model))

        self._model_id = max(plain, key=lambda model: (rank(model), model))
        log.info("Cursor LLM model resolved to %s", self._model_id)
        return self._model_id

    def complete_json(self, instruction: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.available:
            raise LLMUnavailable("Cursor LLM is disabled or CURSOR_API_KEY is missing")

        self.budget.reserve()
        prompt = (
            f"{instruction}\n\n"
            "Return exactly one valid JSON object. Do not use markdown fences. "
            "Do not provide chain-of-thought. Use only the supplied data; if it "
            "is insufficient, express low confidence or veto.\n\n"
            f"INPUT:\n{json.dumps(payload, separators=(',', ':'), ensure_ascii=True)}"
        )
        try:
            from cursor_sdk import Agent, AgentOptions, LocalAgentOptions

            options = AgentOptions(
                    api_key=self.api_key,
                    model=self._resolve_model(),
                    tools=[],
                    local=LocalAgentOptions(cwd=str(Path(self.cfg.sandbox_dir).resolve())),
            )
            timed_out = threading.Event()
            with Agent.create(options) as agent:
                run = agent.send(prompt)

                def cancel() -> None:
                    timed_out.set()
                    try:
                        run.cancel()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Cursor LLM timeout cancellation failed: %s", exc)

                timer = threading.Timer(self.cfg.request_timeout_seconds, cancel)
                timer.daemon = True
                timer.start()
                try:
                    result = run.wait()
                finally:
                    timer.cancel()
        except Exception as exc:  # noqa: BLE001
            raise LLMUnavailable(f"Cursor LLM call failed: {exc}") from exc

        if timed_out.is_set():
            raise LLMUnavailable(
                f"Cursor LLM exceeded {self.cfg.request_timeout_seconds}s timeout"
            )
        if result.status != "finished" or not result.result:
            raise LLMUnavailable(f"Cursor LLM run ended with status {result.status!r}")
        return parse_json_response(result.result)
