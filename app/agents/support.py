"""Customer Support Agent: dados individuais do cliente via ferramentas."""

import re

from app.context import RequestContext
from app.llm import LLMClient, ToolSpec, run_agent
from app.prompts import SUPPORT_SYSTEM, today_label
from app.schemas import AgentOutput, AgentResult, Evidence, Status, Task, ToolResult
from app.tools.customer import render_receivables

_PAID_CLAIM = re.compile(
    r"confirmad|confirmed|\bpag[oa]s?\b|\bpaid\b|depositad|deposited|liquidad|settled"
    r"|creditad|credited",
    re.IGNORECASE,
)
_UNPAID_NOTE = (
    "Atenção: considere apenas o status exibido acima para cada venda; "
    "um recebimento 'previsto' ainda não foi pago."
)


def _safe_prose(prose: str, results: list[ToolResult]) -> str:
    """O texto livre do LLM não pode afirmar pagamento/confirmação de um recebimento que a
    ferramenta não marca como `pago`: nesse caso ele é trocado por uma nota fixa."""
    unpaid = any(row["status"] != "pago" for r in results for row in r.data.get("receivables", []))
    return _UNPAID_NOTE if unpaid and _PAID_CLAIM.search(prose) else prose


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
    # valores, datas e status financeiros vêm do template, não da geração do LLM
    blocks = [render_receivables(r) for r in results if "receivables" in r.data]
    answer = "\n\n".join([*[b for b in blocks if b], _safe_prose(output.answer, results)])
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
