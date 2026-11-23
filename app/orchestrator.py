"""Orquestrador: executa o fluxo escolhido pelo Router, isola falhas por ramo e compõe a resposta.

Agentes não se chamam entre si: o código passa objetos tipados de um para o outro, e só o
contexto técnico mínimo (modelo, código de erro, tipo de problema) vai do Support ao Knowledge.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.agents.knowledge import run_knowledge
from app.agents.router import route
from app.agents.support import run_support
from app.context import (
    Budget,
    BudgetExceeded,
    DeadlineExceeded,
    IndexUnavailable,
    ProviderUnavailable,
    RequestContext,
    VerificationUnavailable,
)
from app.deps import Deps
from app.llm import ToolSpec
from app.observability import log_event
from app.policies import AuthorizedChat, InsufficientScope
from app.schemas import (
    AgentResult,
    ChatResponse,
    DiagnosticGoal,
    Evidence,
    RoutePlan,
    SourceRef,
    Status,
    Task,
    ToolResult,
)
from app.tools.customer import build_customer_tools
from app.tools.knowledge import build_knowledge_tools

_STEP_FAILURES = (
    BudgetExceeded,
    DeadlineExceeded,
    ProviderUnavailable,
    IndexUnavailable,
    VerificationUnavailable,
)
_REASONS: dict[type[Exception], str] = {
    BudgetExceeded: "o limite de processamento da requisição foi atingido",
    DeadlineExceeded: "o tempo limite da requisição foi atingido",
    ProviderUnavailable: "o provedor de IA está indisponível",
    IndexUnavailable: "a base de conhecimento está indisponível",
    VerificationUnavailable: "a verificação das fontes não pôde ser concluída",
}
_LABELS = {"customer_support": "Suporte ao cliente", "knowledge": "Conhecimento"}
_HANDOFF_OPENED = (
    "Como não encontrei orientação oficial para este problema, abri o caso interno {id} para a "
    "equipe de atendimento. A resposta do atendente aparecerá nesta conversa."
)
_HANDOFF_NOTE = (
    "Como não encontrei orientação oficial para este problema, recomendo procurar o atendimento "
    "da Getnet. Isto é apenas uma recomendação: nenhum atendimento foi aberto."
)
_STATUS_PRIORITY: list[Status] = [
    "customer_not_found",
    "needs_clarification",
    "insufficient_evidence",
    "handoff_recommended",
]


def _deadline_hit(ctx: RequestContext) -> bool:
    """Distingue o estouro do nosso prazo de um TimeoutError qualquer vindo de dentro."""
    return ctx.budget.remaining_seconds() <= 0.01


def _loggable_ids(evidence: list[Evidence]) -> list[str]:
    # ids de recebíveis/diagnósticos embutem o número do cliente: não vão para os logs
    return [e.id for e in evidence if e.type != "customer_record"]


def extract_public_context(result: ToolResult, goal: DiagnosticGoal = "general") -> dict[str, str]:
    """Do diagnóstico do terminal, só o que ajuda numa busca pública: nada de ids, valores ou
    dados do cliente. Conexão e recusa são observações separadas; o objetivo da pergunta escolhe
    qual delas é o problema, sem atribuir uma causa comum só porque aparecem juntas."""
    terminal = result.data.get("terminal")
    if result.status != "ok" or not terminal:
        return {}
    last_tx = terminal.get("last_transaction") or {}
    offline = terminal["connection_state"] == "offline"
    connectivity = offline or bool(terminal.get("last_error_code"))
    declined = last_tx.get("status") == "declined"
    observed: dict[str, tuple[str, str | None]] = {}
    if connectivity:
        observed["connectivity"] = ("connectivity", terminal.get("last_error_code"))
    if declined:
        observed["transaction_decline"] = ("transaction_declined", last_tx.get("decline_code"))
    if goal == "general":
        primary = next(iter(observed), None)  # conexão primeiro: sem ela nada transaciona
    else:
        primary = goal if goal in observed else None
    issue, code = observed[primary] if primary else ("none", None)
    context = {"terminal_model": terminal["model"], "issue": issue}
    if code:
        context["error_code"] = code
    other = "transaction_decline" if primary == "connectivity" else None
    if goal == "general" and other in observed and observed[other][1]:
        context["decline_code"] = observed[other][1] or ""
    return context


def merge_status(results: list[AgentResult]) -> Status:
    statuses = [r.status for r in results]
    if len(set(statuses)) == 1:
        return statuses[0]
    if "answered" in statuses:
        return "partial"
    return next((s for s in _STATUS_PRIORITY if s in statuses), "partial")


class _Run:
    """Estado mutável de uma requisição: o que já foi obtido sobrevive a falhas posteriores."""

    def __init__(self, ctx: RequestContext, deps: Deps) -> None:
        self.ctx = ctx
        self.deps = deps
        self.results: list[AgentResult] = []
        self.failures: list[tuple[str, Exception]] = []
        self.customer_tools: list[ToolSpec] = build_customer_tools(deps.repo)
        self.knowledge_tools: list[ToolSpec] = build_knowledge_tools(deps.index, deps.search)
        self.diagnostic_goal: DiagnosticGoal = "general"

    @property
    def stop(self) -> bool:
        return any(isinstance(e, BudgetExceeded | DeadlineExceeded) for _, e in self.failures)

    async def step(self, label: str, work: Callable[[], Awaitable[AgentResult]]) -> None:
        started = time.perf_counter()
        timeout = max(self.ctx.budget.remaining_seconds(), 0.001)
        try:
            result = await asyncio.wait_for(work(), timeout=timeout)
        except TimeoutError:
            if not _deadline_hit(self.ctx):
                raise
            self._fail(label, DeadlineExceeded("request deadline reached"))
        except _STEP_FAILURES as exc:
            self._fail(label, exc)
        else:
            self.results.append(result)
            log_event(
                "agent_completed",
                request_id=self.ctx.request_id,
                agent=result.agent,
                status=result.status,
                source_ids=_loggable_ids(result.evidence),
                duration_ms=round((time.perf_counter() - started) * 1000),
            )

    def _fail(self, label: str, exc: Exception) -> None:
        self.failures.append((label, exc))
        log_event(
            "agent_failed",
            request_id=self.ctx.request_id,
            agent=label,
            error_code=type(exc).__name__,
        )

    async def support(self, task: Task) -> None:
        self.diagnostic_goal = task.diagnostic_goal
        # o LLM só vê as ferramentas que o principal pode usar; sem nenhuma, é 403, não resposta
        tools = [t for t in self.customer_tools if t.required_scope in self.ctx.scopes]
        if not tools:
            raise InsufficientScope("no customer data scope")
        await self.step(
            "suporte",
            lambda: run_support(self.deps.llm, self.ctx, tools, task),
        )

    async def knowledge(
        self,
        task: Task,
        technical: dict[str, str] | None = None,
        original_message: str | None = None,
    ) -> None:
        await self.step(
            "conhecimento",
            lambda: run_knowledge(
                self.deps.llm, self.ctx, self.knowledge_tools, task, technical, original_message
            ),
        )

    def diagnostics_context(self) -> dict[str, str] | None:
        for result in reversed(self.ctx.tool_results):
            technical = extract_public_context(result, self.diagnostic_goal)
            if technical:
                return technical
        return None


async def _run_workflow(plan: RoutePlan, run: _Run) -> None:
    tasks = {t.agent: t for t in plan.tasks}
    if plan.workflow == "knowledge":
        await run.knowledge(tasks["knowledge"], original_message=run.ctx.message)
    elif plan.workflow == "support":
        await run.support(tasks["support"])
    elif plan.workflow == "support_then_knowledge":
        await run.support(tasks["support"])
        asked = bool(run.results) and run.results[-1].status == "needs_clarification"
        if not run.stop and not asked:  # sem terminal definido, primeiro o cliente esclarece
            await run.knowledge(tasks["knowledge"], run.diagnostics_context())
    elif plan.workflow == "combined":
        for task in plan.tasks:
            if run.stop:
                break
            await (run.support(task) if task.agent == "support" else run.knowledge(task))


def _compose(results: list[AgentResult], failures: list[tuple[str, Exception]]) -> str:
    if len(results) == 1:
        body = results[0].answer
    else:
        body = "\n\n".join(f"**{_LABELS.get(r.agent, r.agent)}**\n{r.answer}" for r in results)
    notes = [
        f"Pendência: a parte de {label} não pôde ser concluída porque "
        f"{_REASONS.get(type(exc), 'ocorreu um erro')}."
        for label, exc in failures
    ]
    pending = [p for r in results for p in r.pending]
    if pending:
        notes.insert(0, "Pendências: " + "; ".join(pending) + ".")
    return "\n\n".join([body, *notes])


def _sources(results: list[AgentResult]) -> list[SourceRef]:
    unique: dict[str, Evidence] = {}
    for result in results:
        for evidence in result.evidence:
            unique.setdefault(evidence.id, evidence)
    return [SourceRef(**e.model_dump()) for e in unique.values()]


@dataclass
class ChatOutcome:
    """Resposta e o que o turno durável precisa guardar: plano, opções apresentadas e consumo."""

    response: ChatResponse
    plan: RoutePlan
    terminal_options: list[str]
    llm_calls: int
    tool_calls: int
    # contexto do caso humano, só quando o fluxo pediu escalonamento (sem dados do cliente)
    handoff: dict[str, Any] | None = None


async def handle_chat(
    req: AuthorizedChat, deps: Deps, *, request_id: str | None = None
) -> ChatResponse:
    return (await run_chat(req, deps, request_id=request_id)).response


async def run_chat(
    req: AuthorizedChat,
    deps: Deps,
    *,
    request_id: str | None = None,
    history: Sequence[tuple[str, str]] = (),
    plan: RoutePlan | None = None,
    selected_terminal: str | None = None,
    handoff_id: str | None = None,
) -> ChatOutcome:
    """Recebe um pedido já autorizado (`policies.authorize_chat`): a identidade efetiva vem do
    principal do token, nunca do payload ou do texto. `plan` e `selected_terminal` retomam um
    esclarecimento resolvido pelo servidor: o Router não roda de novo. `handoff_id` é o id
    reservado para o caso humano, se o turno durável puder abri-lo."""
    started = time.perf_counter()
    s = deps.settings
    ctx = RequestContext(
        request_id=request_id or f"req_{uuid.uuid4().hex[:12]}",
        user_id=req.customer_id,
        message=req.message,
        scopes=req.principal.scopes,
        selected_terminal_id=selected_terminal,
        clock=deps.clock,
        budget=Budget(
            max_llm_calls=s.max_llm_calls,
            max_tool_calls=s.max_tool_calls,
            max_web_searches=s.max_web_searches,
            deadline_seconds=s.deadline_seconds,
        ),
    )
    routed = plan is None
    if plan is None:
        try:
            plan = await asyncio.wait_for(
                route(deps.llm, ctx, req.message, history),
                timeout=max(ctx.budget.remaining_seconds(), 0.001),
            )
        except TimeoutError as exc:
            if not _deadline_hit(ctx):
                raise
            raise DeadlineExceeded("request deadline reached") from exc
        log_event("route", request_id=ctx.request_id, workflow=plan.workflow)

    if plan.workflow == "clarify":
        response = _finish(
            ctx, deps, plan, started, "needs_clarification", plan.clarification or "", []
        )
        return _outcome(ctx, plan, response, routed)
    run = _Run(ctx, deps)
    await _run_workflow(plan, run)
    if not run.results:
        log_event(
            "request_failed",
            request_id=ctx.request_id,
            workflow=plan.workflow,
            error_code=type(run.failures[0][1]).__name__,
        )
        raise run.failures[0][1]

    status: Status = "partial" if run.failures else merge_status(run.results)
    answer = _compose(run.results, run.failures)
    handoff: dict[str, Any] | None = None
    if _needs_handoff(plan, run):
        note = _HANDOFF_OPENED.format(id=handoff_id) if handoff_id else _HANDOFF_NOTE
        status, answer = "handoff_recommended", f"{answer}\n\n{note}"
        handoff = {
            "reason": "diagnosed_issue_without_official_guidance",
            "question": req.message,
            "technical_context": run.diagnostics_context() or {},
            "gap": run.results[1].answer,
            "source_ids": _loggable_ids([e for r in run.results for e in r.evidence]),
        }
    response = _finish(ctx, deps, plan, started, status, answer, run.results)
    outcome = _outcome(ctx, plan, response, routed)
    outcome.handoff = handoff
    return outcome


def _outcome(
    ctx: RequestContext, plan: RoutePlan, response: ChatResponse, routed: bool
) -> ChatOutcome:
    if not routed:
        response.agents_used = [a for a in response.agents_used if a != "router"]
    options = next(
        (
            list(r.data["options"])
            for r in reversed(ctx.tool_results)
            if r.status == "needs_clarification" and r.data.get("options")
        ),
        [],
    )
    return ChatOutcome(
        response=response,
        plan=plan,
        terminal_options=options,
        llm_calls=ctx.budget.llm_calls,
        tool_calls=ctx.budget.tool_calls,
    )


def _needs_handoff(plan: RoutePlan, run: _Run) -> bool:
    """Problema técnico diagnosticado e nenhuma orientação oficial encontrada: o sistema não
    resolve sozinho e escala para atendimento humano (o turno durável abre o caso)."""
    if plan.workflow != "support_then_knowledge" or run.failures or len(run.results) != 2:
        return False
    issue = (run.diagnostics_context() or {}).get("issue", "none")
    return run.results[1].status == "insufficient_evidence" and issue != "none"


def _finish(
    ctx: RequestContext,
    deps: Deps,
    plan: RoutePlan,
    started: float,
    status: Status,
    answer: str,
    results: list[AgentResult],
) -> ChatResponse:
    sources = _sources(results)
    response = ChatResponse(
        request_id=ctx.request_id,
        status=status,
        answer=answer,
        agents_used=["router", *dict.fromkeys(r.agent for r in results)],
        sources=sources,
        data_mode="demo" if any(s.type == "customer_record" for s in sources) else None,
        workflow=plan.workflow,
        tools_used=list(ctx.tools_used),
    )
    log_event(
        "request_completed",
        request_id=ctx.request_id,
        workflow=plan.workflow,
        status=status,
        agents=response.agents_used,
        source_ids=_loggable_ids([Evidence(**s.model_dump()) for s in sources]),
        index_version=deps.index.version,
        llm_calls=ctx.budget.llm_calls,
        tool_calls=ctx.budget.tool_calls,
        token_usage={
            "prompt": ctx.budget.prompt_tokens,
            "completion": ctx.budget.completion_tokens,
        },
        duration_ms=round((time.perf_counter() - started) * 1000),
    )
    return response
