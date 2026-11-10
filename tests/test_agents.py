import hashlib
from datetime import UTC, datetime
from pathlib import Path

from app.agents.knowledge import run_knowledge
from app.agents.support import run_support
from app.context import Budget, RequestContext
from app.llm import ToolSpec
from app.rag import KnowledgeIndex, chunk_document
from app.repositories import JsonCustomerRepository
from app.schemas import AgentOutput, Task
from app.tools.customer import build_customer_tools
from app.tools.knowledge import build_knowledge_tools
from app.tools.search import SearchUnavailable, WebResult
from tests.fakes import (
    FakeEmbedder,
    FakeSearch,
    FrozenClock,
    ScriptedLLM,
    final_turn,
    tool_call_turn,
)

SMART_URL = "https://site.getnet.com.br/get-ajuda-maquininha/solucoes-get-smart/"
CUSTOMER_TOOLS = build_customer_tools(JsonCustomerRepository("data/fixtures/customers.json"))


def ctx_for(user_id: str = "cliente1988") -> RequestContext:
    return RequestContext(request_id="r", user_id=user_id, clock=FrozenClock(), budget=Budget())


async def knowledge_tools(
    tmp_path: Path, search: FakeSearch | None = None
) -> tuple[list[ToolSpec], str]:
    index = KnowledgeIndex(str(tmp_path / "idx"), FakeEmbedder())
    chunks = chunk_document(
        "A Get Smart tem tela touch e impressora.",
        source_url=SMART_URL,
        title="Soluções Get Smart",
        fetched_at=datetime(2026, 11, 7, tzinfo=UTC),
    )
    await index.replace_document(SMART_URL, chunks)
    return build_knowledge_tools(index, search), chunks[0].chunk_id


async def test_support_receivables_flow_preserves_tool_data() -> None:
    llm = ScriptedLLM(
        [
            tool_call_turn("get_receivables", sales_date="2026-11-06"),
            final_turn(
                AgentOutput(answer="Seu pagamento foi confirmado.", evidence_ids=["R-1988-001"])
            ),
        ]
    )
    task = Task(agent="support", question="Quando recebo minhas vendas de ontem?")
    result = await run_support(llm, ctx_for(), CUSTOMER_TOOLS, task)
    assert result.agent == "customer_support"
    assert result.status == "answered"
    assert "R$ 1.250,00" in result.answer
    assert "07/11/2026" in result.answer
    assert "status: previsto" in result.answer
    assert {e.id for e in result.evidence} == {"R-1988-001", "R-1988-002"}


async def test_support_prompt_includes_today_from_injected_clock() -> None:
    llm = ScriptedLLM([final_turn(AgentOutput(answer="ok"))])
    await run_support(llm, ctx_for(), CUSTOMER_TOOLS, Task(agent="support", question="q"))
    assert "2026-11-07" in llm.calls[0][0]["content"]
    assert llm.calls[0][1]["content"] == "q"


async def test_support_no_sales_answers_without_customer_not_found() -> None:
    llm = ScriptedLLM(
        [
            tool_call_turn("get_receivables", sales_date="2026-11-01"),
            final_turn(AgentOutput(answer="Não encontrei vendas nessa data.")),
        ]
    )
    result = await run_support(llm, ctx_for(), CUSTOMER_TOOLS, Task(agent="support", question="q"))
    assert result.status == "answered"
    assert "nenhuma venda registrada" in result.answer.lower()


async def test_support_unknown_customer_status_customer_not_found() -> None:
    llm = ScriptedLLM(
        [
            tool_call_turn("get_receivables", sales_date="2026-11-06"),
            final_turn(AgentOutput(answer="Não localizei seu cadastro.")),
        ]
    )
    result = await run_support(
        llm, ctx_for("desconhecido"), CUSTOMER_TOOLS, Task(agent="support", question="q")
    )
    assert result.status == "customer_not_found"
    assert result.evidence == []


async def test_support_ambiguous_terminal_needs_clarification() -> None:
    llm = ScriptedLLM(
        [
            tool_call_turn("get_terminal_diagnostics"),
            final_turn(AgentOutput(answer="Qual terminal: T-2000-01 ou T-2000-02?")),
        ]
    )
    result = await run_support(
        llm, ctx_for("cliente2000"), CUSTOMER_TOOLS, Task(agent="support", question="q")
    )
    assert result.status == "needs_clarification"


async def test_knowledge_rag_answer_has_validated_sources(tmp_path: Path) -> None:
    tools, chunk_id = await knowledge_tools(tmp_path)
    llm = ScriptedLLM(
        [
            tool_call_turn("retrieve_getnet_knowledge", query="Get Smart"),
            final_turn(
                AgentOutput(
                    answer="A Get Smart tem tela touch.", evidence_ids=[chunk_id, "inventado-1"]
                )
            ),
        ]
    )
    task = Task(agent="knowledge", question="O que é a Get Smart?")
    result = await run_knowledge(llm, ctx_for(), tools, task)
    assert result.agent == "knowledge"
    assert result.status == "answered"
    assert [(e.id, e.url, e.title) for e in result.evidence] == [
        (chunk_id, SMART_URL, "Soluções Get Smart")
    ]


async def test_knowledge_cannot_cite_customer_records(tmp_path: Path) -> None:
    tools, _ = await knowledge_tools(tmp_path)
    ctx = ctx_for()
    support_llm = ScriptedLLM(
        [
            tool_call_turn("get_receivables", sales_date="2026-11-06"),
            final_turn(AgentOutput(answer="ok")),
        ]
    )
    await run_support(support_llm, ctx, CUSTOMER_TOOLS, Task(agent="support", question="q"))
    llm = ScriptedLLM([final_turn(AgentOutput(answer="x", evidence_ids=["R-1988-001"]))])
    result = await run_knowledge(llm, ctx, tools, Task(agent="knowledge", question="q"))
    assert result.evidence == []
    assert result.status == "insufficient_evidence"


async def test_knowledge_without_valid_evidence_is_insufficient_evidence(tmp_path: Path) -> None:
    tools, _ = await knowledge_tools(tmp_path)
    llm = ScriptedLLM([final_turn(AgentOutput(answer="Não encontrei.", evidence_ids=["nada"]))])
    result = await run_knowledge(llm, ctx_for(), tools, Task(agent="knowledge", question="q"))
    assert result.status == "insufficient_evidence"
    assert result.evidence == []


async def test_knowledge_web_failure_is_insufficient_evidence(tmp_path: Path) -> None:
    tools, _ = await knowledge_tools(tmp_path, FakeSearch(raises=SearchUnavailable("down")))
    llm = ScriptedLLM(
        [
            tool_call_turn("web_search", query="cotação do euro hoje"),
            final_turn(AgentOutput(answer="Não consegui consultar a cotação.")),
        ]
    )
    task = Task(agent="knowledge", question="Qual a cotação do euro?")
    result = await run_knowledge(llm, ctx_for(), tools, task)
    assert result.status == "insufficient_evidence"


async def test_knowledge_web_answer_is_answered_with_web_source(tmp_path: Path) -> None:
    hit = WebResult(title="Euro", url="https://example.test/euro", content="R$ 6,10")
    search = FakeSearch(results=[hit])
    tools, _ = await knowledge_tools(tmp_path, search)
    ctx = ctx_for()
    web_id = f"web_{hashlib.sha1(hit.url.encode()).hexdigest()[:10]}"
    llm = ScriptedLLM(
        [
            tool_call_turn("web_search", query="cotação do euro hoje"),
            final_turn(AgentOutput(answer="O euro está a R$ 6,10.", evidence_ids=[web_id])),
        ]
    )
    result = await run_knowledge(llm, ctx, tools, Task(agent="knowledge", question="euro?"))
    assert result.status == "answered"
    assert [(e.type, e.url) for e in result.evidence] == [("web_result", hit.url)]
    assert search.queries[-1] == "cotação do euro hoje"


async def test_knowledge_receives_technical_context_in_user_message(tmp_path: Path) -> None:
    tools, _ = await knowledge_tools(tmp_path)
    llm = ScriptedLLM([final_turn(AgentOutput(answer="ok"))])
    await run_knowledge(
        llm,
        ctx_for(),
        tools,
        Task(agent="knowledge", question="Como resolver?"),
        technical_context={"terminal_model": "Get Clássica", "error_code": "ADQ 1-19"},
    )
    user = llm.calls[0][1]["content"]
    assert "Como resolver?" in user
    assert "ADQ 1-19" in user
    assert "Get Clássica" in user


async def test_support_strips_paid_claims_from_prose_when_receivable_is_only_previsto() -> None:
    llm = ScriptedLLM(
        [
            tool_call_turn("get_receivables", sales_date="2026-11-06"),
            final_turn(AgentOutput(answer="Seu pagamento de R$ 1.250,00 já foi confirmado.")),
        ]
    )
    result = await run_support(llm, ctx_for(), CUSTOMER_TOOLS, Task(agent="support", question="q"))
    assert "confirmad" not in result.answer.lower()
    assert "status: previsto" in result.answer
    assert "ainda não foi pago" in result.answer


async def test_support_strips_english_paid_claims_too() -> None:
    llm = ScriptedLLM(
        [
            tool_call_turn("get_receivables", sales_date="2026-11-06"),
            final_turn(AgentOutput(answer="Your money was deposited and confirmed.")),
        ]
    )
    result = await run_support(llm, ctx_for(), CUSTOMER_TOOLS, Task(agent="support", question="q"))
    assert "deposited" not in result.answer.lower()
    assert "confirmed" not in result.answer.lower()


async def test_support_keeps_harmless_prose() -> None:
    llm = ScriptedLLM(
        [
            tool_call_turn("get_receivables", sales_date="2026-11-06"),
            final_turn(AgentOutput(answer="Duas vendas têm datas de liquidação diferentes.")),
        ]
    )
    result = await run_support(llm, ctx_for(), CUSTOMER_TOOLS, Task(agent="support", question="q"))
    assert "Duas vendas têm datas de liquidação diferentes." in result.answer


async def test_knowledge_without_evidence_never_passes_through_the_llm_text(
    tmp_path: Path,
) -> None:
    tools, _ = await knowledge_tools(tmp_path)
    llm = ScriptedLLM([final_turn(AgentOutput(answer="A taxa é de 1,99% por transação."))])
    result = await run_knowledge(llm, ctx_for(), tools, Task(agent="knowledge", question="taxa?"))
    assert result.status == "insufficient_evidence"
    assert "1,99" not in result.answer
    assert "não encontrei" in result.answer.lower()


async def test_knowledge_source_scope_getnet_hides_web_search(tmp_path: Path) -> None:
    tools, _ = await knowledge_tools(tmp_path, FakeSearch())
    llm = ScriptedLLM([final_turn(AgentOutput(answer="x"))])
    task = Task(agent="knowledge", question="q", source_scope="getnet")
    await run_knowledge(llm, ctx_for(), tools, task)
    assert [t.name for t in llm.tools_seen[0] or []] == ["retrieve_getnet_knowledge"]
    llm2 = ScriptedLLM([final_turn(AgentOutput(answer="x"))])
    await run_knowledge(llm2, ctx_for(), tools, Task(agent="knowledge", question="q"))
    assert {t.name for t in llm2.tools_seen[0] or []} == {
        "retrieve_getnet_knowledge",
        "web_search",
    }
