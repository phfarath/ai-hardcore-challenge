"""Contratos tipados trocados entre API, agentes e ferramentas (spec §4.1)."""

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Workflow = Literal["knowledge", "support", "support_then_knowledge", "combined", "clarify"]
Status = Literal[
    "answered",
    "partial",
    "needs_clarification",
    "insufficient_evidence",
    "handoff_recommended",
    "customer_not_found",
]
EvidenceType = Literal["kb_chunk", "web_result", "customer_record"]
DiagnosticGoal = Literal["connectivity", "transaction_decline", "general"]


class ChatRequest(BaseModel):
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    user_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class Task(BaseModel):
    agent: Literal["support", "knowledge"]
    question: str
    # default_factory (não `default`): o schema enviado ao LLM não pode ter a palavra-chave
    # `default`, que o modo estrito de structured outputs pode recusar
    source_scope: Literal["getnet", "web", "auto"] = Field(default_factory=lambda: "auto")
    # o que o cliente quer diagnosticar: escolhe quais fatos do terminal seguem ao Knowledge
    diagnostic_goal: DiagnosticGoal = Field(default_factory=lambda: "general")


class RoutePlan(BaseModel):
    workflow: Workflow
    tasks: list[Task] = Field(default_factory=list)
    clarification: str | None = None

    @field_validator("tasks")
    @classmethod
    def _at_most_two_tasks(cls, tasks: list[Task]) -> list[Task]:
        # validador (não `max_length`): evita `maxItems` no schema enviado ao LLM
        if len(tasks) > 2:
            raise ValueError("a plan has at most 2 tasks")
        return tasks


class Evidence(BaseModel):
    id: str
    type: EvidenceType
    title: str | None = None
    url: str | None = None


class ToolResult(BaseModel):
    status: Literal["ok", "empty", "not_found", "needs_clarification", "error"]
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    reference_date: date | None = None
    error: str | None = None


class AgentOutput(BaseModel):
    """Saída estruturada pedida ao LLM por um especialista."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    evidence_ids: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)


class AgentResult(BaseModel):
    agent: str
    status: Status
    answer: str
    evidence: list[Evidence] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)


class SourceRef(BaseModel):
    id: str
    type: EvidenceType
    title: str | None = None
    url: str | None = None


class ChatResponse(BaseModel):
    request_id: str
    status: Status
    answer: str
    agents_used: list[str]
    sources: list[SourceRef] = Field(default_factory=list)
    data_mode: Literal["demo"] | None = None
    workflow: Workflow | None = None
    tools_used: list[str] = Field(default_factory=list)


class Claim(BaseModel):
    """Uma parte da pergunta e a afirmação que a responde, com o trecho literal que a sustenta."""

    model_config = ConfigDict(extra="forbid")

    obligation: str
    status: Literal["covered", "not_found"]
    statement: str
    evidence_id: str | None
    quote: str | None


class KnowledgeOutput(BaseModel):
    """Saída estruturada do Knowledge: o status final é calculado pelo servidor, não por ele."""

    model_config = ConfigDict(extra="forbid")

    answer: str
    claims: list[Claim]
    pending: list[str]


class ClaimVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: int
    supported: bool
    reason: str


class VerificationOutput(BaseModel):
    """Saída do verificador semântico: suporte por afirmação e cobertura da pergunta."""

    model_config = ConfigDict(extra="forbid")

    verdicts: list[ClaimVerdict]
    answers_question: bool
    uncovered: list[str]
    answer_consistent: bool
