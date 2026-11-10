"""Exercita o `chat.completions.parse` REAL do SDK da OpenAI (só a rede é simulada).

Os testes com stub de `parse` não pegam recusas do SDK, como "ferramenta não é strict"."""

import json
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
from pydantic import BaseModel

from app.context import Budget, ProviderUnavailable
from app.llm import LLMTurn, OpenAIClient, ToolSpec
from app.rag import KnowledgeIndex
from app.repositories import JsonCustomerRepository
from app.schemas import AgentOutput
from app.tools.customer import DiagnosticsArgs, build_customer_tools
from app.tools.knowledge import RetrieveArgs, build_knowledge_tools
from tests.fakes import FakeEmbedder, FakeSearch


def completion(message: dict[str, Any], finish: str = "stop") -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "model-x",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }


FINAL = {
    "role": "assistant",
    "refusal": None,
    "content": json.dumps({"answer": "ok", "evidence_ids": [], "pending": []}),
}
TOOL_CALL = {
    "role": "assistant",
    "refusal": None,
    "content": None,
    "tool_calls": [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_receivables", "arguments": '{"sales_date": "2026-11-06"}'},
        }
    ],
}


def make_client(handler: Any, retry_delay: float = 0.0, reasoning_effort: str = "") -> OpenAIClient:
    sdk = openai.AsyncOpenAI(
        api_key="sk-test",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return OpenAIClient(
        "sk-test", "model-x", client=sdk, retry_delay=retry_delay, reasoning_effort=reasoning_effort
    )


def all_tools(tmp_path: Path) -> list[ToolSpec]:
    customer = build_customer_tools(JsonCustomerRepository("data/fixtures/customers.json"))
    index = KnowledgeIndex(str(tmp_path / "idx"), FakeEmbedder())
    return [*customer, *build_knowledge_tools(index, FakeSearch())]


async def test_real_sdk_parse_accepts_every_tool_in_strict_mode(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion(FINAL))

    turn = await make_client(handler).chat(
        [{"role": "user", "content": "x"}],
        tools=all_tools(tmp_path),
        output_model=AgentOutput,
        budget=Budget(),
    )
    assert isinstance(turn.parsed, AgentOutput)
    tools = seen["body"]["tools"]
    assert {t["function"]["name"] for t in tools} == {
        "get_receivables",
        "get_terminal_diagnostics",
        "retrieve_getnet_knowledge",
        "web_search",
    }
    for tool in tools:
        function = tool["function"]
        params = function["parameters"]
        assert function["strict"] is True
        assert params["additionalProperties"] is False
        assert set(params["required"]) == set(params["properties"])
        assert "default" not in json.dumps(params)
    assert seen["body"]["response_format"]["type"] == "json_schema"


async def test_real_sdk_parse_returns_tool_calls(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=completion(TOOL_CALL, finish="tool_calls"))

    turn = await make_client(handler).chat(
        [{"role": "user", "content": "x"}],
        tools=all_tools(tmp_path),
        output_model=AgentOutput,
        budget=Budget(),
    )
    assert turn.parsed is None
    assert [(c.name, json.loads(c.arguments)) for c in turn.tool_calls] == [
        ("get_receivables", {"sales_date": "2026-11-06"})
    ]


def test_tool_args_accept_the_explicit_nulls_strict_mode_forces() -> None:
    assert DiagnosticsArgs.model_validate_json('{"terminal_id": null}').terminal_id is None
    args = RetrieveArgs.model_validate_json('{"query": "x", "market": "BR", "topic": null}')
    assert args.topic is None


async def test_transient_error_retry_waits_before_trying_again() -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json=completion(FINAL))

    client = make_client(handler, retry_delay=1.5)
    client._sleep = fake_sleep  # type: ignore[attr-defined]
    turn: LLMTurn = await client.chat([], tools=None, output_model=AgentOutput, budget=Budget())
    assert isinstance(turn.parsed, AgentOutput)
    assert sleeps == [1.5]


@pytest.mark.parametrize("model", [AgentOutput])
def test_pydantic_models_used_as_output_are_base_models(model: type[BaseModel]) -> None:
    assert issubclass(model, BaseModel)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (
            401,
            {"error": {"message": "Incorrect API key sk-abc", "code": "invalid_api_key"}},
            "AuthenticationError (HTTP 401, code=invalid_api_key)",
        ),
        (
            404,
            {"error": {"message": "model gone", "code": "model_not_found"}},
            "NotFoundError (HTTP 404, code=model_not_found): model gone",
        ),
        (
            400,
            {"error": {"message": "bad schema", "code": "invalid_request"}},
            "BadRequestError (HTTP 400, code=invalid_request): bad schema",
        ),
    ],
)
async def test_provider_error_reason_names_type_status_code_and_request_error_message(
    status: int, body: dict[str, Any], expected: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    with pytest.raises(ProviderUnavailable) as caught:
        await make_client(handler).chat([], tools=None, output_model=None, budget=Budget())
    assert str(caught.value) == expected
    assert "sk-abc" not in str(caught.value)


async def test_persistent_rate_limit_reason_includes_status_and_code() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "x", "code": "insufficient_quota"}})

    with pytest.raises(ProviderUnavailable) as caught:
        await make_client(handler).chat([], tools=None, output_model=None, budget=Budget())
    assert str(caught.value) == "RateLimitError (HTTP 429, code=insufficient_quota)"


async def test_request_error_message_is_truncated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "x" * 1000, "code": "c"}})

    with pytest.raises(ProviderUnavailable) as caught:
        await make_client(handler).chat([], tools=None, output_model=None, budget=Budget())
    assert len(str(caught.value)) < 400


@pytest.mark.parametrize(("configured", "expected"), [("none", "none"), ("low", "low"), ("", None)])
async def test_reasoning_effort_is_sent_only_when_configured(
    tmp_path: Path, configured: str, expected: str | None
) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion(FINAL))

    await make_client(handler, reasoning_effort=configured).chat(
        [{"role": "user", "content": "x"}],
        tools=all_tools(tmp_path),
        output_model=AgentOutput,
        budget=Budget(),
    )
    assert seen["body"].get("reasoning_effort") == expected
    assert ("reasoning_effort" in seen["body"]) is (expected is not None)


def test_build_deps_passes_reasoning_effort_to_the_llm_client(tmp_path: Path) -> None:
    from pydantic import SecretStr

    from app.config import Settings
    from app.main import build_deps

    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        openai_api_key=SecretStr("sk-test"),
        openai_chat_model="gpt-6-luna",
        openai_reasoning_effort="none",
        chroma_path=str(tmp_path / "idx"),
    )
    deps = build_deps(settings)
    assert deps.llm._reasoning_effort == "none"  # type: ignore[attr-defined]
