import pytest

from app.agents.router import PlanError, route, validate_plan
from app.context import Budget, RequestContext
from app.schemas import RoutePlan, Task
from tests.fakes import FrozenClock, ScriptedLLM, final_turn


def plan(workflow: str, *agents: str, clarification: str | None = None) -> RoutePlan:
    return RoutePlan(
        workflow=workflow,  # type: ignore[arg-type]
        tasks=[Task(agent=a, question="q") for a in agents],  # type: ignore[arg-type]
        clarification=clarification,
    )


def ctx() -> RequestContext:
    return RequestContext(
        request_id="r", user_id="cliente1988", clock=FrozenClock(), budget=Budget()
    )


VALID = [
    plan("knowledge", "knowledge"),
    plan("support", "support"),
    plan("support_then_knowledge", "support", "knowledge"),
    plan("combined", "knowledge", "support"),
    plan("combined", "support", "knowledge"),
    plan("clarify", clarification="Qual terminal?"),
]

INVALID = [
    plan("knowledge", "support"),
    plan("knowledge"),
    plan("support", "knowledge"),
    plan("support_then_knowledge", "knowledge", "support"),
    plan("support_then_knowledge", "support"),
    plan("combined", "knowledge", "knowledge"),
    plan("combined", "support"),
    plan("clarify", "knowledge", clarification="x"),
    plan("clarify"),
]


@pytest.mark.parametrize("candidate", VALID)
def test_validate_plan_accepts_each_workflow_shape(candidate: RoutePlan) -> None:
    validate_plan(candidate)


@pytest.mark.parametrize("candidate", INVALID)
def test_validate_plan_rejects_wrong_order_and_wrong_agents(candidate: RoutePlan) -> None:
    with pytest.raises(PlanError):
        validate_plan(candidate)


async def test_route_retries_once_on_invalid_plan_then_succeeds() -> None:
    good = plan("knowledge", "knowledge")
    llm = ScriptedLLM([final_turn(plan("knowledge", "support")), final_turn(good)])
    result = await route(llm, ctx(), "O que é a Get Smart?")
    assert result == good
    assert len(llm.calls) == 2
    assert "inválido" in llm.calls[1][-1]["content"].lower()


async def test_route_falls_back_to_clarify_after_two_invalid_plans() -> None:
    bad = plan("support", "knowledge")
    llm = ScriptedLLM([final_turn(bad), final_turn(bad)])
    result = await route(llm, ctx(), "???")
    assert result.workflow == "clarify"
    assert result.tasks == []
    assert result.clarification


async def test_route_treats_missing_structured_output_as_invalid() -> None:
    from app.llm import LLMTurn

    llm = ScriptedLLM([LLMTurn(content="texto livre"), final_turn(plan("knowledge", "knowledge"))])
    result = await route(llm, ctx(), "oi")
    assert result.workflow == "knowledge"


async def test_route_router_never_receives_tools_or_user_id() -> None:
    llm = ScriptedLLM([final_turn(plan("knowledge", "knowledge"))])
    await route(llm, ctx(), "O que é a Get Smart?")
    assert llm.tools_seen == [None]
    assert llm.output_models == [RoutePlan]
    sent = " ".join(str(m["content"]) for m in llm.calls[0])
    assert "cliente1988" not in sent
    assert "O que é a Get Smart?" in sent
    assert "2026-11-07" in llm.calls[0][0]["content"]
