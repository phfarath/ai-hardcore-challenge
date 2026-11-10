"""Knowledge Agent: conhecimento público com origem verificável (RAG + busca web)."""

import json

from app.context import RequestContext
from app.llm import LLMClient, ToolSpec, run_agent
from app.prompts import KNOWLEDGE_SYSTEM, today_label
from app.schemas import AgentOutput, AgentResult, Task

NOT_FOUND_ANSWER = (
    "Não encontrei essa informação nas fontes consultadas (páginas oficiais da Getnet e busca "
    "web), então prefiro não responder com dados não verificados."
)


async def run_knowledge(
    llm: LLMClient,
    ctx: RequestContext,
    tools: list[ToolSpec],
    task: Task,
    technical_context: dict[str, str] | None = None,
) -> AgentResult:
    if task.source_scope == "getnet":
        tools = [t for t in tools if t.name != "web_search"]  # Getnet: só fontes oficiais
    user = task.question
    if technical_context:
        user += "\n\nContexto técnico do atendimento (sem dados pessoais): " + json.dumps(
            technical_context, ensure_ascii=False
        )
    system = KNOWLEDGE_SYSTEM.format(today=today_label(ctx.clock.now()))
    output, _ = await run_agent(
        llm, ctx, system=system, user=user, tools=tools, output_model=AgentOutput
    )
    # o servidor valida os ids: só evidências públicas coletadas nesta requisição
    cited = [
        ctx.evidence[i]
        for i in dict.fromkeys(output.evidence_ids)
        if i in ctx.evidence and ctx.evidence[i].type in ("kb_chunk", "web_result")
    ]
    return AgentResult(
        agent="knowledge",
        status="answered" if cited else "insufficient_evidence",
        # sem evidência validada o texto pode ter vindo da memória do modelo: descarta
        answer=output.answer if cited else NOT_FOUND_ANSWER,
        evidence=cited,
        pending=output.pending,
    )
