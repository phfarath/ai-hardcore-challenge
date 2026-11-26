"""Memória: follow-up com pronome vira pergunta autocontida; só o histórico desta conversa entra."""

import json
from pathlib import Path
from typing import Any

import httpx

from app.deps import Deps
from app.llm import LLMTurn
from app.main import create_app
from app.prompts import ROUTER_SYSTEM
from app.schemas import RoutePlan, Task
from tests.fakes import (
    SMART_CHUNK_ID,
    SMART_QUOTE,
    ScriptedLLM,
    claim,
    final_turn,
    kout,
    make_deps,
    tool_call_turn,
    verdict,
)

FIRST = "O que é a Get Smart?"
FOLLOW_UP = "E ela tem impressora?"
RESOLVED = "A Get Smart tem impressora?"


async def _post(deps: Deps, payload: dict[str, Any], sub: str = "user-b") -> dict[str, Any]:
    assert deps.dev_issuer is not None
    headers = {"Authorization": f"Bearer {deps.dev_issuer.issue(sub)}"}
    transport = httpx.ASGITransport(app=create_app(deps))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/chat", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _knowledge_turns(question: str, statement: str) -> list[LLMTurn]:
    return [
        final_turn(
            RoutePlan(
                workflow="knowledge",
                tasks=[Task(agent="knowledge", question=question, source_scope="getnet")],
            )
        ),
        tool_call_turn("retrieve_getnet_knowledge", query=question),
        final_turn(kout(statement, claim(SMART_CHUNK_ID, SMART_QUOTE, statement=statement))),
        final_turn(verdict(True)),
    ]


def _has_history(messages: list[dict[str, str]]) -> bool:
    return any("Histórico recente" in (m.get("content") or "") for m in messages)


async def test_follow_up_with_pronoun_is_answered_from_the_self_contained_task(
    tmp_path: Path,
) -> None:
    deps = await make_deps(
        tmp_path, ScriptedLLM(_knowledge_turns(FIRST, "A Get Smart tem tela touch."))
    )
    first = await _post(deps, {"message": FIRST})
    assert first["status"] == "answered"

    llm = ScriptedLLM(_knowledge_turns(RESOLVED, "A Get Smart tem impressora térmica integrada."))
    deps.llm = llm
    second = await _post(deps, {"message": FOLLOW_UP, "conversation_id": first["conversation_id"]})
    assert second["status"] == "answered"

    router = llm.calls[0]
    assert _has_history(router) and "Get Smart" in router[1]["content"]
    assert router[-1]["content"] == FOLLOW_UP  # a classificação é da mensagem nova
    assert llm.calls[1][-1]["content"].startswith(RESOLVED)  # o Knowledge só vê a autocontida
    judged = json.loads(llm.calls[3][-1]["content"])
    assert judged["mensagem_original_do_cliente"] == RESOLVED  # nada de "ela" sem referente
    assert "e a taxa dela?" in ROUTER_SYSTEM  # o contrato do Router traz o exemplo


async def test_history_of_another_conversation_or_principal_never_reaches_the_router(
    tmp_path: Path,
) -> None:
    deps = await make_deps(
        tmp_path, ScriptedLLM(_knowledge_turns(FIRST, "A Get Smart tem tela touch."))
    )
    await _post(deps, {"message": FIRST})

    for sub in ("user-b", "user-a"):  # nova conversa do mesmo dono e de outro principal
        llm = ScriptedLLM(_knowledge_turns(RESOLVED, "A Get Smart tem impressora térmica."))
        deps.llm = llm
        body = await _post(deps, {"message": RESOLVED}, sub=sub)
        assert body["status"] == "answered"
        assert not _has_history(llm.calls[0]), sub
        assert all(FIRST not in (m.get("content") or "") for c in llm.calls for m in c), sub
