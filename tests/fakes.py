"""Dublês de teste: LLM roteirizado, relógio congelado e helpers de turnos."""

import hashlib
import itertools
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.config import Settings
from app.context import Budget
from app.deps import Deps
from app.llm import LLMClient, LLMTurn, ToolCall, ToolSpec
from app.rag import KnowledgeIndex, chunk_document
from app.repositories import JsonCustomerRepository
from app.tools.search import WebResult

_call_ids = itertools.count(1)


class FrozenClock:
    def __init__(self, dt: datetime | None = None) -> None:
        self._dt = dt or datetime(2026, 11, 7, 10, 0, tzinfo=timezone(timedelta(hours=-3)))

    def now(self) -> datetime:
        return self._dt


class ScriptedLLM:
    """LLM que devolve turnos pré-definidos e registra o que recebeu."""

    def __init__(self, turns: list[LLMTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[list[dict[str, Any]]] = []
        self.tools_seen: list[list[ToolSpec] | None] = []
        self.output_models: list[type[BaseModel] | None] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolSpec] | None,
        output_model: type[BaseModel] | None,
        budget: Budget,
    ) -> LLMTurn:
        budget.charge_llm()
        self.calls.append(list(messages))
        self.tools_seen.append(tools)
        self.output_models.append(output_model)
        if not self._turns:
            raise AssertionError("ScriptedLLM: no turns left")
        return self._turns.pop(0)


def tool_call_turn(name: str, **args: Any) -> LLMTurn:
    call = ToolCall(
        id=f"call_{next(_call_ids)}", name=name, arguments=json.dumps(args, default=str)
    )
    return LLMTurn(tool_calls=[call])


def final_turn(model: BaseModel) -> LLMTurn:
    return LLMTurn(parsed=model)


class FakeEmbedder:
    """Embeddings determinísticos por hash: textos iguais geram vetores iguais."""

    model = "fake-embed"

    def __init__(self, dim: int = 16) -> None:
        assert dim <= 32
        self.dim = dim
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [(b - 127.5) / 127.5 for b in digest[: self.dim]]


class FakeSearch:
    """Provedor de busca web falso: devolve resultados fixos ou levanta o erro configurado."""

    def __init__(
        self, results: "list[WebResult] | None" = None, raises: Exception | None = None
    ) -> None:
        self._results = results or []
        self._raises = raises
        self.queries: list[str] = []

    async def search(self, query: str, max_results: int = 5) -> "list[WebResult]":
        self.queries.append(query)
        if self._raises is not None:
            raise self._raises
        return self._results[:max_results]


SMART_URL = "https://site.getnet.com.br/get-ajuda-maquininha/solucoes-get-smart/"
ADQ_URL = "https://site.getnet.com.br/get-ajuda-erros-maquininha/erro-adq-1-19/"
SMART_CHUNK_ID = "site-getnet-com-br-get-ajuda-maquininha-solucoes-get-smart-001"
ADQ_CHUNK_ID = "site-getnet-com-br-get-ajuda-erros-maquininha-erro-adq-1-19-001"
_SEED = [
    (SMART_URL, "Soluções Get Smart", "A Get Smart tem tela touch e impressora térmica integrada."),
    (
        ADQ_URL,
        "Erro ADQ 1-19: erro de conexão",
        "Erro ADQ 1-19: erro de conexão. Verifique o Wi-Fi ou o chip e reinicie a maquininha.",
    ),
]


async def make_deps(
    tmp_path: Path,
    llm: LLMClient,
    *,
    search: Any = None,
    seed: bool = True,
    clock: FrozenClock | None = None,
    **settings_overrides: Any,
) -> Deps:
    """Deps de teste: fixtures reais de clientes, índice Chroma em tmp_path com 2 chunks."""
    index = KnowledgeIndex(str(tmp_path / "idx"), FakeEmbedder())
    if seed:
        for url, title, text in _SEED:
            chunks = chunk_document(
                text, source_url=url, title=title, fetched_at=datetime(2026, 11, 7, tzinfo=UTC)
            )
            await index.replace_document(url, chunks)
    return Deps(
        llm=llm,
        repo=JsonCustomerRepository("data/fixtures/customers.json"),
        index=index,
        search=search,
        clock=clock or FrozenClock(),
        settings=Settings(_env_file=None, **settings_overrides),  # type: ignore[call-arg]
    )
