"""Camada de LLM: contrato do cliente, cliente OpenAI e o loop de tool calling compartilhado.

O orçamento de chamadas ao LLM é cobrado pelo próprio cliente (a cada tentativa), de modo que
clientes reais e falsos obedecem ao mesmo limite.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

import openai
from pydantic import BaseModel, ValidationError

from app.context import Budget, BudgetExceeded, ProviderUnavailable, RequestContext
from app.observability import log_event
from app.schemas import ToolResult

T = TypeVar("T", bound=BaseModel)


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str


class LLMTurn(BaseModel):
    content: str | None = None
    tool_calls: list[ToolCall] = []
    parsed: BaseModel | None = None
    usage: dict[str, int] = {}


@dataclass
class ToolSpec:
    """Ferramenta exposta ao LLM. `args_model` define só o que o LLM pode escolher:
    identidade e contexto do cliente nunca entram aqui (vêm do `RequestContext`)."""

    name: str
    description: str
    args_model: type[BaseModel]
    run: Callable[[Any, RequestContext], Awaitable[ToolResult]]
    uses_web: bool = False


class LLMClient(Protocol):
    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolSpec] | None,
        output_model: type[BaseModel] | None,
        budget: Budget,
    ) -> LLMTurn: ...


def _tool_schema(spec: ToolSpec) -> dict[str, Any]:
    """Schema `strict` pelo helper do SDK: `chat.completions.parse` recusa ferramentas não-strict,
    e o modo estrito exige todos os campos obrigatórios (opcionais viram `null` explícito)."""
    tool = openai.pydantic_function_tool(
        spec.args_model, name=spec.name, description=spec.description
    )
    return dict(tool)


def describe_provider_error(exc: Exception | None) -> str:
    """Motivo curto e seguro para log: tipo, status HTTP, código e, para erros de requisição,
    a mensagem truncada."""
    if exc is None:
        return "unknown"
    parts = []
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    if status is not None:
        parts.append(f"HTTP {status}")
    if isinstance(code, str):
        parts.append(f"code={code}")
    reason = f"{type(exc).__name__} ({', '.join(parts)})" if parts else type(exc).__name__
    # só erros de requisição (400/404/422): a mensagem descreve o problema na requisição e não
    # traz credenciais; em 401/403 ela pode conter pedaço da chave, então fica de fora
    if status in (400, 404, 422):
        body = getattr(exc, "body", None)
        message = body.get("message") if isinstance(body, dict) else getattr(exc, "message", None)
        if isinstance(message, str):
            reason += f": {message[:300]}"
    return reason


class OpenAIClient:
    _TRANSIENT = (
        openai.APIConnectionError,
        openai.APITimeoutError,
        openai.RateLimitError,
        openai.InternalServerError,
    )

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        client: openai.AsyncOpenAI | None = None,
        retry_delay: float = 1.0,
        reasoning_effort: str = "",
    ) -> None:
        self._client = client or openai.AsyncOpenAI(api_key=api_key, timeout=timeout, max_retries=0)
        self._model = model
        self._retry_delay = retry_delay
        # modelos de raciocínio recusam ferramentas no Chat Completions sem `reasoning_effort`
        # (ex.: 'none'); outros modelos rejeitam o parâmetro, por isso só é enviado se configurado
        self._reasoning_effort = reasoning_effort
        self._sleep = asyncio.sleep

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[ToolSpec] | None,
        output_model: type[BaseModel] | None,
        budget: Budget,
    ) -> LLMTurn:
        kwargs: dict[str, Any] = {"model": self._model, "messages": messages}
        if tools:
            kwargs["tools"] = [_tool_schema(t) for t in tools]
        if output_model is not None:
            kwargs["response_format"] = output_model
        if self._reasoning_effort:
            kwargs["reasoning_effort"] = self._reasoning_effort

        last_error: Exception | None = None
        for attempt in range(2):  # uma nova tentativa para erro transitório, após uma pausa
            if attempt:
                await self._sleep(self._retry_delay)
            budget.charge_llm()
            try:
                completion = await self._client.chat.completions.parse(**kwargs)
            except self._TRANSIENT as exc:
                last_error = exc
                continue
            except ValidationError:
                # saída fora do schema: o chamador decide (Router tenta de novo, agentes falham)
                return LLMTurn()
            except openai.OpenAIError as exc:
                raise ProviderUnavailable(describe_provider_error(exc)) from exc
            message = completion.choices[0].message
            calls = [
                ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments)
                for tc in (message.tool_calls or [])
                if tc.type == "function"
            ]
            usage = completion.usage
            if usage:
                budget.record_usage(usage.prompt_tokens, usage.completion_tokens)
            return LLMTurn(
                content=message.content,
                tool_calls=calls,
                parsed=message.parsed,
                usage={
                    "prompt_tokens": usage.prompt_tokens if usage else 0,
                    "completion_tokens": usage.completion_tokens if usage else 0,
                },
            )
        raise ProviderUnavailable(describe_provider_error(last_error)) from last_error


async def _execute_tool(
    call: ToolCall, by_name: dict[str, ToolSpec], ctx: RequestContext
) -> ToolResult:
    spec = by_name.get(call.name)
    if spec is None:
        return ToolResult(status="error", error=f"unknown tool: {call.name}")
    try:
        args = spec.args_model.model_validate_json(call.arguments)
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        return ToolResult(status="error", error=f"invalid arguments: {', '.join(fields)}")
    try:
        ctx.budget.charge_tool()
        if spec.uses_web:
            ctx.budget.charge_web()
    except BudgetExceeded as exc:
        return ToolResult(status="error", error=str(exc))
    result = await spec.run(args, ctx)
    for evidence in result.evidence:
        ctx.evidence[evidence.id] = evidence
    return result


async def run_agent(
    llm: LLMClient,
    ctx: RequestContext,
    *,
    system: str,
    user: str,
    tools: list[ToolSpec],
    output_model: type[T],
    max_rounds: int = 3,
) -> tuple[T, list[ToolResult]]:
    """Loop de tool calling: o LLM escolhe ferramentas até devolver a saída estruturada final.

    Falhas de ferramenta (nome ou argumentos inválidos, orçamento de ferramentas) voltam ao LLM
    como `ToolResult(status="error")`; limites de LLM e prazo propagam como exceção.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    by_name = {t.name: t for t in tools}
    results: list[ToolResult] = []
    invalid_outputs = 0
    for _round in range(max_rounds):
        ctx.budget.check_deadline()
        turn = await llm.chat(
            messages, tools=tools or None, output_model=output_model, budget=ctx.budget
        )
        if not turn.tool_calls:
            if not isinstance(turn.parsed, output_model):
                # recusa ou saída fora do schema: uma nova tentativa antes de desistir
                invalid_outputs += 1
                if invalid_outputs > 1:
                    raise ProviderUnavailable("model returned no structured output")
                continue
            return turn.parsed, results
        messages.append(
            {
                "role": "assistant",
                "content": turn.content,
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": c.arguments},
                    }
                    for c in turn.tool_calls
                ],
            }
        )
        for call in turn.tool_calls:
            started = time.perf_counter()
            result = await _execute_tool(call, by_name, ctx)
            results.append(result)
            ctx.tool_results.append(result)
            if call.name in by_name:
                ctx.tools_used.append(call.name)
            log_event(
                "tool_call",
                request_id=ctx.request_id,
                tool_name=call.name,
                status=result.status,
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result.model_dump_json()}
            )
    # rodadas esgotadas: uma última chamada SEM ferramentas força a resposta final, em vez de
    # perder o que as ferramentas já trouxeram
    ctx.budget.check_deadline()
    turn = await llm.chat(messages, tools=None, output_model=output_model, budget=ctx.budget)
    if turn.tool_calls or not isinstance(turn.parsed, output_model):
        raise BudgetExceeded("agent exceeded the maximum number of tool rounds")
    return turn.parsed, results
