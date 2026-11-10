from app.schemas import ChatResponse
from evals.run_evals import Case, load_cases, score_case

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
WORKFLOWS = {"knowledge", "support", "support_then_knowledge", "combined", "clarify"}


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
        "expected_tools": ["get_receivables"],
        "expected_source_urls_any": None,
        "expected_status": "answered",
        "must_preserve": ["1.250,00"],
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
