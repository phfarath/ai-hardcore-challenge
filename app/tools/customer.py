"""Ferramentas do Customer Support. A identidade vem sempre de `ctx.user_id`, nunca de
argumentos escolhidos pelo LLM."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.context import RequestContext
from app.llm import ToolSpec
from app.repositories import CustomerRepository
from app.schemas import Evidence, ToolResult


class ReceivablesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sales_date: date


class DiagnosticsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    terminal_id: str | None = None


def build_customer_tools(repo: CustomerRepository) -> list[ToolSpec]:
    async def get_receivables(args: ReceivablesArgs, ctx: RequestContext) -> ToolResult:
        if not repo.customer_exists(ctx.user_id):
            return ToolResult(status="not_found", error="customer not found")
        now = ctx.clock.now()
        rows = repo.receivables_for(ctx.user_id, args.sales_date, now.date())
        data = {
            "sales_date": args.sales_date.isoformat(),
            "queried_at": now.isoformat(),
            "data_mode": "demo",
            "receivables": [r.model_dump(mode="json") for r in rows],
        }
        return ToolResult(
            status="ok" if rows else "empty",
            data=data,
            evidence=[
                Evidence(id=r.id, type="customer_record", title=f"Recebível {r.id}") for r in rows
            ],
            reference_date=args.sales_date,
        )

    async def get_terminal_diagnostics(args: DiagnosticsArgs, ctx: RequestContext) -> ToolResult:
        if not repo.customer_exists(ctx.user_id):
            return ToolResult(status="not_found", error="customer not found")
        now = ctx.clock.now()
        terminals = repo.terminals_for(ctx.user_id, now)
        if not terminals:
            return ToolResult(status="empty", data={"data_mode": "demo"})
        chosen = [t for t in terminals if t.id == args.terminal_id]
        if not chosen and len(terminals) == 1 and args.terminal_id is None:
            chosen = terminals
        if not chosen:
            return ToolResult(
                status="needs_clarification",
                data={"options": [t.id for t in terminals], "data_mode": "demo"},
            )
        terminal = chosen[0]
        return ToolResult(
            status="ok",
            data={"terminal": terminal.model_dump(mode="json"), "data_mode": "demo"},
            evidence=[
                Evidence(
                    id=f"diag-{terminal.id}",
                    type="customer_record",
                    title="Diagnóstico do terminal",
                )
            ],
            reference_date=terminal.observed_at.date(),
        )

    return [
        ToolSpec(
            name="get_receivables",
            description=(
                "Consulta os recebíveis (valor, data prevista de liquidação e status) do cliente "
                "atual para vendas realizadas em uma data. Use a data de hoje informada no "
                "contexto para resolver 'ontem', 'hoje' etc."
            ),
            args_model=ReceivablesArgs,
            run=get_receivables,
        ),
        ToolSpec(
            name="get_terminal_diagnostics",
            description=(
                "Consulta o diagnóstico da maquininha do cliente atual: modelo, estado de "
                "conexão, último código de erro e última transação. Envie terminal_id = null se o "
                "cliente não informou qual terminal."
            ),
            args_model=DiagnosticsArgs,
            run=get_terminal_diagnostics,
        ),
    ]


def _money(amount: str, currency: str) -> str:
    value = f"{Decimal(amount):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {value}" if currency == "BRL" else f"{currency} {value}"


def _br_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d/%m/%Y")


def render_receivables(result: ToolResult) -> str:
    """Bloco determinístico com valor, data e status exatamente como a ferramenta devolveu,
    para que a geração do LLM não transforme 'previsto' em 'confirmado'."""
    if result.status not in ("ok", "empty"):
        return ""
    day = _br_date(result.data["sales_date"])
    rows = result.data["receivables"]
    if not rows:
        return f"Nenhuma venda registrada em {day} (dados demonstrativos)."
    lines = [f"Recebíveis das vendas de {day} (dados demonstrativos):"]
    for r in rows:
        lines.append(
            f"- Venda {r['id']}: {_money(r['amount'], r['currency'])}, "
            f"liquidação em {_br_date(r['settlement_date'])}, status: {r['status']}"
        )
    return "\n".join(lines)
