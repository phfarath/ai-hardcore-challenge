"""T04: conversa durável, esclarecimento em dois turnos, idempotência e concorrência."""

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from app.auth import DEV_ISSUER, Authenticator, JsonAccessRepository, StaticKey
from app.context import Budget, ProviderUnavailable
from app.conversations import parse_choice, request_hash
from app.deps import Deps
from app.llm import LLMTurn, ToolSpec
from app.main import create_app
from app.policies import AuthorizedChat
from app.repositories import Terminal
from app.schemas import AgentOutput, RoutePlan, Task
from app.state import ConversationConflict, TurnResult, TurnStart
from tests.fakes import (
    DEV_SUB,
    FrozenClock,
    ScriptedLLM,
    dev_auth,
    final_turn,
    make_deps,
    principal_for,
    tool_call_turn,
)

YESTERDAY = "2026-11-06"
WHICH = "Minha maquininha está com problema?"


def _plan(workflow: str = "support") -> LLMTurn:
    return final_turn(RoutePlan(workflow=workflow, tasks=[Task(agent="support", question="q")]))  # type: ignore[arg-type]


def _out(text: str) -> LLMTurn:
    return final_turn(AgentOutput(answer=text, evidence_ids=[]))


def _ask_terminal_turns() -> list[LLMTurn]:
    """Router → Support pede diagnóstico sem terminal → ferramenta pede esclarecimento."""
    return [
        _plan(),
        tool_call_turn("get_terminal_diagnostics", terminal_id=None),
        _out("Qual terminal?"),
    ]


def _diagnosis_turns() -> list[LLMTurn]:
    """Só o Support: na retomada o plano salvo é reaproveitado, sem Router."""
    return [tool_call_turn("get_terminal_diagnostics", terminal_id=None), _out("Terminal online.")]


def _receivables_turns() -> list[LLMTurn]:
    return [
        _plan(),
        tool_call_turn("get_receivables", sales_date=YESTERDAY),
        _out("Explicação."),
    ]


async def _post(
    deps: Deps,
    payload: dict[str, Any],
    *,
    sub: str = "user-b",
    key: str | None = None,
) -> httpx.Response:
    assert deps.dev_issuer is not None
    headers = {"Authorization": f"Bearer {deps.dev_issuer.issue(sub)}"}
    if key:
        headers["Idempotency-Key"] = key
    transport = httpx.ASGITransport(app=create_app(deps))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/chat", json=payload, headers=headers)


async def _get(deps: Deps, conversation_id: str, sub: str) -> httpx.Response:
    assert deps.dev_issuer is not None
    headers = {"Authorization": f"Bearer {deps.dev_issuer.issue(sub)}"}
    transport = httpx.ASGITransport(app=create_app(deps))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(f"/conversations/{conversation_id}", headers=headers)


async def _first_turn(tmp_path: Path, clock: FrozenClock | None = None) -> tuple[Deps, str]:
    deps = await make_deps(tmp_path, ScriptedLLM(_ask_terminal_turns()), clock=clock)
    body = (await _post(deps, {"message": WHICH})).json()
    assert body["status"] == "needs_clarification"
    assert "1. T-2000-01\n2. T-2000-02" in body["answer"]
    return deps, body["conversation_id"]


async def test_second_turn_selects_the_listed_terminal_after_restart(tmp_path: Path) -> None:
    _, conversation_id = await _first_turn(tmp_path)

    llm = ScriptedLLM(_diagnosis_turns())
    restarted = await make_deps(tmp_path, llm)  # novo processo: mesmo arquivo de estado
    body = (
        await _post(restarted, {"message": "o segundo", "conversation_id": conversation_id})
    ).json()

    assert body["status"] == "answered"
    assert body["conversation_id"] == conversation_id
    assert body["agents_used"] == ["customer_support"]  # o Router não roda de novo
    assert llm.output_models[0] is AgentOutput
    tool_message = next(m for m in llm.calls[1] if m.get("role") == "tool")
    assert "T-2000-02" in tool_message["content"]
    assert "T-2000-01" not in tool_message["content"]
    history = (await _get(restarted, conversation_id, "user-b")).json()
    assert [m["role"] for m in history["messages"]] == ["user", "assistant"] * 2
    assert history["pending"] is None


async def test_choice_out_of_range_asks_again_without_llm(tmp_path: Path) -> None:
    deps, conversation_id = await _first_turn(tmp_path)
    deps.llm = ScriptedLLM([])
    body = (await _post(deps, {"message": "o quarto", "conversation_id": conversation_id})).json()
    assert body["status"] == "needs_clarification"
    assert "1. T-2000-01\n2. T-2000-02" in body["answer"]

    deps.llm = ScriptedLLM(_diagnosis_turns())
    body = (await _post(deps, {"message": "2", "conversation_id": conversation_id})).json()
    assert body["status"] == "answered"


class _ChangedTerminals:
    """Repositório cujos terminais mudaram desde a pergunta (um saiu, outro entrou)."""

    def __init__(self, base: Any) -> None:
        self._base = base

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def terminals_for(self, customer_id: str, now: Any) -> list[Terminal]:
        terminals = self._base.terminals_for(customer_id, now)
        return [terminals[1], terminals[0].model_copy(update={"id": "T-2000-09"})]


async def test_position_is_refused_when_the_list_changed(tmp_path: Path) -> None:
    deps, conversation_id = await _first_turn(tmp_path)
    deps.llm = ScriptedLLM([])
    deps.repo = _ChangedTerminals(deps.repo)
    body = (await _post(deps, {"message": "o segundo", "conversation_id": conversation_id})).json()
    assert body["status"] == "needs_clarification"
    assert "mudou" in body["answer"]
    assert "1. T-2000-02\n2. T-2000-09" in body["answer"]


async def test_new_question_clears_the_pending_choice_and_reaches_router_with_history(
    tmp_path: Path,
) -> None:
    deps, conversation_id = await _first_turn(tmp_path)
    llm = ScriptedLLM(_receivables_turns())
    deps.llm = llm
    body = (
        await _post(
            deps,
            {"message": "Quando recebo as vendas de ontem?", "conversation_id": conversation_id},
        )
    ).json()
    assert "3.200,00" in body["answer"]
    router_messages = llm.calls[0]
    assert any("Histórico recente" in m["content"] for m in router_messages[1:-1])
    assert router_messages[-1]["content"] == "Quando recebo as vendas de ontem?"


async def test_conversation_of_another_principal_is_not_found(tmp_path: Path) -> None:
    deps, conversation_id = await _first_turn(tmp_path)
    deps.llm = ScriptedLLM([])
    response = await _post(
        deps, {"message": "o segundo", "conversation_id": conversation_id}, sub="user-a"
    )
    assert response.status_code == 404
    assert (await _get(deps, conversation_id, "user-a")).status_code == 404
    assert (await _get(deps, conversation_id, "user-b")).status_code == 200
    unknown = f"conv_{'0' * 32}"
    assert (await _get(deps, unknown, "user-b")).status_code == 404
    assert deps.llm.calls == []  # type: ignore[attr-defined]


async def test_revoked_access_does_not_resume_the_conversation(tmp_path: Path) -> None:
    deps, conversation_id = await _first_turn(tmp_path)
    revoked = JsonAccessRepository()
    revoked.profiles = {
        **revoked.profiles,
        "user-b": {**revoked.profiles["user-b"], "customers": []},
    }
    issuer = dev_auth()[1]
    deps.auth = Authenticator(
        StaticKey(issuer.public_key),
        issuer=DEV_ISSUER,
        audience="getnet-support-api",
        access=revoked,
    )
    deps.llm = ScriptedLLM([])
    response = await _post(deps, {"message": "o segundo", "conversation_id": conversation_id})
    assert response.status_code == 403
    assert deps.llm.calls == []  # type: ignore[attr-defined]


async def test_same_idempotency_key_returns_the_saved_result(tmp_path: Path) -> None:
    llm = ScriptedLLM(_receivables_turns())  # só um turno roteirizado: repetir chamaria o LLM
    deps = await make_deps(tmp_path, llm)
    payload = {"message": "Quando recebo as vendas de ontem?"}
    first = await _post(deps, payload, key="k-1")
    again = await _post(deps, payload, key="k-1")
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()
    assert len(llm.calls) == 3

    other = await _post(deps, {"message": "E as de anteontem?"}, key="k-1")
    assert other.status_code == 409
    assert other.json() == {"error": "idempotency_key_reused"}


async def test_failed_run_is_retried_with_the_same_key(tmp_path: Path) -> None:
    class Down:
        async def chat(self, *args: Any, **kwargs: Any) -> LLMTurn:
            raise ProviderUnavailable("down")

    deps = await make_deps(tmp_path, Down())
    payload = {"message": "Quando recebo as vendas de ontem?"}
    assert (await _post(deps, payload, key="k-2")).status_code == 503

    deps.llm = ScriptedLLM(_receivables_turns())
    retry = await _post(deps, payload, key="k-2")
    assert retry.status_code == 200
    run = deps.state.find_run("getnet", DEV_SUB["cliente2000"], "k-2")
    assert run is not None and run.state == "completed" and run.attempts == 2


async def test_abandoned_run_is_reexecuted_after_its_lease_expires(tmp_path: Path) -> None:
    """Processo caiu depois de abrir o turno: a lease vence e a mesma chave executa de novo."""
    clock = FrozenClock()
    deps = await make_deps(tmp_path, ScriptedLLM(_receivables_turns()), clock=clock)
    message = "Quando recebo as vendas de ontem?"
    chat = AuthorizedChat(
        message=message, customer_id="cliente2000", principal=principal_for("cliente2000")
    )
    now = clock.now().timestamp()
    deps.state.start_turn(
        None,
        TurnStart(
            tenant_id="getnet",
            owner_sub="user-b",
            customer_id="cliente2000",
            request_hash=request_hash(chat, None),
            idempotency_key="k-3",
            now=now,
            lease_until=now + 45,
            expires_at=now + 3600,
        ),
    )
    assert (await _post(deps, {"message": message}, key="k-3")).status_code == 409  # em andamento
    clock.advance(seconds=60)
    response = await _post(deps, {"message": message}, key="k-3")
    assert response.status_code == 200
    assert "3.200,00" in response.json()["answer"]


class _GatedLLM:
    """Segura a primeira chamada até o teste liberar: simula um turno em andamento."""

    def __init__(self, inner: ScriptedLLM) -> None:
        self.inner = inner
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolSpec] | None,
        output_model: type[BaseModel] | None,
        budget: Budget,
    ) -> LLMTurn:
        self.entered.set()
        await self.release.wait()
        return await self.inner.chat(
            messages, tools=tools, output_model=output_model, budget=budget
        )


async def test_concurrent_turns_on_one_conversation_do_not_lose_updates(tmp_path: Path) -> None:
    deps, conversation_id = await _first_turn(tmp_path)
    gated = _GatedLLM(ScriptedLLM(_diagnosis_turns()))
    deps.llm = gated
    turn = {"message": "o segundo", "conversation_id": conversation_id}

    first = asyncio.create_task(_post(deps, turn))
    await gated.entered.wait()
    competing = await _post(deps, {"message": "o primeiro", "conversation_id": conversation_id})
    gated.release.set()
    done = await first

    assert competing.status_code == 409
    assert competing.json() == {"error": "conversation_busy"}
    assert done.status_code == 200
    history = (await _get(deps, conversation_id, "user-b")).json()["messages"]
    assert [m["seq"] for m in history] == [1, 2, 3, 4]
    assert history[2]["content"] == "o segundo"


async def test_stale_version_is_never_written_over(tmp_path: Path) -> None:
    deps = await make_deps(tmp_path, ScriptedLLM([]))
    start = TurnStart(
        tenant_id="getnet",
        owner_sub="user-b",
        customer_id="cliente2000",
        request_hash="h",
        idempotency_key=None,
        now=1.0,
        lease_until=2.0,
        expires_at=9e9,
    )
    conversation, run = deps.state.start_turn(None, start)
    result = TurnResult(
        user_message="u",
        assistant_message="a",
        response={},
        pending=None,
        llm_calls=0,
        tool_calls=0,
        now=3.0,
        expires_at=9e9,
    )
    deps.state.complete_turn(run.id, conversation.id, conversation.version, result)
    with pytest.raises(ConversationConflict):
        deps.state.complete_turn(run.id, conversation.id, conversation.version, result)
    assert len(deps.state.messages(conversation.id)) == 2


async def test_expired_conversation_is_purged_and_not_found(tmp_path: Path) -> None:
    clock = FrozenClock()
    deps, conversation_id = await _first_turn(tmp_path, clock)
    clock.advance(hours=deps.settings.conversation_retention_hours + 1)
    deps.llm = ScriptedLLM([])
    response = await _post(deps, {"message": "o segundo", "conversation_id": conversation_id})
    assert response.status_code == 404
    assert deps.state.get_conversation(conversation_id) is None
    assert deps.state.messages(conversation_id) == []


async def test_expired_choice_is_not_resumed(tmp_path: Path) -> None:
    clock = FrozenClock()
    deps, conversation_id = await _first_turn(tmp_path, clock)
    clock.advance(minutes=deps.settings.pending_choice_minutes + 1)
    llm = ScriptedLLM([final_turn(RoutePlan(workflow="clarify", clarification="Pode detalhar?"))])
    deps.llm = llm
    body = (await _post(deps, {"message": "o segundo", "conversation_id": conversation_id})).json()
    assert body["answer"] == "Pode detalhar?"
    assert llm.output_models[0] is RoutePlan  # pedido novo: passa pelo Router


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("o segundo", (True, "T-2000-02")),
        ("Segunda", (True, "T-2000-02")),
        ("2", (True, "T-2000-02")),
        ("the first one", (True, "T-2000-01")),
        ("o último", (True, "T-2000-02")),
        ("t-2000-01 por favor", (True, "T-2000-01")),
        ("o terceiro", (True, None)),
        ("Quando recebo as vendas de ontem? Preciso saber hoje mesmo, é urgente.", (False, None)),
        ("obrigado", (False, None)),
    ],
)
def test_parse_choice(message: str, expected: tuple[bool, str | None]) -> None:
    assert parse_choice(message, ["T-2000-01", "T-2000-02"]) == expected
