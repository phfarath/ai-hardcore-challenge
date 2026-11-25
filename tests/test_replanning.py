"""T07: decisões depois de observar resultados, dentro do orçamento e sem ampliar permissões."""

from pathlib import Path
from typing import Any

import httpx
import pytest

from app.agents.router import PlanError, validate_plan
from app.deps import Deps
from app.llm import LLMTurn
from app.main import create_app
from app.orchestrator import handle_chat, run_chat
from app.policies import RECEIVABLES_READ, TERMINALS_READ
from app.schemas import AgentOutput, RoutePlan, Task
from tests.fakes import (
    ADQ_CHUNK_ID,
    ADQ_QUOTE,
    SMART_CHUNK_ID,
    SMART_QUOTE,
    FakeSearch,
    ScriptedLLM,
    authorized,
    claim,
    final_turn,
    kout,
    make_deps,
    missing,
    tool_call_turn,
    verdict,
)

QUESTION = "O que é a Get Smart e como resolvo o erro ADQ 1-19?"
GAP = "como resolver o erro ADQ 1-19"


def _knowledge_plan(question: str = QUESTION) -> LLMTurn:
    return final_turn(
        RoutePlan(
            workflow="knowledge",
            tasks=[Task(agent="knowledge", question=question, source_scope="getnet")],
        )
    )


def _first_pass_with_gap() -> list[LLMTurn]:
    """Knowledge comprova a parte da Get Smart; o verificador aponta a lacuna do erro ADQ."""
    return [
        tool_call_turn("retrieve_getnet_knowledge", query="Get Smart"),
        final_turn(
            kout(
                "A Get Smart tem tela touch.",
                claim(SMART_CHUNK_ID, SMART_QUOTE, statement="A Get Smart tem tela touch."),
                missing(GAP),
            )
        ),
        final_turn(verdict(True, uncovered=[GAP])),
    ]


def _repair_turns(chunk_id: str, quote: str, statement: str) -> list[LLMTurn]:
    return [
        tool_call_turn("retrieve_getnet_knowledge", query="erro ADQ 1-19"),
        final_turn(kout(statement, claim(chunk_id, quote, statement=statement))),
        final_turn(verdict(True)),
    ]


async def test_gap_seen_by_the_verifier_triggers_one_repair_that_covers_it(
    tmp_path: Path,
) -> None:
    """Cenário 1: evidência nova (a lacuna) muda o plano; a reparação traz fonte nova."""
    turns = [
        _knowledge_plan(),
        *_first_pass_with_gap(),
        *_repair_turns(ADQ_CHUNK_ID, ADQ_QUOTE, "Verifique o Wi-Fi ou o chip e reinicie."),
    ]
    llm = ScriptedLLM(turns)
    deps = await make_deps(tmp_path, llm, max_replans=1)
    response = await handle_chat(authorized(QUESTION), deps)
    assert response.plan_changes == ["knowledge_repair"]
    assert response.status == "answered"
    assert "tela touch" in response.answer and "reinicie" in response.answer
    assert "Não localizei evidência" not in response.answer  # a lacuna foi coberta
    assert response.answer.count("Com base nas fontes consultadas") == 1
    assert {s.id for s in response.sources} == {SMART_CHUNK_ID, ADQ_CHUNK_ID}
    repair_prompt = llm.calls[4][-1]["content"]
    assert GAP in repair_prompt and "outra formulação" in repair_prompt


async def test_diagnosed_error_code_adds_official_guidance_to_a_support_only_plan(
    tmp_path: Path,
) -> None:
    """Cenário 2: o diagnóstico mostra um código que o plano não ia explicar; entra a busca
    de orientação, só em fontes oficiais, mesmo com busca web configurada."""
    turns = [
        final_turn(
            RoutePlan(
                workflow="support",
                tasks=[Task(agent="support", question="Como está a minha maquininha?")],
            )
        ),
        tool_call_turn("get_terminal_diagnostics"),
        final_turn(AgentOutput(answer="Sua maquininha está offline (ADQ 1-19).")),
        tool_call_turn("retrieve_getnet_knowledge", query="erro ADQ 1-19"),
        final_turn(kout("Verifique o Wi-Fi.", claim(ADQ_CHUNK_ID, ADQ_QUOTE))),
        final_turn(verdict(True)),
    ]
    llm = ScriptedLLM(turns)
    deps = await make_deps(tmp_path, llm, search=FakeSearch(), max_replans=1)
    response = await handle_chat(authorized("Como está a minha maquininha?"), deps)
    assert response.plan_changes == ["guidance_added"]
    assert response.status == "answered"
    assert response.agents_used == ["router", "customer_support", "knowledge"]
    assert ADQ_CHUNK_ID in {s.id for s in response.sources}
    knowledge_tools = {t.name for t in llm.tools_seen[3] or []}
    assert knowledge_tools == {"retrieve_getnet_knowledge"}  # nada de web numa orientação Getnet
    guidance_prompt = llm.calls[3][-1]["content"]
    assert "ADQ 1-19" in guidance_prompt and "cliente1988" not in guidance_prompt


async def test_repair_without_new_evidence_ends_safely_with_what_was_proven(
    tmp_path: Path,
) -> None:
    """Sem progresso: a mesma fonte de novo não muda a resposta, e não há segunda reparação."""
    turns = [
        _knowledge_plan(),
        *_first_pass_with_gap(),
        *_repair_turns(SMART_CHUNK_ID, SMART_QUOTE, "A Get Smart tem tela touch."),
    ]
    llm = ScriptedLLM(turns)
    deps = await make_deps(tmp_path, llm, max_replans=3)
    response = await handle_chat(authorized(QUESTION), deps)
    assert response.plan_changes == ["knowledge_repair:no_progress"]
    assert response.status == "partial"
    assert response.answer.count("tela touch") == 1
    assert f"Não localizei evidência suficiente nas fontes consultadas para: {GAP}." in (
        response.answer
    )
    assert len(llm.calls) == 7  # roteiro esgotado: uma reparação só, mesmo com limite maior


async def test_nothing_found_twice_stays_insufficient(tmp_path: Path) -> None:
    question = "Qual é a taxa da maquininha Getnet Quantum 9000?"
    turns = [
        _knowledge_plan(question),
        tool_call_turn("retrieve_getnet_knowledge", query="Quantum 9000"),
        final_turn(kout("Não encontrei.", missing("taxa da Quantum 9000"))),
        tool_call_turn("retrieve_getnet_knowledge", query="Getnet Quantum 9000 taxa"),
        final_turn(kout("Não encontrei.", missing("taxa da Quantum 9000"))),
    ]
    deps = await make_deps(tmp_path, ScriptedLLM(turns), max_replans=1)
    response = await handle_chat(authorized(question), deps)
    assert response.status == "insufficient_evidence"
    assert response.plan_changes == ["knowledge_repair:no_progress"]
    assert response.sources == []


async def test_no_repair_without_budget_left_to_verify_it(tmp_path: Path) -> None:
    """Reserva para encerrar: com menos chamadas do que a reparação precisa, ela não começa."""
    turns = [_knowledge_plan(), *_first_pass_with_gap()]
    llm = ScriptedLLM(turns)
    deps = await make_deps(tmp_path, llm, max_llm_calls=6, max_replans=1)
    response = await handle_chat(authorized(QUESTION), deps)
    assert response.plan_changes == []
    assert response.status == "partial"
    assert len(llm.calls) == 4


async def test_failed_repair_keeps_the_first_result_without_a_failure_note(
    tmp_path: Path,
) -> None:
    """Retry de provedor é do cliente LLM; uma estratégia nova que falha não apaga o comprovado."""
    turns = [
        _knowledge_plan(),
        *_first_pass_with_gap(),
        tool_call_turn("retrieve_getnet_knowledge", query="erro ADQ 1-19"),
        final_turn(kout("Reinicie.", claim(ADQ_CHUNK_ID, ADQ_QUOTE))),
        LLMTurn(),  # verificador sem saída estruturada → VerificationUnavailable
    ]
    deps = await make_deps(tmp_path, ScriptedLLM(turns), max_replans=1)
    response = await handle_chat(authorized(QUESTION), deps)
    assert response.plan_changes == ["knowledge_repair:failed"]
    assert response.status == "partial"
    assert "tela touch" in response.answer
    assert "Pendência: a parte de" not in response.answer
    assert "Reinicie" not in response.answer


async def test_resumed_plan_does_not_reset_the_replan_counter(tmp_path: Path) -> None:
    """Replay: o plano salvo retomado carrega o que já foi gasto e não ganha nova reparação."""
    plan = RoutePlan(
        workflow="knowledge",
        tasks=[Task(agent="knowledge", question=QUESTION, source_scope="getnet")],
    )
    llm = ScriptedLLM(_first_pass_with_gap())
    deps = await make_deps(tmp_path, llm, max_replans=1)
    outcome = await run_chat(authorized(QUESTION), deps, plan=plan, replans_used=1)
    assert outcome.response.plan_changes == []
    assert outcome.replans_used == 1
    assert len(llm.calls) == 3


async def _api(
    deps: Deps,
    method: str,
    url: str,
    sub: str,
    json: dict[str, Any] | None = None,
    scopes: list[str] | None = None,
) -> httpx.Response:
    assert deps.dev_issuer is not None
    headers = {"Authorization": f"Bearer {deps.dev_issuer.issue(sub, scopes=scopes)}"}
    transport = httpx.ASGITransport(app=create_app(deps))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, url, json=json, headers=headers)


async def test_resumed_plan_is_rechecked_against_the_current_scopes(tmp_path: Path) -> None:
    """Replay não amplia permissão: o plano salvo é retomado com as ferramentas que o token
    atual permite, e o contador de replanejamento salvo vai junto na pendência."""
    turns = [
        final_turn(RoutePlan(workflow="support", tasks=[Task(agent="support", question="q")])),
        tool_call_turn("get_terminal_diagnostics", terminal_id=None),
        final_turn(AgentOutput(answer="Qual terminal?")),
        final_turn(AgentOutput(answer="Não consigo consultar o terminal.")),
    ]
    llm = ScriptedLLM(turns)
    deps = await make_deps(tmp_path, llm, max_replans=1)
    first = await _api(
        deps, "POST", "/chat", "user-b", {"message": "Minha maquininha não conecta."}
    )
    body = first.json()
    assert body["status"] == "needs_clarification"
    conversation = deps.state.get_conversation(body["conversation_id"])
    assert conversation is not None and conversation.pending is not None
    assert conversation.pending["replans_used"] == 0
    narrowed = await _api(
        deps,
        "POST",
        "/chat",
        "user-b",
        {"message": "o segundo", "conversation_id": body["conversation_id"]},
        scopes=[RECEIVABLES_READ],
    )
    assert narrowed.status_code == 200
    assert {t.name for t in llm.tools_seen[-1] or []} == {"get_receivables"}
    assert "get_terminal_diagnostics" not in narrowed.json()["tools_used"]


def _human_plan() -> LLMTurn:
    return final_turn(RoutePlan(workflow="handoff"))


async def test_explicit_request_for_a_human_opens_a_case(tmp_path: Path) -> None:
    deps = await make_deps(tmp_path, ScriptedLLM([_human_plan()]))
    response = await _api(
        deps, "POST", "/chat", "user-a", {"message": "Quero falar com um atendente."}
    )
    body = response.json()
    assert body["status"] == "handoff_opened"
    assert body["run_status"] == "awaiting_operator"
    assert body["handoff_id"] and body["handoff_id"] in body["answer"]
    assert body["agents_used"] == ["router"]
    queue = (await _api(deps, "GET", "/operator/handoffs", "operator-1")).json()
    assert [h["reason"] for h in queue] == ["customer_requested_human"]


async def test_explicit_request_without_case_scope_is_only_guidance(tmp_path: Path) -> None:
    deps = await make_deps(tmp_path, ScriptedLLM([_human_plan()]))
    response = await _api(
        deps,
        "POST",
        "/chat",
        "user-a",
        {"message": "Quero falar com um atendente."},
        scopes=[RECEIVABLES_READ, TERMINALS_READ],
    )
    body = response.json()
    assert body["handoff_id"] is None
    assert "nenhum atendimento foi aberto" in body["answer"]
    assert deps.state.list_handoffs("getnet") == []


def test_handoff_plan_cannot_carry_tasks() -> None:
    with pytest.raises(PlanError):
        validate_plan(
            RoutePlan(workflow="handoff", tasks=[Task(agent="support", question="dados")])
        )


async def test_escalation_still_happens_after_a_repair_without_progress(tmp_path: Path) -> None:
    turns = [
        final_turn(
            RoutePlan(
                workflow="support_then_knowledge",
                tasks=[
                    Task(agent="support", question="diagnóstico"),
                    Task(agent="knowledge", question="orientação"),
                ],
            )
        ),
        tool_call_turn("get_terminal_diagnostics"),
        final_turn(AgentOutput(answer="Sua máquina está offline.")),
        final_turn(kout("Não sei.")),
        final_turn(kout("Ainda não sei.")),
    ]
    deps = await make_deps(tmp_path, ScriptedLLM(turns), max_replans=1)
    body = (
        await _api(deps, "POST", "/chat", "user-a", {"message": "Minha maquininha não conecta."})
    ).json()
    assert body["plan_changes"] == ["knowledge_repair:no_progress"]
    assert body["status"] == "handoff_opened"
    assert body["handoff_id"] is not None


async def test_repair_after_nothing_found_keeps_the_subject_header(tmp_path: Path) -> None:
    """e02: a primeira tentativa não sustentou nada; a reparação é a resposta inteira e precisa
    do assunto (o local) no cabeçalho."""
    question = "Previsão do tempo em Porto Alegre amanhã"
    turns = [
        _knowledge_plan(question),
        tool_call_turn("retrieve_getnet_knowledge", query="tempo Porto Alegre"),
        final_turn(kout("Não encontrei.", missing(question))),
        tool_call_turn("retrieve_getnet_knowledge", query="Porto Alegre previsão"),
        final_turn(
            kout(
                "Chuva.",
                claim(SMART_CHUNK_ID, SMART_QUOTE, statement="A fonte cita tela touch."),
                missing("temperatura"),
            )
        ),
        final_turn(verdict(True, uncovered=["temperatura"])),
    ]
    deps = await make_deps(tmp_path, ScriptedLLM(turns), max_replans=1)
    response = await handle_chat(authorized(question), deps)
    assert response.plan_changes == ["knowledge_repair"]
    assert response.status == "partial"
    assert response.answer.startswith("Com base nas fontes consultadas sobre: Previsão")
    assert "Porto Alegre" in response.answer


async def test_partial_repair_is_appended_without_a_second_header(tmp_path: Path) -> None:
    turns = [
        _knowledge_plan(),
        *_first_pass_with_gap(),
        tool_call_turn("retrieve_getnet_knowledge", query="erro ADQ 1-19"),
        final_turn(
            kout(
                "Reinicie.",
                claim(ADQ_CHUNK_ID, ADQ_QUOTE, statement="Verifique o Wi-Fi ou o chip."),
                missing("prazo de visita técnica"),
            )
        ),
        final_turn(verdict(True, uncovered=["prazo de visita técnica"])),
    ]
    deps = await make_deps(tmp_path, ScriptedLLM(turns), max_replans=1)
    response = await handle_chat(authorized(QUESTION), deps)
    assert response.status == "partial"
    assert response.answer.count("Com base nas fontes consultadas") == 1
    assert "tela touch" in response.answer and "Wi-Fi ou o chip" in response.answer
    assert "prazo de visita técnica" in response.answer


async def test_status_says_whether_a_case_was_opened_or_only_recommended(tmp_path: Path) -> None:
    """Caso real: `handoff_opened` + `handoff_id` + `awaiting_operator`. Sem escopo para abrir
    caso: `handoff_recommended`, sem id, turno concluído."""
    deps = await make_deps(tmp_path, ScriptedLLM([_human_plan(), _human_plan()]))
    message = {"message": "Quero falar com um atendente."}
    opened = (await _api(deps, "POST", "/chat", "user-a", message)).json()
    assert opened["status"] == "handoff_opened"
    assert opened["handoff_id"] and opened["run_status"] == "awaiting_operator"
    limited = [RECEIVABLES_READ, TERMINALS_READ]
    recommended = (await _api(deps, "POST", "/chat", "user-a", message, scopes=limited)).json()
    assert recommended["status"] == "handoff_recommended"
    assert recommended["handoff_id"] is None and recommended["run_status"] == "completed"
