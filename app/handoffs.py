"""Atendimento humano (T05): fila interna, decisão do operador e retomada.

O operador decide sobre um snapshot sem dados financeiros nem identificadores do cliente além do
estabelecimento. A decisão e o evento de retomada são gravados juntos; um worker do próprio
processo consome o evento, revalida o acesso do cliente e entrega a resposta na conversa uma
única vez. A resposta humana aparece como tal: não vira fato oficial nem altera dado de conta.
"""

import asyncio
import contextlib
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from app.auth import AccessRepository, Principal
from app.context import Clock
from app.observability import log_event
from app.policies import ResourceNotFound, authorize_operator, authorize_owned
from app.state import Handoff, StateStore

HUMAN_REPLY = (
    "Atualização do caso {id}: resposta do atendimento humano.\n\n{content}\n\n"
    "Esta orientação foi escrita por um atendente da equipe de suporte; ela não altera os dados "
    "da sua conta nem substitui as informações oficiais da Getnet."
)
NO_ACTION = (
    "Atualização do caso {id}: o atendimento humano analisou o seu pedido e encerrou o caso "
    "sem uma orientação adicional.\n\nMotivo informado: {content}"
)

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


class ClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int


class OperatorDecision(BaseModel):
    """Decisão estruturada sobre a versão do caso que o operador revisou."""

    model_config = ConfigDict(extra="forbid")
    decision_type: Literal["reply", "no_action"]
    content: _Text
    justification: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ]
    expected_version: int


def operator_handoff(
    store: StateStore, principal: Principal, handoff_id: str, scope: str
) -> Handoff:
    """Caso de outro tenant responde como inexistente; papel ou escopo ausente → 403."""
    handoff = store.get_handoff(handoff_id)
    if handoff is None or handoff.tenant_id != principal.tenant_id:
        raise ResourceNotFound("handoff not found")
    authorize_operator(principal, handoff.tenant_id, scope)
    return handoff


def customer_handoff(
    store: StateStore, principal: Principal, handoff_id: str, scope: str
) -> Handoff:
    """O cliente só vê o próprio caso: mesmo principal e cliente ainda autorizado."""
    handoff = store.get_handoff(handoff_id)
    if handoff is None or handoff.owner_sub != principal.sub:
        raise ResourceNotFound("handoff not found")
    authorize_owned(principal, handoff.customer_id, scope)
    return handoff


def effective_state(handoff: Handoff, now: float) -> str:
    """Caso aberto que passou do prazo aparece como `expired`: ninguém mais pode decidi-lo, então
    não fica na fila nem parece em andamento para o cliente. Derivado na leitura, sem migração."""
    if handoff.state in ("pending", "assigned") and handoff.expires_at <= now:
        return "expired"
    return handoff.state


def operator_view(handoff: Handoff, now: float) -> dict[str, Any]:
    return {
        "id": handoff.id,
        "state": effective_state(handoff, now),
        "version": handoff.version,
        "reason": handoff.reason,
        "customer_id": handoff.customer_id,
        "snapshot": handoff.snapshot,
        "assignee": handoff.assignee,
        "decision": handoff.decision,
        "created_at": handoff.created_at,
        "expires_at": handoff.expires_at,
    }


def customer_view(handoff: Handoff, now: float) -> dict[str, Any]:
    return {
        "id": handoff.id,
        "state": effective_state(handoff, now),
        "decision_type": (handoff.decision or {}).get("decision_type"),
        "conversation_id": handoff.conversation_id,
        "expires_at": handoff.expires_at,
    }


def render_resume(handoff: Handoff) -> str:
    decision = handoff.decision or {}
    template = HUMAN_REPLY if decision.get("decision_type") == "reply" else NO_ACTION
    return template.format(id=handoff.id, content=decision.get("content", ""))


class ResumeWorker:
    """Consome eventos de retomada com lease: se o processo cair no meio, outro ciclo retoma o
    evento, e a entrega deduplicada impede mensagem repetida."""

    def __init__(
        self, store: StateStore, access: AccessRepository, clock: Clock, lease_seconds: float = 30
    ) -> None:
        self._store = store
        self._access = access
        self._clock = clock
        self._lease = lease_seconds

    def process_once(self) -> bool:
        now = self._clock.now().timestamp()
        event = self._store.claim_resume_event(now, now + self._lease)
        if event is None:
            return False
        handoff = self._store.get_handoff(event.handoff_id)
        if handoff is None:  # conversa expirou e levou o caso junto
            return True
        allowed = self._access.customers_for(handoff.tenant_id, handoff.owner_sub)
        if handoff.customer_id not in allowed:
            # acesso revogado depois da decisão: a resposta não é entregue
            delivered = self._store.complete_resume(event, None, "access_revoked", now)
        else:
            delivered = self._store.complete_resume(event, render_resume(handoff), "delivered", now)
        log_event("handoff_resumed", handoff_id=handoff.id, delivered=delivered)
        return True

    async def run(self, stop: asyncio.Event, interval: float = 1.0) -> None:
        while not stop.is_set():
            while self.process_once():
                pass
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
