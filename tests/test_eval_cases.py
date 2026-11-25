import json

import httpx
import pytest

from app.schemas import ChatResponse
from evals.run_evals import Case, load_cases, score_case, score_error, summarize

ENUNCIADO_MESSAGES = [
    "What's the difference between the Get Clássica and the Get Smart?",
    "What's the weather forecast in Porto Alegre tomorrow?",
    "When will the money from yesterday's sales be deposited?",
    "Do I need a bank account to receive my sales via Pix?",
    "My card machine won't connect to the internet, what should I do?",
    "How does receivables advance (antecipação) work with Getnet?",
    "What's the euro exchange rate today?",
    "My card machine is showing a transaction decline error.",
    "How many installments can I split a sale into with the crediário?",
    "Can I sell through WhatsApp using the Payment Link?",
]
WORKFLOWS = {"knowledge", "support", "support_then_knowledge", "combined", "clarify", "handoff"}


def test_cases_cover_ten_enunciado_messages() -> None:
    cases = load_cases()
    by_message = {c.message: c for c in cases}
    for message in ENUNCIADO_MESSAGES:
        assert message in by_message
        assert by_message[message].user_id == "cliente1988"


def test_cases_schema_valid_and_include_extra_edge_cases() -> None:
    cases = load_cases()
    assert len({c.id for c in cases}) == len(cases)
    assert all(c.expected_workflow in WORKFLOWS for c in cases)
    assert len(cases) >= 15
    statuses = {c.expected_status for c in cases}
    assert {"insufficient_evidence", "customer_not_found", "needs_clarification"} <= statuses


def case(**overrides: object) -> Case:
    base: dict[str, object] = {
        "id": "x",
        "message": "m",
        "user_id": "cliente1988",
        "expected_workflow": "support",
        "required_tools": ["get_receivables"],
        "expected_source_urls_any": None,
        "expected_status": "answered",
        "must_preserve": ["1.250,00"],
        "must_preserve_records": [["1.250,00", "previsto"]],
        "allowed_amounts": ["1.250,00"],
        "must_not_claim": ["confirmado"],
    }
    return Case(**{**base, **overrides})  # type: ignore[arg-type]


def response(**overrides: object) -> ChatResponse:
    base: dict[str, object] = {
        "request_id": "r",
        "status": "answered",
        "answer": "Você receberá R$ 1.250,00, status: previsto.",
        "agents_used": ["router", "customer_support"],
        "sources": [],
        "data_mode": "demo",
        "workflow": "support",
        "tools_used": ["get_receivables"],
    }
    return ChatResponse(**{**base, **overrides})  # type: ignore[arg-type]


def test_score_case_passes_on_matching_response() -> None:
    score = score_case(case(), response())
    assert score.passed
    assert score.checks["route"] is True and score.checks["sources"] is None


def test_score_case_fails_on_forbidden_claim_wrong_route_and_missing_value() -> None:
    bad = response(answer="Seu pagamento foi confirmado.", workflow="knowledge")
    score = score_case(case(), bad)
    assert not score.passed
    assert score.checks["route"] is False
    assert score.checks["preserve"] is False
    assert score.checks["forbidden"] is False


def test_score_case_checks_expected_sources_any_match() -> None:
    expected = ["https://site.getnet.com.br/crediario/"]
    sources = [{"id": "c1", "type": "kb_chunk", "url": expected[0], "title": "Crediário"}]
    ok = score_case(case(expected_source_urls_any=expected), response(sources=sources))
    missing = score_case(case(expected_source_urls_any=expected), response())
    assert ok.checks["sources"] is True
    assert missing.checks["sources"] is False


SMART = "https://site.getnet.com.br/get-ajuda-maquininha/solucoes-get-smart/"
CREDIARIO = "https://site.getnet.com.br/crediario/"


def kb_source(url: str) -> dict[str, str]:
    return {"id": "c1", "type": "kb_chunk", "url": url, "title": "t"}


def knowledge_case(**overrides: object) -> Case:
    base: dict[str, object] = {
        "expected_workflow": "knowledge",
        "required_tools": ["retrieve_getnet_knowledge"],
        "expected_source_urls_any": [CREDIARIO],
        "must_preserve": [],
        "must_preserve_records": [],
        "allowed_amounts": None,
        "must_mention_any": [["48"]],
        "must_not_claim": [],
    }
    return case(**{**base, **overrides})


def knowledge_response(answer: str, **overrides: object) -> ChatResponse:
    base: dict[str, object] = {
        "answer": answer,
        "workflow": "knowledge",
        "agents_used": ["router", "knowledge"],
        "tools_used": ["retrieve_getnet_knowledge"],
        "sources": [kb_source(CREDIARIO)],
    }
    return response(**{**base, **overrides})


def test_wrong_answer_with_valid_source_fails() -> None:
    wrong = knowledge_response("Com o crediário você parcela em até 12 vezes.")
    right = knowledge_response("Com o Crediário, o cliente parcela em até 48 vezes.")
    assert score_case(knowledge_case(), wrong).checks["anchors"] is False
    assert score_case(knowledge_case(), right).passed


def test_extra_private_tool_fails_even_when_required_ones_ran() -> None:
    extra = knowledge_response(
        "Até 48 vezes.", tools_used=["retrieve_getnet_knowledge", "get_receivables"]
    )
    assert score_case(knowledge_case(), extra).checks["tools"] is False


def test_allowed_extra_tool_passes_and_order_is_enforced_when_required() -> None:
    c = case(
        required_tools=["get_terminal_diagnostics", "retrieve_getnet_knowledge"],
        ordered=True,
        must_preserve=[],
        must_preserve_records=[],
        allowed_amounts=None,
    )
    right = response(tools_used=["get_terminal_diagnostics", "retrieve_getnet_knowledge"])
    reversed_ = response(tools_used=["retrieve_getnet_knowledge", "get_terminal_diagnostics"])
    assert score_case(c, right).checks["tools"] is True
    assert score_case(c, reversed_).checks["tools"] is False
    loose = case(required_tools=["get_receivables"], allowed_tools=["web_search"])
    assert score_case(loose, response(tools_used=["get_receivables", "web_search"])).checks["tools"]


def test_number_bound_to_wrong_status_fails_records() -> None:
    wrong = response(
        answer="- Venda R-1: R$ 1.250,00, status: pago\n- Venda R-2: R$ 480,50, status: previsto"
    )
    assert score_case(case(), wrong).checks["records"] is False


def test_contradicting_amount_in_free_text_fails() -> None:
    contradiction = response(
        answer="- Venda R-1: R$ 1.250,00, status: previsto\nO total é R$ 9.999,00 amanhã."
    )
    score = score_case(case(), contradiction)
    assert score.checks["amounts"] is False
    assert not score.passed


def test_contradictory_status_fails() -> None:
    bad = knowledge_response("Até 48 vezes.", status="insufficient_evidence")
    assert score_case(knowledge_case(), bad).checks["status"] is False


def test_legitimate_rewording_passes() -> None:
    reworded = knowledge_response(
        "With the Crediário, your customer can split purchases into up to 48 installments."
    )
    assert score_case(knowledge_case(), reworded).passed


def test_empty_lists_are_not_applicable_and_terms_match_whole_words() -> None:
    c = knowledge_case(must_mention_any=[], expected_source_urls_any=None)
    score = score_case(c, knowledge_response("qualquer"))
    assert score.checks["preserve"] is None
    assert score.checks["forbidden"] is None
    assert score.checks["anchors"] is None
    assert score.checks["sources"] is None
    word = knowledge_case(must_mention_any=[["48"]])
    assert score_case(word, knowledge_response("até 480 vezes")).checks["anchors"] is False


def test_request_errors_count_as_failures_in_every_applicable_check() -> None:
    err = score_error(case(), "HTTP 503")
    ok = score_case(case(), response())
    assert not err.passed
    summary = summarize([ok, err])
    assert summary.errors == 1 and summary.passed == 1
    assert summary.metrics["route"] == {"ok": 1, "applicable": 2}
    assert summary.metrics["sources"] == {"ok": 0, "applicable": 0}


def test_financial_cases_bind_values_to_status_and_restrict_amounts() -> None:
    by_id = {c.id: c for c in load_cases()}
    e03 = by_id["e03-yesterday-sales-deposit"]
    assert ["1.250,00", "previsto"] in e03.must_preserve_records
    assert e03.allowed_amounts == ["1.250,00", "480,50"]
    assert by_id["x14-unknown-customer-receivables"].allowed_amounts == []
    assert by_id["e05-machine-wont-connect"].ordered


@pytest.mark.xfail(strict=True, reason="runner ainda não faz casos de vários turnos")
async def test_multi_turn_case_reuses_the_conversation_and_scores_the_last_turn() -> None:
    """x17: os turnos de preparo abrem a conversa; só o último é pontuado."""
    from evals.run_evals import _ask

    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/dev/token":
            return httpx.Response(200, json={"access_token": "t"})
        body = json.loads(request.content)
        seen.append(body)
        answer = ChatResponse(
            request_id="r",
            status="answered",
            answer=f"turno {len(seen)}",
            workflow="knowledge",
            agents_used=["router", "knowledge"],
            tools_used=["retrieve_getnet_knowledge"],
            sources=[],
            conversation_id="conv_1",
        )
        return httpx.Response(200, json=answer.model_dump(mode="json"))

    multi = case(message="Como eu ativo ele?", setup_messages=["Fale do modo quiosque."])
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        result = await _ask(client, multi)
    assert isinstance(result, ChatResponse) and result.answer == "turno 2"
    assert "conversation_id" not in seen[0]
    assert seen[1] == {
        "message": "Como eu ativo ele?",
        "user_id": "cliente1988",
        "conversation_id": "conv_1",
    }
