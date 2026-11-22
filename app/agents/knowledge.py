"""Knowledge Agent: conhecimento público com origem verificável (RAG + busca web)."""

import json

from app.context import RequestContext
from app.llm import LLMClient, ToolSpec, run_agent
from app.prompts import KNOWLEDGE_SYSTEM, today_label
from app.schemas import AgentResult, Claim, Evidence, KnowledgeOutput, Task
from app.verification import evidence_texts, ground, verify

NOT_FOUND_ANSWER = (
    "Não encontrei essa informação nas fontes consultadas (páginas oficiais da Getnet e busca "
    "web), então prefiro não responder com dados não verificados."
)
GAP_NOTE = "Não localizei evidência suficiente nas fontes consultadas para: {gaps}."
_VERIFIER_CALLS = 1


def _compose_verified(claims: list[Claim], gaps: list[str]) -> str:
    """Resposta montada só com afirmações aprovadas: nenhum texto não verificado é liberado."""
    lines = [f"- {c.statement}" for c in claims]
    if gaps:
        lines.append(GAP_NOTE.format(gaps="; ".join(dict.fromkeys(gaps))))
    return "\n".join(lines)


async def run_knowledge(
    llm: LLMClient,
    ctx: RequestContext,
    tools: list[ToolSpec],
    task: Task,
    technical_context: dict[str, str] | None = None,
    original_message: str | None = None,
) -> AgentResult:
    """`original_message` só no fluxo `knowledge` puro: nos compostos a mensagem traz partes de
    outro agente, que o verificador não deve cobrar daqui."""
    if task.source_scope == "getnet":
        tools = [t for t in tools if t.name != "web_search"]  # Getnet: só fontes oficiais
    user = task.question
    if technical_context:
        user += "\n\nContexto técnico do atendimento (sem dados pessoais): " + json.dumps(
            technical_context, ensure_ascii=False
        )
    system = KNOWLEDGE_SYSTEM.format(today=today_label(ctx.clock.now()))
    ctx.budget.reserve_llm(_VERIFIER_CALLS)  # a verificação não pode ser engolida pelo loop
    try:
        output, _ = await run_agent(
            llm, ctx, system=system, user=user, tools=tools, output_model=KnowledgeOutput
        )
    finally:
        ctx.budget.release_llm(_VERIFIER_CALLS)

    texts = evidence_texts(ctx)
    grounded = [g for c in output.claims if (g := ground(c, ctx, texts)) is not None]
    supported: list[Claim] = []
    uncovered: list[str] = []
    consistent = False
    if grounded:
        verdict = await verify(llm, ctx, task.question, output.answer, grounded, original_message)
        ok = {v.claim for v in verdict.verdicts if v.supported}
        supported = [c for i, c in enumerate(grounded) if i in ok]
        # a cobertura bloqueia só quando falta o núcleo do pedido, não um detalhe a mais
        uncovered = [] if verdict.answers_question else verdict.uncovered or ["a pergunta"]
        consistent = verdict.answer_consistent
    # lacunas vêm do verificador, que julga contra a pergunta real; partes que o próprio modelo
    # marcou como not_found podem ser desdobramentos que o cliente não pediu
    gaps = uncovered

    if not supported:
        # sem afirmação sustentada o texto pode ter vindo da memória do modelo: descarta
        return AgentResult(
            agent="knowledge",
            status="insufficient_evidence",
            answer=NOT_FOUND_ANSWER,
            evidence=[],
            pending=output.pending,
        )
    evidence: dict[str, Evidence] = {}
    for claim in supported:
        assert claim.evidence_id is not None
        evidence.setdefault(claim.evidence_id, ctx.evidence[claim.evidence_id])
    return AgentResult(
        agent="knowledge",
        status="partial" if gaps else "answered",
        answer=output.answer if consistent and not gaps else _compose_verified(supported, gaps),
        evidence=list(evidence.values()),
        pending=output.pending,
    )
