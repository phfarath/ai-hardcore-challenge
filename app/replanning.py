"""Replanejamento limitado (T07): decisões tomadas pelo código depois de observar cada resultado.

O plano do Router é o ponto de partida, não um roteiro fixo. Depois de cada etapa o orquestrador
olha o que voltou e pode mudar o plano uma vez, por evidência nova:

- `knowledge_repair`: o verificador apontou lacunas; uma nova tentativa mira só nelas.
- `guidance_added`: o diagnóstico do terminal mostrou um código de erro ou recusa que o plano não
  ia explicar; entra uma busca de orientação oficial (só fontes Getnet).

Retry de provedor (mesma chamada, erro transitório) fica no cliente LLM e não conta aqui. Uma
mudança de plano só acontece se sobrar orçamento para concluí-la e ainda verificar a resposta;
sem progresso, o resultado anterior é mantido e o pedido termina com o que já foi comprovado.
Nenhuma mudança amplia permissões: o escopo de fontes só fica igual ou mais estreito, e as
ferramentas continuam filtradas pelos escopos do principal.
"""

from dataclasses import dataclass

from app.agents.knowledge import split_header
from app.context import RequestContext
from app.language import Language
from app.schemas import AgentResult, Status, Task

KNOWLEDGE_REPAIR = "knowledge_repair"
GUIDANCE_ADDED = "guidance_added"
NO_PROGRESS = "no_progress"

REPAIR_HINT: dict[Language, str] = {
    "pt": (
        "Uma busca anterior não encontrou evidência para estas partes. Procure só por elas, com "
        "outra formulação de consulta: {gaps}"
    ),
    "en": (
        "A previous search found no evidence for these parts. Search only for them, with a "
        "different query wording: {gaps}"
    ),
}
_GUIDANCE_QUESTION = (
    "Qual a orientação oficial da Getnet para o problema observado no terminal ({issue}{code})?"
)
_ISSUE_LABELS = {"connectivity": "falha de conexão", "transaction_declined": "transação recusada"}


@dataclass(frozen=True)
class ReplanPolicy:
    max_replans: int
    min_llm_calls: int
    min_seconds: float

    def affordable(self, ctx: RequestContext, used: int) -> bool:
        """Reserva para concluir: a nova etapa precisa de ferramenta, resposta e verificação."""
        budget = ctx.budget
        return (
            used < self.max_replans
            and budget.remaining_llm_calls() >= self.min_llm_calls
            and budget.remaining_seconds() >= self.min_seconds
        )


def repair_task(task: Task, gaps: list[str], lang: Language = "pt") -> tuple[Task, str]:
    """A pergunta passa a ser só as lacunas (é contra elas que o verificador julga); o escopo
    de fontes é o da tarefa original e nunca se alarga."""
    question = "; ".join(g.strip().rstrip(".") for g in gaps)
    hint = REPAIR_HINT[lang].format(gaps=question)
    return task.model_copy(update={"question": question}), hint


def guidance_task(technical: dict[str, str]) -> Task | None:
    """Só para um código concreto: sem código não há o que procurar nas páginas de erro."""
    code = technical.get("error_code") or technical.get("decline_code")
    issue = technical.get("issue", "none")
    if issue == "none" or not code:
        return None
    label = _ISSUE_LABELS.get(issue, issue)
    question = _GUIDANCE_QUESTION.format(issue=label, code=f", código {code}")
    return Task(agent="knowledge", question=question, source_scope="getnet")


def made_progress(before: AgentResult, after: AgentResult) -> bool:
    """Progresso = evidência sustentada nova. Reformular sem fonte nova não conta."""
    known = {e.id for e in before.evidence}
    return any(e.id not in known for e in after.evidence)


def merge_repair(before: AgentResult, after: AgentResult) -> AgentResult:
    """Junta o que cada tentativa comprovou; as lacunas que restam são as da tentativa nova.

    Ordem (D4): o reparo vem primeiro. Ele mira a lacuna que o verificador apontou na primeira
    tentativa (ex.: a cotação de hoje, quando só havia uma de três dias antes), então é a parte
    que responde ao que faltava; o que a primeira tentativa comprovou fica depois, como
    complemento. Um só cabeçalho de assunto, o da primeira tentativa."""
    if before.status == "insufficient_evidence":
        return after
    evidence = {e.id: e for e in [*before.evidence, *after.evidence]}
    status: Status = "answered" if not after.gaps else "partial"
    header, earlier = split_header(before.answer)
    _, repaired = split_header(after.answer)
    parts = [header, repaired, earlier] if header else [repaired, earlier]
    return AgentResult(
        agent=before.agent,
        status=status,
        answer="\n\n".join(parts),
        evidence=list(evidence.values()),
        pending=list(dict.fromkeys([*before.pending, *after.pending])),
        gaps=after.gaps,
    )
