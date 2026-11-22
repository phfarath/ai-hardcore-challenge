"""Customer Support Agent: dados individuais do cliente via ferramentas."""

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from app.context import RequestContext
from app.llm import LLMClient, ToolSpec, run_agent
from app.prompts import SUPPORT_SYSTEM, today_label
from app.schemas import AgentOutput, AgentResult, Evidence, Status, Task, ToolResult
from app.tools.customer import render_receivables

_AMOUNT = re.compile(r"\d{1,3}(?:[.,]\d{3})*[.,]\d{2}(?!\d)|\d+[.,]\d{2}(?!\d)")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def _decimal(text: str) -> Decimal | None:
    """'1.250,00', '1,250.00', '89,90' e '89.90' viram Decimal: o último separador é o decimal."""
    integer, decimals = text[:-3], text[-2:]
    try:
        return Decimal(re.sub(r"[.,]", "", integer) + "." + decimals)
    except InvalidOperation:
        return None


def _tool_amounts(value: Any) -> set[Decimal]:
    if isinstance(value, dict):
        found = {d for k, v in value.items() if k == "amount" and (d := _decimal_or_none(v))}
        return found.union(*(_tool_amounts(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_tool_amounts(v) for v in value))
    return set()


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _safe_prose(prose: str, results: list[ToolResult]) -> str:
    """Texto do LLM só entra quando não há recebíveis (esses são renderizados pelo servidor,
    com as notas de status) e, mesmo assim, sem frases com valores que nenhuma ferramenta
    devolveu."""
    if any("receivables" in r.data for r in results):
        return ""
    allowed = set().union(*(_tool_amounts(r.data) for r in results))
    kept = [
        sentence
        for sentence in _SENTENCE.split(prose)
        if all(_decimal(m) in allowed for m in _AMOUNT.findall(sentence))
    ]
    return " ".join(s.strip() for s in kept if s.strip())


def _status(results: list[ToolResult]) -> Status:
    statuses = {r.status for r in results}
    if "not_found" in statuses:
        return "customer_not_found"
    has_data = bool(statuses & {"ok", "empty"})
    if "needs_clarification" in statuses:
        return "partial" if has_data else "needs_clarification"
    if has_data:
        return "answered"
    # sem nenhuma ferramenta usada o agente só pode estar pedindo mais informação
    return "insufficient_evidence" if results else "needs_clarification"


async def run_support(
    llm: LLMClient, ctx: RequestContext, tools: list[ToolSpec], task: Task
) -> AgentResult:
    system = SUPPORT_SYSTEM.format(today=today_label(ctx.clock.now()))
    output, results = await run_agent(
        llm, ctx, system=system, user=task.question, tools=tools, output_model=AgentOutput
    )
    blocks = [render_receivables(r) for r in results if "receivables" in r.data]
    answer = "\n\n".join(p for p in [*blocks, _safe_prose(output.answer, results)] if p)
    evidence: dict[str, Evidence] = {}
    for result in results:
        for item in result.evidence:
            evidence[item.id] = item
    return AgentResult(
        agent="customer_support",
        status=_status(results),
        answer=answer,
        evidence=list(evidence.values()),
        pending=output.pending,
    )
