"""Router Agent: transforma a mensagem em um plano pequeno e validado pelo código."""

from app.context import RequestContext
from app.llm import LLMClient
from app.prompts import ROUTER_SYSTEM, today_label
from app.schemas import RoutePlan

GENERIC_CLARIFICATION = (
    "Não consegui entender seu pedido. Pode descrever com mais detalhes o que você precisa "
    "sobre a Getnet (produtos, vendas ou sua maquininha)?"
)


class PlanError(Exception):
    """O plano do Router não corresponde a um fluxo permitido."""


def validate_plan(plan: RoutePlan) -> None:
    agents = [t.agent for t in plan.tasks]
    expected: dict[str, list[str]] = {
        "knowledge": ["knowledge"],
        "support": ["support"],
        "support_then_knowledge": ["support", "knowledge"],
    }
    if plan.workflow == "clarify":
        if agents or not (plan.clarification or "").strip():
            raise PlanError("clarify requires no tasks and a clarification question")
    elif plan.workflow == "combined":
        if sorted(agents) != ["knowledge", "support"]:
            raise PlanError("combined requires exactly one support and one knowledge task")
    elif agents != expected[plan.workflow]:
        raise PlanError(f"{plan.workflow} requires tasks {expected[plan.workflow]}, got {agents}")


async def route(llm: LLMClient, ctx: RequestContext, message: str) -> RoutePlan:
    """Uma chamada estruturada, sem ferramentas e sem identidade do cliente.
    Plano inválido: uma nova tentativa; persistindo, pede esclarecimento genérico."""
    messages: list[dict[str, str]] = [
        {"role": "system", "content": ROUTER_SYSTEM.format(today=today_label(ctx.clock.now()))},
        {"role": "user", "content": message},
    ]
    for attempt in range(2):
        turn = await llm.chat(messages, tools=None, output_model=RoutePlan, budget=ctx.budget)
        if isinstance(turn.parsed, RoutePlan):
            try:
                validate_plan(turn.parsed)
            except PlanError as exc:
                reason = str(exc)
                messages.append({"role": "assistant", "content": turn.parsed.model_dump_json()})
            else:
                return turn.parsed
        else:
            reason = "resposta sem saída estruturada"
        if attempt == 0:
            messages.append(
                {
                    "role": "user",
                    "content": f"Plano inválido: {reason}. Responda de novo com um plano válido.",
                }
            )
    return RoutePlan(workflow="clarify", clarification=GENERIC_CLARIFICATION)
