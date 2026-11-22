"""Regressões dos achados da avaliação (plano de revisão, T01).

Cada teste descreve o comportamento correto e está marcado `xfail(strict=True)` até a tarefa
que o corrige: quando a correção entrar, o teste passa a passar e o `strict` obriga a remover
a marcação. Assim o defeito fica registrado antes da mudança de comportamento.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.agents.knowledge import run_knowledge
from app.agents.support import run_support
from app.context import Budget, RequestContext
from app.orchestrator import extract_public_context
from app.rag import KnowledgeIndex, chunk_document
from app.repositories import JsonCustomerRepository
from app.schemas import AgentOutput, Task, ToolResult
from app.tools.customer import build_customer_tools
from app.tools.knowledge import build_knowledge_tools
from tests.fakes import FakeEmbedder, FrozenClock, ScriptedLLM, final_turn, tool_call_turn

CUSTOMER_TOOLS = build_customer_tools(JsonCustomerRepository("data/fixtures/customers.json"))
FAQ_URL = "https://site.getnet.com.br/duvidas/"


def ctx_for(user_id: str = "cliente1988") -> RequestContext:
    return RequestContext(request_id="r", user_id=user_id, clock=FrozenClock(), budget=Budget())


@pytest.mark.xfail(strict=True, reason="T03: prosa financeira livre ainda é anexada ao template")
async def test_free_text_cannot_contradict_receivables_amounts() -> None:
    llm = ScriptedLLM(
        [
            tool_call_turn("get_receivables", sales_date="2026-11-06"),
            final_turn(AgentOutput(answer="O total é R$ 9.999,00 amanhã.", evidence_ids=[])),
        ]
    )
    task = Task(agent="support", question="Quando recebo minhas vendas de ontem?")
    result = await run_support(llm, ctx_for(), CUSTOMER_TOOLS, task)
    assert "R$ 1.250,00" in result.answer
    assert "9.999,00" not in result.answer


def _terminal_with_decline_and_connectivity_error() -> ToolResult:
    """Mesma combinação da fixture `cliente1988`: ADQ 1-19 e recusa com código 51."""
    return ToolResult(
        status="ok",
        data={
            "terminal": {
                "id": "T-1988-01",
                "model": "Get Clássica",
                "connection_state": "offline",
                "last_error_code": "ADQ 1-19",
                "last_transaction": {"status": "declined", "decline_code": "51"},
            }
        },
    )


@pytest.mark.xfail(strict=True, reason="T03: contexto público ignora a intenção do diagnóstico")
def test_decline_question_gets_decline_context_not_connectivity() -> None:
    context = extract_public_context(  # type: ignore[call-arg]
        _terminal_with_decline_and_connectivity_error(), goal="transaction_decline"
    )
    assert context["issue"] == "transaction_declined"
    assert context["error_code"] == "51"


@pytest.mark.xfail(strict=True, reason="T06: evidência genérica ainda conta como suficiente")
async def test_generic_evidence_does_not_answer_specific_product_rate(tmp_path: Path) -> None:
    index = KnowledgeIndex(str(tmp_path / "idx"), FakeEmbedder())
    chunks = chunk_document(
        "As taxas das maquininhas variam conforme o plano contratado.",
        source_url=FAQ_URL,
        title="Dúvidas frequentes",
        fetched_at=datetime(2026, 11, 7, tzinfo=UTC),
    )
    await index.replace_document(FAQ_URL, chunks)
    llm = ScriptedLLM(
        [
            tool_call_turn("retrieve_getnet_knowledge", query="taxa Getnet Quantum 9000"),
            final_turn(
                AgentOutput(
                    answer="As taxas variam conforme o plano contratado.",
                    evidence_ids=[chunks[0].chunk_id],
                )
            ),
        ]
    )
    task = Task(
        agent="knowledge",
        question="Qual é a taxa da maquininha Getnet Quantum 9000?",
        source_scope="getnet",
    )
    result = await run_knowledge(llm, ctx_for(), build_knowledge_tools(index, None), task)
    assert result.status == "insufficient_evidence"
