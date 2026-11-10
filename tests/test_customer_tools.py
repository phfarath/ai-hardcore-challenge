import json
from datetime import date

from pydantic import BaseModel

from app.context import Budget, RequestContext
from app.llm import ToolSpec
from app.repositories import JsonCustomerRepository
from app.schemas import ToolResult
from app.tools.customer import (
    DiagnosticsArgs,
    ReceivablesArgs,
    build_customer_tools,
    render_receivables,
)
from tests.fakes import FrozenClock

REPO = JsonCustomerRepository("data/fixtures/customers.json")
TOOLS = {t.name: t for t in build_customer_tools(REPO)}


def ctx_for(user_id: str) -> RequestContext:
    return RequestContext(request_id="r", user_id=user_id, clock=FrozenClock(), budget=Budget())


async def call(tool: ToolSpec, args: BaseModel, user_id: str) -> ToolResult:
    return await tool.run(args, ctx_for(user_id))


async def receivables(user_id: str, day: date) -> ToolResult:
    return await call(TOOLS["get_receivables"], ReceivablesArgs(sales_date=day), user_id)


async def test_receivables_cliente1988_yesterday_preserves_values_and_status() -> None:
    result = await receivables("cliente1988", date(2026, 11, 6))
    assert result.status == "ok"
    rows = result.data["receivables"]
    assert [(r["amount"], r["settlement_date"], r["status"]) for r in rows] == [
        ("1250.00", "2026-11-07", "previsto"),
        ("480.50", "2026-12-06", "previsto"),
    ]
    assert {e.type for e in result.evidence} == {"customer_record"}
    assert result.reference_date == date(2026, 11, 6)


async def test_receivables_isolated_between_customers() -> None:
    result = await receivables("cliente2000", date(2026, 11, 6))
    ids = [r["id"] for r in result.data["receivables"]]
    assert ids == ["R-2000-001", "R-2000-002"]
    assert "1988" not in result.model_dump_json()


async def test_receivables_unknown_customer_not_found() -> None:
    result = await receivables("desconhecido", date(2026, 11, 6))
    assert result.status == "not_found"
    assert result.data == {}


async def test_receivables_no_sales_on_date_is_empty_not_not_found() -> None:
    result = await receivables("cliente1988", date(2026, 11, 1))
    assert result.status == "empty"
    assert result.data["receivables"] == []


async def test_diagnostics_cliente1988_returns_model_error_code_and_separate_tx() -> None:
    result = await call(TOOLS["get_terminal_diagnostics"], DiagnosticsArgs(), "cliente1988")
    assert result.status == "ok"
    term = result.data["terminal"]
    assert term["model"] == "Get Clássica"
    assert term["connection_state"] == "offline"
    assert term["last_error_code"] == "ADQ 1-19"
    assert term["last_transaction"]["status"] == "declined"
    assert term["last_transaction"]["occurred_at"] != term["observed_at"]


async def test_diagnostics_multiple_terminals_requires_clarification() -> None:
    result = await call(TOOLS["get_terminal_diagnostics"], DiagnosticsArgs(), "cliente2000")
    assert result.status == "needs_clarification"
    assert result.data["options"] == ["T-2000-01", "T-2000-02"]


async def test_diagnostics_foreign_terminal_id_needs_clarification_without_leak() -> None:
    args = DiagnosticsArgs(terminal_id="T-2000-01")
    result = await call(TOOLS["get_terminal_diagnostics"], args, "cliente1988")
    assert result.status == "needs_clarification"
    assert result.data["options"] == ["T-1988-01"]
    assert "T-2000" not in result.model_dump_json()


async def test_diagnostics_unknown_customer_not_found() -> None:
    result = await call(TOOLS["get_terminal_diagnostics"], DiagnosticsArgs(), "desconhecido")
    assert result.status == "not_found"


async def test_tool_args_do_not_expose_identity() -> None:
    for tool in TOOLS.values():
        schema = json.dumps(tool.args_model.model_json_schema())
        assert "user_id" not in schema
        assert "customer" not in schema


async def test_render_receivables_keeps_previsto_and_never_says_confirmado() -> None:
    result = await receivables("cliente1988", date(2026, 11, 6))
    text = render_receivables(result)
    assert "R$ 1.250,00" in text
    assert "07/11/2026" in text
    assert "previsto" in text
    assert "confirmado" not in text.lower()
    assert "demonstrativ" in text.lower()


async def test_render_receivables_empty_says_no_sales() -> None:
    text = render_receivables(await receivables("cliente1988", date(2026, 11, 1)))
    assert "nenhuma venda" in text.lower()
