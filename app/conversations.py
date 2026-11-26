"""Turno durável (T04): conversa autorizada, idempotência, esclarecimento em dois turnos.

O orquestrador continua sem estado; aqui fica o que atravessa requisições. O banco só é tocado
antes (abrir o turno) e depois (gravar o resultado) da execução, nunca durante ela.
"""

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.deps import Deps
from app.orchestrator import run_chat
from app.policies import HANDOFFS_CREATE, AuthorizedChat, ResourceNotFound
from app.schemas import ChatResponse, RoutePlan
from app.state import Conversation, HandoffDraft, Message, RunRecord, TurnResult, TurnStart
from app.tools.customer import format_terminal_options

_LEASE_MARGIN_SECONDS = 5.0
_HISTORY_MESSAGES = 6

_ORDINALS = [
    (r"\b(primeir[oa]|first|1[ºo°]?)\b", 0),
    (r"\b(segund[oa]|second|2[ºo°]?)\b", 1),
    (r"\b(terceir[oa]|third|3[ºo°]?)\b", 2),
    (r"\b(quart[oa]|fourth|4[ºo°]?)\b", 3),
    (r"\b([úu]ltim[oa]|last)\b", -1),
]
STALE_OPTIONS = (
    "A lista de terminais mudou desde a pergunta anterior, então não posso usar a posição "
    "escolhida. "
)
UNRESOLVED_CHOICE = "Não identifiquei qual terminal da lista você escolheu. "


@dataclass
class _TurnOutput:
    response: ChatResponse
    pending: dict[str, Any] | None
    llm_calls: int = 0
    tool_calls: int = 0
    handoff: HandoffDraft | None = None


class IdempotencyConflict(Exception):
    """Mesma chave de idempotência com outro conteúdo (409)."""


class RequestInProgress(Exception):
    """A requisição com essa chave ainda está em execução (409)."""


def request_hash(chat: AuthorizedChat, conversation_id: str | None) -> str:
    canonical = json.dumps(
        {
            "message": chat.message,
            "customer_id": chat.customer_id,
            "conversation_id": conversation_id,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def parse_choice(message: str, options: list[str]) -> tuple[bool, str | None]:
    """(tentou escolher, terminal escolhido). Código do terminal vale sempre; posição só se a
    lista tiver esse item. Mensagens que não parecem escolha seguem o fluxo normal."""
    text = message.lower()
    for option in options:
        if option.lower() in text:
            return True, option
    if len(text) > 60:  # uma escolha é curta; frases longas são pedidos novos
        return False, None
    for pattern, index in _ORDINALS:
        if re.search(pattern, text):
            in_range = -len(options) <= index < len(options)
            return True, options[index] if in_range else None
    return False, None


def authorize_conversation(
    conversation: Conversation | None, chat: AuthorizedChat, now: float
) -> Conversation:
    """Conversa só continua para o mesmo principal, tenant e cliente, e se não expirou; qualquer
    outra situação responde igual a inexistente, sem revelar que existe."""
    principal = chat.principal
    if (
        conversation is None
        or conversation.expires_at <= now
        or conversation.tenant_id != principal.tenant_id
        or conversation.owner_sub != principal.sub
        or conversation.customer_id != chat.customer_id
        or conversation.customer_id not in principal.customer_ids
    ):
        raise ResourceNotFound("conversation not found")
    return conversation


def _pending_choice(conversation: Conversation, now: float) -> dict[str, Any] | None:
    pending = conversation.pending
    if not pending or pending.get("kind") != "terminal_choice":
        return None
    if pending["expires_at"] <= now:
        return None
    return pending


async def handle_turn(
    chat: AuthorizedChat,
    deps: Deps,
    *,
    conversation_id: str | None = None,
    idempotency_key: str | None = None,
) -> ChatResponse:
    store = deps.state
    settings = deps.settings
    now_dt = deps.clock.now()
    now = now_dt.timestamp()
    lease_until = now + settings.deadline_seconds + _LEASE_MARGIN_SECONDS
    expires_at = now + timedelta(hours=settings.conversation_retention_hours).total_seconds()
    store.purge_expired(now)
    digest = request_hash(chat, conversation_id)
    principal = chat.principal

    run: RunRecord | None = None
    if idempotency_key is not None:
        run = store.find_run(principal.tenant_id, principal.sub, idempotency_key)
        if run is not None:
            if run.request_hash != digest:
                raise IdempotencyConflict(idempotency_key)
            if run.state == "completed" and run.response is not None:
                return ChatResponse(**run.response)  # repetição após perda de conexão
            if run.state == "running" and run.lease_until > now:
                raise RequestInProgress(idempotency_key)
            conversation_id = run.conversation_id

    if conversation_id is not None:
        authorize_conversation(store.get_conversation(conversation_id), chat, now)

    if run is not None:
        run = store.restart_run(run.id, now, lease_until)
        conversation = authorize_conversation(
            store.get_conversation(run.conversation_id), chat, now
        )
    else:
        start = TurnStart(
            tenant_id=principal.tenant_id,
            owner_sub=principal.sub,
            customer_id=chat.customer_id,
            request_hash=digest,
            idempotency_key=idempotency_key,
            now=now,
            lease_until=lease_until,
            expires_at=expires_at,
        )
        try:
            conversation, run = store.start_turn(conversation_id, start)
        except sqlite3.IntegrityError as exc:  # mesma chave chegou em paralelo
            raise RequestInProgress(idempotency_key or "") from exc

    history = [(m.role, m.content) for m in store.messages(conversation.id, _HISTORY_MESSAGES)]
    try:
        out = await _execute(chat, deps, conversation, history, now)
    except BaseException:
        store.fail_run(run.id, deps.clock.now().timestamp())
        raise

    update: dict[str, Any] = {"conversation_id": conversation.id, "run_id": run.id}
    if out.handoff is not None:
        update |= {"handoff_id": out.handoff.id, "run_status": "awaiting_operator"}
    response = out.response.model_copy(update=update)
    store.complete_turn(
        run.id,
        conversation.id,
        conversation.version,
        TurnResult(
            user_message=chat.message,
            assistant_message=response.answer,
            response=response.model_dump(mode="json"),
            pending=out.pending,
            llm_calls=out.llm_calls,
            tool_calls=out.tool_calls,
            handoff=out.handoff,
            now=deps.clock.now().timestamp(),
            expires_at=expires_at,
        ),
    )
    return response


async def _execute(
    chat: AuthorizedChat,
    deps: Deps,
    conversation: Conversation,
    history: Sequence[tuple[str, str]],
    now: float,
) -> _TurnOutput:
    """Resolve um esclarecimento pendente ou executa um pedido novo."""
    pending = _pending_choice(conversation, now)
    plan: RoutePlan | None = None
    selected: str | None = None
    message = chat.message
    if pending is not None:
        attempted, choice = parse_choice(chat.message, pending["options"])
        if attempted:
            current = [t.id for t in deps.repo.terminals_for(chat.customer_id, deps.clock.now())]
            if current != pending["options"]:
                # a posição só vale para a lista apresentada; lista nova, nova pergunta
                return _ask_again(STALE_OPTIONS, current, pending, now, deps)
            if choice is None:
                return _ask_again(UNRESOLVED_CHOICE, current, pending, now, deps)
            plan = RoutePlan.model_validate(pending["plan"])
            selected = choice
            message = f"{pending['question']}\n(Terminal escolhido pelo cliente: {choice})"

    # o caso só é aberto se o principal puder criá-lo; senão a resposta fica na recomendação
    can_open = HANDOFFS_CREATE in chat.principal.scopes
    handoff_id = f"hof_{uuid.uuid4().hex[:16]}" if can_open else None
    outcome = await run_chat(
        AuthorizedChat(message=message, customer_id=chat.customer_id, principal=chat.principal),
        deps,
        history=history,
        plan=plan,
        selected_terminal=selected,
        handoff_id=handoff_id,
    )
    new_pending = None
    if outcome.terminal_options and outcome.response.status in ("needs_clarification", "partial"):
        new_pending = {
            "kind": "terminal_choice",
            "options": outcome.terminal_options,
            "plan": outcome.plan.model_dump(mode="json"),
            "question": message,
            "expires_at": now + deps.settings.pending_choice_minutes * 60,
        }
    handoff = None
    if outcome.handoff is not None and handoff_id is not None:
        handoff = HandoffDraft(
            id=handoff_id,
            reason=outcome.handoff["reason"],
            snapshot=outcome.handoff,
            expires_at=now + deps.settings.handoff_ttl_hours * 3600,
        )
    return _TurnOutput(
        outcome.response, new_pending, outcome.llm_calls, outcome.tool_calls, handoff
    )


def _ask_again(
    reason: str, current: list[str], pending: dict[str, Any], now: float, deps: Deps
) -> _TurnOutput:
    """Pergunta de novo sem chamar o LLM, com a lista atual guardada para a próxima escolha."""
    response = ChatResponse(
        request_id=f"req_{uuid.uuid4().hex[:12]}",
        status="needs_clarification",
        answer=reason + format_terminal_options(current),
        agents_used=[],
        workflow="support",
    )
    renewed = {
        **pending,
        "options": current,
        "expires_at": now + deps.settings.pending_choice_minutes * 60,
    }
    return _TurnOutput(response, renewed if current else None)


def conversation_view(messages: list[Message], conversation: Conversation) -> dict[str, Any]:
    return {
        "conversation_id": conversation.id,
        "messages": [m.model_dump() for m in messages],
        "pending": (conversation.pending or {}).get("kind"),
    }
