"""Autorização por recurso (T02). Decisões puras sobre o `Principal`: nenhuma depende de texto
da conversa nem de campo escolhido pelo LLM.

Política de existência: um recurso de outro dono (conversa, execução, caso) responde como
inexistente (404), para não revelar que existe. O seletor de cliente do /chat é diferente: o
chamador já o informou, e a recusa é 403 igual para cliente existente ou não.
"""

from dataclasses import dataclass

from app.auth import Principal

RECEIVABLES_READ = "receivables:read"
TERMINALS_READ = "terminals:read"
HANDOFFS_CREATE = "handoffs:create"
HANDOFFS_READ = "handoffs:read"
HANDOFFS_RESOLVE = "handoffs:resolve"
OPERATOR_ROLE = "operator"


class Forbidden(Exception):
    """Principal autenticado sem acesso ao recurso pedido (403)."""


class InsufficientScope(Forbidden):
    """Principal autenticado sem o escopo exigido (403)."""


class ResourceNotFound(Exception):
    """Recurso inexistente ou de outro dono: as duas situações respondem igual (404)."""


class CustomerSelectionRequired(Exception):
    """O principal acessa mais de um estabelecimento e não disse qual (400)."""


@dataclass(frozen=True)
class AuthorizedChat:
    """Pedido de chat já autorizado: o cliente efetivo foi confrontado com o principal."""

    message: str
    customer_id: str
    principal: Principal


def authorize_chat(principal: Principal, message: str, user_id: str | None) -> AuthorizedChat:
    """`user_id` do payload é só um seletor: precisa estar entre os estabelecimentos do
    principal. Roda antes do Router, então nenhuma chamada ao LLM acontece se falhar."""
    if user_id is None:
        if not principal.customer_ids:
            raise Forbidden("principal has no customer access")
        if len(principal.customer_ids) > 1:
            raise CustomerSelectionRequired("user_id is required for this principal")
        (user_id,) = principal.customer_ids
    elif user_id not in principal.customer_ids:
        raise Forbidden("customer not authorized for principal")
    return AuthorizedChat(message=message, customer_id=user_id, principal=principal)


def require_scope(principal: Principal, scope: str) -> None:
    if scope not in principal.scopes:
        raise InsufficientScope(scope)


def authorize_owned(principal: Principal, owner_customer_id: str, scope: str) -> None:
    """Recurso de um cliente (conversa, execução, caso): dono primeiro, escopo depois, para que
    um recurso alheio nunca devolva uma resposta diferente de 'não encontrado'."""
    if owner_customer_id not in principal.customer_ids:
        raise ResourceNotFound("resource not found")
    require_scope(principal, scope)


def authorize_operator(principal: Principal, tenant_id: str, scope: str) -> None:
    """Operador age só no próprio tenant; o papel não concede acesso a outros tenants."""
    if OPERATOR_ROLE not in principal.roles or principal.tenant_id != tenant_id:
        raise Forbidden("operator access denied")
    require_scope(principal, scope)
