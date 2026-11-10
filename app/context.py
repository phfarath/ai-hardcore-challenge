"""Contexto por requisição: relógio, orçamento, identidade e evidências coletadas."""

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from app.schemas import Evidence, ToolResult


class BudgetExceeded(Exception):
    """Um limite de chamadas (LLM, ferramentas ou buscas web) foi atingido."""


class DeadlineExceeded(Exception):
    """O prazo total da requisição acabou."""


class ProviderUnavailable(Exception):
    """O provedor de LLM falhou de forma persistente."""


class IndexUnavailable(Exception):
    """O índice de conhecimento está ausente ou é incompatível com o embedder atual."""


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def __init__(self, timezone: str = "America/Sao_Paulo") -> None:
        self._tz = ZoneInfo(timezone)

    def now(self) -> datetime:
        return datetime.now(self._tz)


class Budget:
    def __init__(
        self,
        max_llm_calls: int = 6,
        max_tool_calls: int = 4,
        max_web_searches: int = 1,
        deadline_seconds: float = 40.0,
    ) -> None:
        self._max_llm = max_llm_calls
        self._max_tool = max_tool_calls
        self._max_web = max_web_searches
        self._deadline = deadline_seconds
        self._started = time.monotonic()
        self.llm_calls = 0
        self.tool_calls = 0
        self.web_searches = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def charge_llm(self) -> None:
        if self.llm_calls >= self._max_llm:
            raise BudgetExceeded("llm call limit reached")
        self.llm_calls += 1

    def charge_tool(self) -> None:
        if self.tool_calls >= self._max_tool:
            raise BudgetExceeded("tool call limit reached")
        self.tool_calls += 1

    def charge_web(self) -> None:
        if self.web_searches >= self._max_web:
            raise BudgetExceeded("web search limit reached")
        self.web_searches += 1

    def record_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens

    def remaining_seconds(self) -> float:
        return self._deadline - (time.monotonic() - self._started)

    def check_deadline(self) -> None:
        if self.remaining_seconds() <= 0:
            raise DeadlineExceeded("request deadline reached")


@dataclass
class RequestContext:
    request_id: str
    user_id: str
    clock: Clock
    budget: Budget
    evidence: dict[str, Evidence] = field(default_factory=dict)
    tool_results: list[ToolResult] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
