from types import SimpleNamespace

import httpx
import openai
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from app.context import Budget, BudgetExceeded, ProviderUnavailable, RequestContext
from app.llm import LLMTurn, OpenAIClient, ToolSpec, run_agent
from app.schemas import AgentOutput, Evidence, ToolResult
from tests.fakes import FrozenClock, ScriptedLLM, final_turn, tool_call_turn


class EchoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


def make_ctx(budget: Budget | None = None) -> RequestContext:
    return RequestContext(
        request_id="r1", user_id="cliente1988", clock=FrozenClock(), budget=budget or Budget()
    )


def make_echo_tool(calls: list[str]) -> ToolSpec:
    async def run(args: BaseModel, ctx: RequestContext) -> ToolResult:
        assert isinstance(args, EchoArgs)
        calls.append(args.text)
        return ToolResult(
            status="ok",
            data={"echo": args.text},
            evidence=[Evidence(id="ev1", type="kb_chunk", title="T", url="https://x.test")],
        )

    return ToolSpec(name="echo", description="echo", args_model=EchoArgs, run=run)


async def test_run_agent_executes_tool_then_returns_final() -> None:
    calls: list[str] = []
    llm = ScriptedLLM(
        [tool_call_turn("echo", text="hello"), final_turn(AgentOutput(answer="done"))]
    )
    out, results = await run_agent(
        llm,
        make_ctx(),
        system="s",
        user="u",
        tools=[make_echo_tool(calls)],
        output_model=AgentOutput,
    )
    assert out.answer == "done"
    assert calls == ["hello"]
    assert len(results) == 1
    assert len(llm.calls) == 2
    tool_messages = [m for m in llm.calls[1] if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert "hello" in tool_messages[0]["content"]


async def test_unknown_tool_becomes_error_result() -> None:
    llm = ScriptedLLM([tool_call_turn("nope", x=1), final_turn(AgentOutput(answer="ok"))])
    _, results = await run_agent(
        llm, make_ctx(), system="s", user="u", tools=[], output_model=AgentOutput
    )
    assert [r.status for r in results] == ["error"]


async def test_extra_arg_user_id_is_rejected() -> None:
    calls: list[str] = []
    llm = ScriptedLLM(
        [
            tool_call_turn("echo", text="a", user_id="cliente2000"),
            final_turn(AgentOutput(answer="ok")),
        ]
    )
    _, results = await run_agent(
        llm,
        make_ctx(),
        system="s",
        user="u",
        tools=[make_echo_tool(calls)],
        output_model=AgentOutput,
    )
    assert results[0].status == "error"
    assert calls == []


async def test_tool_budget_exhausted_is_error_result_not_exception() -> None:
    calls: list[str] = []
    llm = ScriptedLLM(
        [
            tool_call_turn("echo", text="1"),
            tool_call_turn("echo", text="2"),
            final_turn(AgentOutput(answer="ok")),
        ]
    )
    _, results = await run_agent(
        llm,
        make_ctx(Budget(max_tool_calls=1)),
        system="s",
        user="u",
        tools=[make_echo_tool(calls)],
        output_model=AgentOutput,
    )
    assert [r.status for r in results] == ["ok", "error"]
    assert calls == ["1"]


async def test_evidence_registered_in_ctx() -> None:
    ctx = make_ctx()
    llm = ScriptedLLM([tool_call_turn("echo", text="x"), final_turn(AgentOutput(answer="ok"))])
    await run_agent(
        llm, ctx, system="s", user="u", tools=[make_echo_tool([])], output_model=AgentOutput
    )
    assert set(ctx.evidence) == {"ev1"}
    assert ctx.evidence["ev1"].url == "https://x.test"


async def test_llm_budget_exceeded_propagates() -> None:
    llm = ScriptedLLM([tool_call_turn("echo", text="x"), final_turn(AgentOutput(answer="ok"))])
    with pytest.raises(BudgetExceeded):
        await run_agent(
            llm,
            make_ctx(Budget(max_llm_calls=1)),
            system="s",
            user="u",
            tools=[make_echo_tool([])],
            output_model=AgentOutput,
        )


async def test_max_rounds_exceeded_raises_budget_exceeded() -> None:
    llm = ScriptedLLM([tool_call_turn("echo", text=str(i)) for i in range(5)])
    with pytest.raises(BudgetExceeded):
        await run_agent(
            llm,
            make_ctx(Budget(max_tool_calls=10)),
            system="s",
            user="u",
            tools=[make_echo_tool([])],
            output_model=AgentOutput,
            max_rounds=2,
        )


async def test_missing_structured_output_is_retried_once_then_succeeds() -> None:
    llm = ScriptedLLM([LLMTurn(content="texto livre"), final_turn(AgentOutput(answer="ok"))])
    out, _ = await run_agent(
        llm, make_ctx(), system="s", user="u", tools=[], output_model=AgentOutput
    )
    assert out.answer == "ok"
    assert len(llm.calls) == 2


async def test_missing_structured_output_twice_raises_provider_unavailable() -> None:
    llm = ScriptedLLM([LLMTurn(content="a"), LLMTurn(content="b")])
    with pytest.raises(ProviderUnavailable):
        await run_agent(llm, make_ctx(), system="s", user="u", tools=[], output_model=AgentOutput)


class _StubCompletions:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def parse(self, **kwargs: object) -> object:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _client_with(outcomes: list[object]) -> tuple[OpenAIClient, _StubCompletions]:
    client = OpenAIClient("sk-test", "model-x", retry_delay=0)
    stub = _StubCompletions(outcomes)
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=stub))  # type: ignore[assignment]
    return client, stub


def _completion(content: str = "ok") -> object:
    message = SimpleNamespace(content=content, tool_calls=None, parsed=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)


def _connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1"))


async def test_openai_client_retries_transient_error_once_and_charges_each_attempt() -> None:
    client, stub = _client_with([_connection_error(), _completion("hi")])
    budget = Budget()
    turn = await client.chat([], tools=None, output_model=None, budget=budget)
    assert turn.content == "hi"
    assert stub.calls == 2
    assert budget.llm_calls == 2


async def test_openai_client_persistent_transient_error_is_provider_unavailable() -> None:
    client, stub = _client_with([_connection_error(), _connection_error()])
    with pytest.raises(ProviderUnavailable):
        await client.chat([], tools=None, output_model=None, budget=Budget())
    assert stub.calls == 2


async def test_openai_client_schema_violation_becomes_empty_turn() -> None:
    try:
        AgentOutput.model_validate({"answer": 1, "extra": True})
    except ValidationError as exc:
        violation = exc
    client, _ = _client_with([violation])
    turn = await client.chat([], tools=None, output_model=AgentOutput, budget=Budget())
    assert turn.parsed is None
    assert turn.tool_calls == []


async def test_tool_results_are_recorded_on_the_context() -> None:
    ctx = make_ctx()
    llm = ScriptedLLM([tool_call_turn("echo", text="x"), final_turn(AgentOutput(answer="ok"))])
    await run_agent(
        llm, ctx, system="s", user="u", tools=[make_echo_tool([])], output_model=AgentOutput
    )
    assert [r.data for r in ctx.tool_results] == [{"echo": "x"}]


async def test_openai_client_records_token_usage_on_budget() -> None:
    completion = _completion("hi")
    completion.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5)  # type: ignore[attr-defined]
    client, _ = _client_with([completion])
    budget = Budget()
    await client.chat([], tools=None, output_model=None, budget=budget)
    assert (budget.prompt_tokens, budget.completion_tokens) == (10, 5)


async def test_after_max_rounds_a_final_turn_without_tools_forces_the_answer() -> None:
    llm = ScriptedLLM(
        [
            tool_call_turn("echo", text="1"),
            tool_call_turn("echo", text="2"),
            final_turn(AgentOutput(answer="resposta final")),
        ]
    )
    out, results = await run_agent(
        llm,
        make_ctx(Budget(max_tool_calls=10)),
        system="s",
        user="u",
        tools=[make_echo_tool([])],
        output_model=AgentOutput,
        max_rounds=2,
    )
    assert out.answer == "resposta final"
    assert len(results) == 2
    assert len(llm.calls) == 3
    assert llm.tools_seen[-1] is None
