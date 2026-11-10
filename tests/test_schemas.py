import pytest
from pydantic import ValidationError

from app.schemas import ChatRequest, RoutePlan, Task


def _task(agent: str = "knowledge") -> dict[str, str]:
    return {"agent": agent, "question": "q"}


def test_route_plan_rejects_three_tasks() -> None:
    with pytest.raises(ValidationError):
        RoutePlan(workflow="combined", tasks=[_task(), _task(), _task()])  # type: ignore[list-item]


def test_route_plan_rejects_unknown_workflow() -> None:
    with pytest.raises(ValidationError):
        RoutePlan(workflow="chitchat", tasks=[])  # type: ignore[arg-type]


def test_route_plan_accepts_valid_plan() -> None:
    plan = RoutePlan(workflow="knowledge", tasks=[Task(agent="knowledge", question="q")])
    assert plan.tasks[0].source_scope == "auto"


@pytest.mark.parametrize(
    ("message", "user_id"),
    [
        ("", "cliente1988"),
        ("   ", "cliente1988"),
        ("x" * 2001, "cliente1988"),
        ("oi", ""),
        ("oi", "../x"),
        ("oi", "a" * 65),
        ("oi", "cliente 1"),
    ],
)
def test_chat_request_rejects_invalid_input(message: str, user_id: str) -> None:
    with pytest.raises(ValidationError):
        ChatRequest(message=message, user_id=user_id)


def test_chat_request_accepts_cliente1988() -> None:
    req = ChatRequest(message="  Quando recebo?  ", user_id="cliente1988")
    assert req.message == "Quando recebo?"
    assert req.user_id == "cliente1988"


def _keys(node: object) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in _keys(v)}
    if isinstance(node, list):
        return {k for v in node for k in _keys(v)}
    return set()


def test_llm_facing_schemas_avoid_keywords_risky_for_strict_structured_outputs() -> None:
    from openai.lib._pydantic import to_strict_json_schema

    from app.schemas import AgentOutput

    for model in (RoutePlan, AgentOutput):
        used = _keys(to_strict_json_schema(model))
        assert not used & {"default", "maxItems", "minItems", "minLength", "maxLength", "pattern"}


def test_route_plan_task_source_scope_still_defaults_to_auto() -> None:
    assert Task(agent="support", question="q").source_scope == "auto"
