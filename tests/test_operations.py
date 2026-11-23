"""T09: fronteira de privacidade, logs com allowlist, retry por causa, prontidão, desligamento
e saturação previsível."""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest

from app.context import Budget, ProviderUnavailable, RequestContext
from app.deps import Deps
from app.llm import LLMTurn, OpenAIClient
from app.main import create_app
from app.observability import log_event
from app.orchestrator import handle_chat
from app.privacy import PrivateDataInQuery, check_public_query
from app.schemas import AgentOutput, RoutePlan, Task, ToolResult
from app.tools.knowledge import WebSearchArgs, build_knowledge_tools
from tests.fakes import (
    ADQ_CHUNK_ID,
    ADQ_QUOTE,
    FakeEmbedder,
    FakeSearch,
    FrozenClock,
    ScriptedLLM,
    authorized,
    claim,
    final_turn,
    kout,
    make_deps,
    tool_call_turn,
    verdict,
)

# valores que só existem nos dados do cliente: não podem sair para busca, embedding nem logs
CANARIES = ["cliente1988", "T-1988-01", "R-1988-001", "1.250,00", "1250.00", "89.90"]
MESSAGE = "Minha maquininha não conecta, o que faço? Sou o cliente1988."


def _ctx(user_id: str = "cliente1988") -> RequestContext:
    return RequestContext(request_id="r", user_id=user_id, clock=FrozenClock(), budget=Budget())


@pytest.mark.parametrize(
    "query",
    [
        "erro ADQ 1-19 na Get Clássica",
        "previsão do tempo em Porto Alegre amanhã",
        "cotação do euro hoje",
        "o que significa o código de recusa 51",
    ],
)
def test_public_queries_pass(query: str) -> None:
    check_public_query(query, _ctx())


@pytest.mark.parametrize(
    ("query", "reason"),
    [
        ("vendas do cliente1988", "request_private_value"),
        ("contato fulano@loja.com.br", "email"),
        ("CPF 123.456.789-09", "document"),
        ("antecipar R$ 1.250,00", "amount"),
        ("pedido 1234567890", "long_number"),
        ("terminal T-2000-02 offline", "record_id"),
    ],
)
def test_private_data_is_refused_with_a_category_only(query: str, reason: str) -> None:
    with pytest.raises(PrivateDataInQuery) as raised:
        check_public_query(query, _ctx())
    assert raised.value.reason == reason


def test_values_returned_by_customer_tools_become_private_for_the_request() -> None:
    ctx = _ctx()
    ctx.tool_results.append(
        ToolResult(
            status="ok",
            data={"terminal": {"id": "T-9999-01", "last_transaction": {"amount": "1250.00"}}},
            evidence=[{"id": "diag", "type": "customer_record"}],  # type: ignore[list-item]
        )
    )
    for query in ("status T-9999-01", "venda de 1.250,00", "venda de 1250,00"):
        with pytest.raises(PrivateDataInQuery):
            check_public_query(query, ctx)


async def test_canaries_never_leave_through_search_embeddings_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Fluxo completo com o modelo tentando vazar dados: as consultas privadas são recusadas
    antes do provedor, e nada privado aparece nos logs."""
    turns = [
        final_turn(
            RoutePlan(
                workflow="support_then_knowledge",
                tasks=[
                    Task(agent="support", question="diagnóstico", diagnostic_goal="connectivity"),
                    Task(agent="knowledge", question="Como resolver o erro ADQ 1-19?"),
                ],
            )
        ),
        tool_call_turn("get_terminal_diagnostics"),
        final_turn(AgentOutput(answer="Sua maquininha está offline (ADQ 1-19).")),
        tool_call_turn("web_search", query="cliente1988 terminal T-1988-01 offline"),
        tool_call_turn("retrieve_getnet_knowledge", query="venda de R$ 89,90 recusada T-1988-01"),
        tool_call_turn("retrieve_getnet_knowledge", query="erro ADQ 1-19"),
        final_turn(kout("Verifique o Wi-Fi.", claim(ADQ_CHUNK_ID, ADQ_QUOTE))),
        final_turn(verdict(True)),
    ]
    search = FakeSearch()
    deps = await make_deps(tmp_path, ScriptedLLM(turns), search=search)
    embedder = deps.index._embedder  # type: ignore[attr-defined]
    assert isinstance(embedder, FakeEmbedder)
    seeded = len(embedder.calls)
    with caplog.at_level(logging.INFO, logger="getnet.support"):
        response = await handle_chat(authorized(MESSAGE), deps)
    assert response.status == "answered"
    assert search.queries == []  # a única busca web pedida levava dados do cliente
    sent = [text for call in embedder.calls[seeded:] for text in call]
    assert sent == ["erro ADQ 1-19"]
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "public_query_refused" in logs
    for canary in [*CANARIES, MESSAGE]:
        assert canary not in logs
        assert all(canary not in query for query in sent)


def test_log_fields_outside_the_allowlist_are_dropped(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="getnet.support"):
        log_event(
            "x",
            request_id="req_1",
            message="texto do cliente",
            user_id="cliente1988",
            payload={"amount": "1250.00"},
            detail="token for cliente1988 with R$ 1.250,00",
        )
    record = json.loads(caplog.records[-1].getMessage())
    assert record["request_id"] == "req_1"
    assert record["dropped_fields"] == ["message", "payload", "user_id"]
    assert "cliente1988" not in json.dumps(record) and "1.250" not in json.dumps(record)


async def test_request_completed_aggregates_usage_including_embeddings(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    turns = [
        final_turn(
            RoutePlan(
                workflow="knowledge",
                tasks=[Task(agent="knowledge", question="ADQ 1-19?", source_scope="getnet")],
            )
        ),
        tool_call_turn("retrieve_getnet_knowledge", query="erro ADQ 1-19"),
        tool_call_turn("retrieve_getnet_knowledge", query="ADQ 1-19 conexão"),
        final_turn(kout("Verifique o Wi-Fi.", claim(ADQ_CHUNK_ID, ADQ_QUOTE))),
        final_turn(verdict(True)),
    ]
    deps = await make_deps(tmp_path, ScriptedLLM(turns), max_embedding_calls=1)
    with caplog.at_level(logging.INFO, logger="getnet.support"):
        await handle_chat(authorized("ADQ 1-19?"), deps)
    events = [json.loads(r.getMessage()) for r in caplog.records]
    done = next(e for e in events if e["event"] == "request_completed")
    assert done["embedding_calls"] == 1  # a segunda consulta estourou o limite de embeddings
    assert done["web_searches"] == 0
    assert done["llm_calls"] == 5 and done["tool_calls"] == 2
    assert set(done["token_usage"]) == {"prompt", "completion"}


def _client(handler: Any) -> OpenAIClient:
    sdk = openai.AsyncOpenAI(
        api_key="sk-test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return OpenAIClient("sk-test", "model-x", client=sdk, retry_delay=0.0, timeout=30.0)


async def test_permanent_quota_error_fails_without_retry() -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        error = {"message": "quota", "type": "insufficient_quota", "code": "insufficient_quota"}
        return httpx.Response(429, json={"error": error})

    with pytest.raises(ProviderUnavailable, match="insufficient_quota"):
        await _client(handler).chat(
            [{"role": "user", "content": "x"}], tools=None, output_model=None, budget=Budget()
        )
    assert len(calls) == 1


async def test_transient_error_is_retried_only_if_the_deadline_allows() -> None:
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={"error": {"message": "busy", "type": "server_error"}})

    with pytest.raises(ProviderUnavailable):
        await _client(handler).chat(
            [{"role": "user", "content": "x"}], tools=None, output_model=None, budget=Budget()
        )
    assert len(calls) == 2
    calls.clear()
    with pytest.raises(ProviderUnavailable):
        await _client(handler).chat(
            [{"role": "user", "content": "x"}],
            tools=None,
            output_model=None,
            budget=Budget(deadline_seconds=0.5),  # sem prazo para pausa e nova chamada
        )
    assert len(calls) == 1


async def test_each_llm_call_is_bounded_by_the_remaining_deadline() -> None:
    seen: dict[str, Any] = {}

    class Recorder:
        async def parse(self, **kwargs: Any) -> Any:
            seen.update(kwargs)
            raise openai.APIConnectionError(request=httpx.Request("POST", "http://x"))

    client = OpenAIClient("sk", "m", retry_delay=0.0, timeout=30.0)
    client._client = type("C", (), {"chat": type("Ch", (), {"completions": Recorder()})()})()  # type: ignore[assignment]
    with pytest.raises(ProviderUnavailable):
        await client.chat(
            [{"role": "user", "content": "x"}],
            tools=None,
            output_model=None,
            budget=Budget(deadline_seconds=5.0),
        )
    assert 0 < seen["timeout"] <= 5.0


async def test_slow_search_is_cut_by_the_remaining_deadline(tmp_path: Path) -> None:
    class SlowSearch(FakeSearch):
        async def search(self, query: str, max_results: int = 5) -> Any:
            await asyncio.sleep(5)
            return []

    deps = await make_deps(tmp_path, ScriptedLLM([]), search=SlowSearch())
    web = next(t for t in build_knowledge_tools(deps.index, deps.search) if t.name == "web_search")
    ctx = RequestContext(
        request_id="r", user_id="c", clock=FrozenClock(), budget=Budget(deadline_seconds=0.2)
    )
    result = await web.run(WebSearchArgs(query="cotação do euro"), ctx)
    assert result.status == "error" and "timeout" in (result.error or "")


async def _get(deps: Deps, url: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=create_app(deps))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(url)


async def test_ready_reports_each_check(tmp_path: Path) -> None:
    deps = await make_deps(tmp_path, ScriptedLLM([]))
    ok = await _get(deps, "/ready")
    assert ok.status_code == 200
    assert ok.json() == {
        "status": "ready",
        "checks": {"index": "ready", "state": "ok", "lifecycle": "running"},
    }
    empty = await make_deps(tmp_path / "empty", ScriptedLLM([]), seed=False)
    missing = await _get(empty, "/ready")
    assert missing.status_code == 503
    assert missing.json()["checks"]["index"] == "missing"
    assert (await _get(empty, "/health")).status_code == 200  # liveness não depende do índice


async def test_shutdown_drains_and_closes_provider_clients(tmp_path: Path) -> None:
    """SIGTERM no uvicorn executa o fim do lifespan: sai da prontidão e fecha os clientes."""
    closed: list[str] = []

    class ClosingSearch(FakeSearch):
        async def aclose(self) -> None:
            closed.append("search")

    class ClosingLLM(ScriptedLLM):
        async def aclose(self) -> None:
            closed.append("llm")

    deps = await make_deps(tmp_path, ClosingLLM([]), search=ClosingSearch())
    app = create_app(deps)
    async with app.router.lifespan_context(app):
        assert app.state.draining is False
    assert app.state.draining is True
    assert sorted(closed) == ["llm", "search"]


class _GateLLM(ScriptedLLM):
    """Segura a primeira chamada até o teste liberar: simula uma requisição em andamento."""

    def __init__(self, turns: list[LLMTurn]) -> None:
        super().__init__(turns)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def chat(self, *args: Any, **kwargs: Any) -> LLMTurn:
        self.entered.set()
        await self.release.wait()
        return await super().chat(*args, **kwargs)


async def test_saturation_gets_an_immediate_predictable_503(tmp_path: Path) -> None:
    llm = _GateLLM([final_turn(RoutePlan(workflow="clarify", clarification="Qual terminal?"))])
    deps = await make_deps(tmp_path, llm, max_concurrent_chats=1)
    assert deps.dev_issuer is not None
    headers = {"Authorization": f"Bearer {deps.dev_issuer.issue('user-a')}"}
    transport = httpx.ASGITransport(app=create_app(deps))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = asyncio.create_task(client.post("/chat", json={"message": "oi"}, headers=headers))
        await asyncio.wait_for(llm.entered.wait(), 5)
        second = await client.post("/chat", json={"message": "oi"}, headers=headers)
        llm.release.set()
        done = await first
    assert second.status_code == 503
    assert second.json() == {"error": "overloaded"}
    assert second.headers["Retry-After"] == "2"
    assert done.status_code == 200
