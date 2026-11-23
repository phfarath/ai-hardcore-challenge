"""Dublês de teste: LLM roteirizado, relógio congelado e helpers de turnos."""

import hashlib
import itertools
import json
from datetime import UTC, datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.auth import Authenticator, DevIssuer, Principal, build_auth
from app.config import Settings
from app.context import Budget
from app.deps import Deps
from app.llm import LLMClient, LLMTurn, ToolCall, ToolSpec
from app.policies import (
    HANDOFFS_CREATE,
    HANDOFFS_READ,
    RECEIVABLES_READ,
    TERMINALS_READ,
    AuthorizedChat,
)
from app.rag import KnowledgeIndex, chunk_document
from app.repositories import JsonCustomerRepository
from app.schemas import Claim, ClaimVerdict, KnowledgeOutput, VerificationOutput
from app.state import SqliteStateStore
from app.tools.search import WebResult

_call_ids = itertools.count(1)


class FrozenClock:
    def __init__(self, dt: datetime | None = None) -> None:
        self._dt = dt or datetime(2026, 11, 7, 10, 0, tzinfo=timezone(timedelta(hours=-3)))

    def now(self) -> datetime:
        return self._dt

    def advance(self, **delta: float) -> None:
        self._dt += timedelta(**delta)


class ScriptedLLM:
    """LLM que devolve turnos pré-definidos e registra o que recebeu."""

    def __init__(self, turns: list[LLMTurn]) -> None:
        self._turns = list(turns)
        self.calls: list[list[dict[str, Any]]] = []
        self.tools_seen: list[list[ToolSpec] | None] = []
        self.output_models: list[type[BaseModel] | None] = []

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolSpec] | None,
        output_model: type[BaseModel] | None,
        budget: Budget,
    ) -> LLMTurn:
        budget.charge_llm()
        self.calls.append(list(messages))
        self.tools_seen.append(tools)
        self.output_models.append(output_model)
        if not self._turns:
            raise AssertionError("ScriptedLLM: no turns left")
        return self._turns.pop(0)


def tool_call_turn(name: str, **args: Any) -> LLMTurn:
    call = ToolCall(
        id=f"call_{next(_call_ids)}", name=name, arguments=json.dumps(args, default=str)
    )
    return LLMTurn(tool_calls=[call])


def final_turn(model: BaseModel) -> LLMTurn:
    return LLMTurn(parsed=model)


class FakeEmbedder:
    """Embeddings determinísticos por hash: textos iguais geram vetores iguais."""

    model = "fake-embed"

    def __init__(self, dim: int = 16) -> None:
        assert dim <= 32
        self.dim = dim
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [(b - 127.5) / 127.5 for b in digest[: self.dim]]


class FakeSearch:
    """Provedor de busca web falso: devolve resultados fixos ou levanta o erro configurado."""

    def __init__(
        self, results: "list[WebResult] | None" = None, raises: Exception | None = None
    ) -> None:
        self._results = results or []
        self._raises = raises
        self.queries: list[str] = []

    async def search(self, query: str, max_results: int = 5) -> "list[WebResult]":
        self.queries.append(query)
        if self._raises is not None:
            raise self._raises
        return self._results[:max_results]


SMART_URL = "https://site.getnet.com.br/get-ajuda-maquininha/solucoes-get-smart/"
ADQ_URL = "https://site.getnet.com.br/get-ajuda-erros-maquininha/erro-adq-1-19/"
SMART_CHUNK_ID = "site-getnet-com-br-get-ajuda-maquininha-solucoes-get-smart-001"
ADQ_CHUNK_ID = "site-getnet-com-br-get-ajuda-erros-maquininha-erro-adq-1-19-001"
_SEED = [
    (SMART_URL, "Soluções Get Smart", "A Get Smart tem tela touch e impressora térmica integrada."),
    (
        ADQ_URL,
        "Erro ADQ 1-19: erro de conexão",
        "Erro ADQ 1-19: erro de conexão. Verifique o Wi-Fi ou o chip e reinicie a maquininha.",
    ),
]


async def make_deps(
    tmp_path: Path,
    llm: LLMClient,
    *,
    search: Any = None,
    seed: bool = True,
    clock: FrozenClock | None = None,
    **settings_overrides: Any,
) -> Deps:
    """Deps de teste: fixtures reais de clientes, índice Chroma em tmp_path com 2 chunks."""
    # replanejamento desligado por padrão: os roteiros dos testes antigos contam cada chamada;
    # os testes da T07 o ligam explicitamente
    settings_overrides.setdefault("max_replans", 0)
    index = KnowledgeIndex(str(tmp_path / "idx"), FakeEmbedder())
    if seed:
        for url, title, text in _SEED:
            chunks = chunk_document(
                text, source_url=url, title=title, fetched_at=datetime(2026, 11, 7, tzinfo=UTC)
            )
            await index.replace_document(url, chunks)
    return Deps(
        llm=llm,
        repo=JsonCustomerRepository("data/fixtures/customers.json"),
        index=index,
        search=search,
        clock=clock or FrozenClock(),
        settings=Settings(_env_file=None, **settings_overrides),  # type: ignore[call-arg]
        auth=dev_auth()[0],
        state=SqliteStateStore(tmp_path / "state.db"),
        dev_issuer=dev_auth()[1],
    )


@cache
def dev_auth() -> tuple[Authenticator, DevIssuer]:
    """Emissor local com chave efêmera, gerado uma vez por sessão de testes."""
    auth, issuer = build_auth(Settings(_env_file=None, app_env="development"))  # type: ignore[call-arg]
    assert issuer is not None
    return auth, issuer


CUSTOMER_SCOPES = frozenset({RECEIVABLES_READ, TERMINALS_READ, HANDOFFS_CREATE, HANDOFFS_READ})
DEV_SUB = {"cliente1988": "user-a", "cliente2000": "user-b", "desconhecido": "user-c"}


def principal_for(customer_id: str, scopes: frozenset[str] = CUSTOMER_SCOPES) -> Principal:
    return Principal(
        sub=DEV_SUB.get(customer_id, "user-x"),
        tenant_id="getnet",
        roles=frozenset({"customer"}),
        scopes=scopes,
        customer_ids=frozenset({customer_id}),
    )


def authorized(
    message: str, user_id: str = "cliente1988", scopes: frozenset[str] = CUSTOMER_SCOPES
) -> AuthorizedChat:
    """Pedido já autorizado, como `policies.authorize_chat` o entrega ao orquestrador."""
    return AuthorizedChat(
        message=message, customer_id=user_id, principal=principal_for(user_id, scopes)
    )


def claim(
    evidence_id: str,
    quote: str,
    *,
    obligation: str = "pergunta",
    statement: str | None = None,
) -> Claim:
    """Afirmação coberta que cita literalmente um trecho da evidência."""
    return Claim(
        obligation=obligation,
        status="covered",
        statement=statement or quote,
        evidence_id=evidence_id,
        quote=quote,
    )


def missing(obligation: str = "pergunta") -> Claim:
    return Claim(
        obligation=obligation, status="not_found", statement="", evidence_id=None, quote=None
    )


def kout(answer: str, *claims: Claim, pending: list[str] | None = None) -> KnowledgeOutput:
    return KnowledgeOutput(answer=answer, claims=list(claims), pending=pending or [])


def verdict(
    *supported: bool, uncovered: list[str] | None = None, consistent: bool = True
) -> VerificationOutput:
    return VerificationOutput(
        verdicts=[ClaimVerdict(claim=i, supported=s, reason="r") for i, s in enumerate(supported)],
        answers_question=not uncovered,
        uncovered=uncovered or [],
        answer_consistent=consistent,
    )


SMART_QUOTE = "A Get Smart tem tela touch e impressora térmica integrada."
ADQ_QUOTE = "Verifique o Wi-Fi ou o chip e reinicie a maquininha."


def knowledge_turns(answer: str, chunk_id: str, quote: str) -> list[LLMTurn]:
    """Knowledge que busca, responde citando um trecho sustentado e passa na verificação."""
    return [
        tool_call_turn("retrieve_getnet_knowledge", query="x"),
        final_turn(kout(answer, claim(chunk_id, quote))),
        final_turn(verdict(True)),
    ]
