"""Verificação da resposta do Knowledge antes da entrega (T06).

Duas camadas: proveniência determinística (o trecho citado existe, literalmente, numa evidência
pública coletada nesta requisição) e uma chamada semântica separada, sem ferramentas, que julga
suporte e cobertura a partir da pergunta original, e não da tarefa que o Knowledge escolheu.
"""

import json
import re
import unicodedata

from app.context import BudgetExceeded, ProviderUnavailable, RequestContext, VerificationUnavailable
from app.llm import LLMClient
from app.prompts import VERIFIER_SYSTEM
from app.schemas import Claim, VerificationOutput

MIN_QUOTE_CHARS = 12
_PUBLIC = ("kb_chunk", "web_result")


def _normalize(text: str) -> str:
    """Compara citações sem depender de tipografia: hífens, aspas e espaços variam entre a página
    e a cópia do modelo, o conteúdo não."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[‐-―−]", "-", text)
    text = re.sub(r"[\"'“”‘’«»…]", "", text)
    return re.sub(r"\s+", " ", text).strip(" .;:")


def evidence_texts(ctx: RequestContext) -> dict[str, str]:
    """Texto de cada evidência pública coletada, guardado pelo servidor (o LLM não o reescreve)."""
    texts: dict[str, str] = {}
    for result in ctx.tool_results:
        for chunk in result.data.get("chunks", []):
            texts[chunk["chunk_id"]] = chunk["text"]
        for entry in result.data.get("results", []):
            texts[entry["id"]] = f"{entry.get('title', '')}\n{entry.get('content', '')}"
    return texts


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", _normalize(text))


def ground(claim: Claim, ctx: RequestContext, texts: dict[str, str]) -> Claim | None:
    """Afirmação com proveniência verificada, ou None. O trecho tem de existir literalmente
    numa evidência pública desta requisição; se o modelo errou o id (ex.: outro chunk da mesma
    página), a afirmação é religada à evidência que de fato contém o trecho."""
    if claim.status != "covered" or not claim.quote:
        return None
    if len(_normalize(claim.quote)) < MIN_QUOTE_CHARS:
        return None
    quote = _compact(claim.quote)
    public = [i for i in texts if (e := ctx.evidence.get(i)) is not None and e.type in _PUBLIC]
    candidates = sorted(public, key=lambda i: i != claim.evidence_id)  # o id citado primeiro
    for evidence_id in candidates:
        if quote in _compact(texts[evidence_id]):
            return claim.model_copy(update={"evidence_id": evidence_id})
    return None


def _source_label(ctx: RequestContext, claim: Claim) -> str:
    """Título e URL da fonte vêm dos metadados do servidor: identificam a entidade da página
    (ex.: a página de um modelo, a cidade de uma previsão) sem confiar no texto do modelo."""
    evidence = ctx.evidence.get(claim.evidence_id or "")
    if evidence is None:
        return ""
    return " | ".join(p for p in (evidence.title, evidence.url) if p)


async def verify(
    llm: LLMClient,
    ctx: RequestContext,
    question: str,
    answer: str,
    claims: list[Claim],
    original_message: str | None = None,
) -> VerificationOutput:
    """Julgamento semântico. Qualquer falha vira `VerificationUnavailable`: sem veredito, o
    rascunho não é liberado (indisponibilidade não é o mesmo que 'não sustentado')."""
    payload = {
        "mensagem_original_do_cliente": original_message or question,
        "parte_atribuida_a_este_agente": question,
        "resposta_ao_cliente": answer,
        "afirmacoes": [
            {
                "n": i,
                "parte_da_pergunta": c.obligation,
                "afirmacao": c.statement,
                "fonte": _source_label(ctx, c),
                "trecho": c.quote,
            }
            for i, c in enumerate(claims)
        ],
    }
    messages = [
        {"role": "system", "content": VERIFIER_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    try:
        turn = await llm.chat(
            messages, tools=None, output_model=VerificationOutput, budget=ctx.budget
        )
    except (ProviderUnavailable, BudgetExceeded) as exc:
        raise VerificationUnavailable(type(exc).__name__) from exc
    if turn.tool_calls or not isinstance(turn.parsed, VerificationOutput):
        raise VerificationUnavailable("verifier returned no structured output")
    return turn.parsed
