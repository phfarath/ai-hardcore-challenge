"""Estado durável: conversas, mensagens e execuções em SQLite (T04).

Transações curtas: nenhuma fica aberta durante chamada ao LLM. O turno grava a intenção
(execução `running` com lease), libera o banco, executa e registra o resultado conferindo a
versão da conversa. A lease impede dois turnos simultâneos na mesma conversa e, se o processo
cair, expira e deixa a mesma chave de idempotência reexecutar.
"""

import json
import sqlite3
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel

RunState = Literal["running", "completed", "failed"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_sub TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    pending TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    run_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (conversation_id, seq)
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    owner_sub TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('running', 'completed', 'failed')),
    request_hash TEXT NOT NULL,
    idempotency_key TEXT,
    response TEXT,
    attempts INTEGER NOT NULL DEFAULT 1,
    llm_calls INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS runs_idempotency
    ON runs (tenant_id, owner_sub, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS runs_by_conversation ON runs (conversation_id, state);
CREATE TABLE IF NOT EXISTS handoffs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    run_id TEXT NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    state TEXT NOT NULL
        CHECK (state IN ('pending', 'assigned', 'resolved', 'completed', 'cancelled')),
    assignee TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    decision TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS handoffs_queue ON handoffs (tenant_id, state, created_at);
CREATE TABLE IF NOT EXISTS resume_events (
    id TEXT PRIMARY KEY,
    handoff_id TEXT NOT NULL UNIQUE REFERENCES handoffs(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN ('pending', 'processing', 'done')),
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    processed_at REAL
);
CREATE TABLE IF NOT EXISTS audit_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    outcome TEXT NOT NULL,
    correlation TEXT,
    at REAL NOT NULL
);
"""


class ConversationBusy(Exception):
    """Outro turno da mesma conversa está em andamento (409)."""


class ConversationConflict(Exception):
    """A conversa mudou desde o início do turno; o resultado não é gravado por cima (409)."""


class Conversation(BaseModel):
    id: str
    tenant_id: str
    owner_sub: str
    customer_id: str
    version: int
    pending: dict[str, Any] | None
    expires_at: float


class Message(BaseModel):
    seq: int
    role: Literal["user", "assistant"]
    content: str
    run_id: str
    created_at: float


class RunRecord(BaseModel):
    id: str
    conversation_id: str
    state: RunState
    request_hash: str
    response: dict[str, Any] | None
    lease_until: float
    attempts: int


class TurnStart(BaseModel):
    """Quem abre o turno e em que condições; os dados de escopo vêm do principal autorizado."""

    tenant_id: str
    owner_sub: str
    customer_id: str
    request_hash: str
    idempotency_key: str | None
    now: float
    lease_until: float
    expires_at: float


HandoffState = Literal["pending", "assigned", "resolved", "completed", "cancelled"]


class HandoffConflict(Exception):
    """O caso não está no estado/versão esperados (já assumido, já decidido, versão antiga)."""


class HandoffExpired(HandoffConflict):
    """O prazo do caso venceu; a decisão não é aceita."""


class HandoffDraft(BaseModel):
    """Caso a abrir junto com o resultado do turno (mesma transação)."""

    id: str
    reason: str
    snapshot: dict[str, Any]
    expires_at: float


class Handoff(BaseModel):
    id: str
    conversation_id: str
    run_id: str
    tenant_id: str
    customer_id: str
    owner_sub: str
    reason: str
    snapshot: dict[str, Any]
    state: HandoffState
    assignee: str | None
    version: int
    decision: dict[str, Any] | None
    created_at: float
    expires_at: float


class ResumeEvent(BaseModel):
    id: str
    handoff_id: str
    attempts: int


class AuditEvent(BaseModel):
    actor: str
    action: str
    resource: str
    outcome: str
    correlation: str | None
    at: float


class TurnResult(BaseModel):
    user_message: str
    assistant_message: str
    response: dict[str, Any]
    pending: dict[str, Any] | None
    llm_calls: int
    tool_calls: int
    now: float
    expires_at: float
    handoff: HandoffDraft | None = None


class StateStore(Protocol):
    def get_conversation(self, conversation_id: str) -> Conversation | None: ...

    def messages(self, conversation_id: str, limit: int = 50) -> list[Message]: ...

    def find_run(self, tenant_id: str, owner_sub: str, key: str) -> RunRecord | None: ...

    def start_turn(
        self, conversation_id: str | None, start: TurnStart
    ) -> tuple[Conversation, RunRecord]: ...

    def restart_run(self, run_id: str, now: float, lease_until: float) -> RunRecord: ...

    def complete_turn(
        self, run_id: str, conversation_id: str, expected_version: int, result: TurnResult
    ) -> None: ...

    def fail_run(self, run_id: str, now: float) -> None: ...

    def purge_expired(self, now: float) -> int: ...

    def ping(self) -> bool: ...

    def get_handoff(self, handoff_id: str) -> Handoff | None: ...

    def list_handoffs(self, tenant_id: str, state: HandoffState | None = None) -> list[Handoff]: ...

    def claim_handoff(
        self, handoff_id: str, operator: str, expected_version: int, now: float
    ) -> Handoff: ...

    def resolve_handoff(
        self,
        handoff_id: str,
        operator: str,
        expected_version: int,
        decision: dict[str, Any],
        now: float,
    ) -> Handoff: ...

    def claim_resume_event(self, now: float, lease_until: float) -> ResumeEvent | None: ...

    def complete_resume(
        self, event: ResumeEvent, message: str | None, outcome: str, now: float
    ) -> bool: ...

    def audit_trail(self, resource: str) -> list[AuditEvent]: ...


class SqliteStateStore:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def ping(self) -> bool:
        """Prontidão: o arquivo abre e o schema está lá (leitura barata, sem lock de escrita)."""
        try:
            db = self._connect()
            try:
                db.execute("SELECT 1 FROM runs LIMIT 1").fetchall()
            finally:
                db.close()
        except sqlite3.Error:
            return False
        return True

    @contextmanager
    def _write(self) -> Generator[sqlite3.Connection]:
        """Transação de escrita imediata: serializa escritores sem segurar o banco além dela."""
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    @staticmethod
    def _conversation(row: sqlite3.Row) -> Conversation:
        return Conversation(
            id=row["id"],
            tenant_id=row["tenant_id"],
            owner_sub=row["owner_sub"],
            customer_id=row["customer_id"],
            version=row["version"],
            pending=json.loads(row["pending"]) if row["pending"] else None,
            expires_at=row["expires_at"],
        )

    @staticmethod
    def _run(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            conversation_id=row["conversation_id"],
            state=row["state"],
            request_hash=row["request_hash"],
            response=json.loads(row["response"]) if row["response"] else None,
            lease_until=row["lease_until"],
            attempts=row["attempts"],
        )

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        db = self._connect()
        try:
            row = db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
            found = row.fetchone()
        finally:
            db.close()
        return self._conversation(found) if found else None

    def messages(self, conversation_id: str, limit: int = 50) -> list[Message]:
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT * FROM (SELECT seq, role, content, run_id, created_at FROM messages "
                "WHERE conversation_id = ? ORDER BY seq DESC LIMIT ?) ORDER BY seq",
                (conversation_id, limit),
            ).fetchall()
        finally:
            db.close()
        return [Message(**dict(r)) for r in rows]

    def find_run(self, tenant_id: str, owner_sub: str, key: str) -> RunRecord | None:
        db = self._connect()
        try:
            row = db.execute(
                "SELECT * FROM runs WHERE tenant_id = ? AND owner_sub = ? AND idempotency_key = ?",
                (tenant_id, owner_sub, key),
            ).fetchone()
        finally:
            db.close()
        return self._run(row) if row else None

    def start_turn(
        self, conversation_id: str | None, start: TurnStart
    ) -> tuple[Conversation, RunRecord]:
        """Cria a conversa (se nova) e a execução `running` na mesma transação."""
        with self._write() as db:
            if conversation_id is None:
                conversation_id = f"conv_{uuid.uuid4().hex}"
                db.execute(
                    "INSERT INTO conversations (id, tenant_id, owner_sub, customer_id, created_at, "
                    "updated_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        conversation_id,
                        start.tenant_id,
                        start.owner_sub,
                        start.customer_id,
                        start.now,
                        start.now,
                        start.expires_at,
                    ),
                )
            busy = db.execute(
                "SELECT 1 FROM runs WHERE conversation_id = ? AND state = 'running' "
                "AND lease_until > ?",
                (conversation_id, start.now),
            ).fetchone()
            if busy:
                raise ConversationBusy(conversation_id)
            run_id = f"run_{uuid.uuid4().hex}"
            db.execute(
                "INSERT INTO runs (id, conversation_id, tenant_id, owner_sub, state, request_hash, "
                "idempotency_key, lease_until, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)",
                (
                    run_id,
                    conversation_id,
                    start.tenant_id,
                    start.owner_sub,
                    start.request_hash,
                    start.idempotency_key,
                    start.lease_until,
                    start.now,
                    start.now,
                ),
            )
            conversation = self._conversation(
                db.execute(
                    "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
                ).fetchone()
            )
            run = self._run(db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())
        return conversation, run

    def restart_run(self, run_id: str, now: float, lease_until: float) -> RunRecord:
        """Reexecuta uma execução falha ou abandonada (lease vencida) com a mesma chave."""
        with self._write() as db:
            row = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            run = self._run(row)
            abandoned = run.state == "running" and run.lease_until <= now
            if run.state != "failed" and not abandoned:
                raise ConversationBusy(run.conversation_id)
            busy = db.execute(
                "SELECT 1 FROM runs WHERE conversation_id = ? AND state = 'running' "
                "AND lease_until > ? AND id != ?",
                (run.conversation_id, now, run_id),
            ).fetchone()
            if busy:
                raise ConversationBusy(run.conversation_id)
            db.execute(
                "UPDATE runs SET state = 'running', attempts = attempts + 1, lease_until = ?, "
                "updated_at = ? WHERE id = ?",
                (lease_until, now, run_id),
            )
            updated = db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._run(updated)

    def complete_turn(
        self, run_id: str, conversation_id: str, expected_version: int, result: TurnResult
    ) -> None:
        """Mensagens, pendência, versão da conversa e resultado da execução numa transação. Se
        a versão mudou (outro turno gravou depois de uma lease vencida), nada é gravado."""
        with self._write() as db:
            updated = db.execute(
                "UPDATE conversations SET version = version + 1, pending = ?, updated_at = ?, "
                "expires_at = ? WHERE id = ? AND version = ?",
                (
                    json.dumps(result.pending) if result.pending else None,
                    result.now,
                    result.expires_at,
                    conversation_id,
                    expected_version,
                ),
            ).rowcount
            if updated != 1:
                raise ConversationConflict(conversation_id)
            (seq,) = db.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            for offset, (role, content) in enumerate(
                [("user", result.user_message), ("assistant", result.assistant_message)], 1
            ):
                db.execute(
                    "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?)",
                    (conversation_id, seq + offset, role, content, run_id, result.now),
                )
            db.execute(
                "UPDATE runs SET state = 'completed', response = ?, llm_calls = ?, "
                "tool_calls = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps(result.response, ensure_ascii=False),
                    result.llm_calls,
                    result.tool_calls,
                    result.now,
                    run_id,
                ),
            )
            if result.handoff is not None:
                self._insert_handoff(db, result.handoff, conversation_id, run_id, result.now)

    def _insert_handoff(
        self,
        db: sqlite3.Connection,
        draft: HandoffDraft,
        conversation_id: str,
        run_id: str,
        now: float,
    ) -> None:
        tenant_id, customer_id = db.execute(
            "SELECT tenant_id, customer_id FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        db.execute(
            "INSERT INTO handoffs (id, conversation_id, run_id, tenant_id, customer_id, reason, "
            "snapshot, state, created_at, updated_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                draft.id,
                conversation_id,
                run_id,
                tenant_id,
                customer_id,
                draft.reason,
                json.dumps(draft.snapshot, ensure_ascii=False),
                now,
                now,
                draft.expires_at,
            ),
        )
        self._audit(db, tenant_id, "system", "handoff.created", draft.id, "pending", run_id, now)

    @staticmethod
    def _audit(
        db: sqlite3.Connection,
        tenant_id: str,
        actor: str,
        action: str,
        resource: str,
        outcome: str,
        correlation: str | None,
        now: float,
    ) -> None:
        db.execute(
            "INSERT INTO audit_events (tenant_id, actor, action, resource, outcome, correlation, "
            "at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, actor, action, resource, outcome, correlation, now),
        )

    _HANDOFF_SELECT = (
        "SELECT h.*, c.owner_sub FROM handoffs h JOIN conversations c ON c.id = h.conversation_id"
    )

    @staticmethod
    def _handoff(row: sqlite3.Row) -> Handoff:
        return Handoff(
            id=row["id"],
            conversation_id=row["conversation_id"],
            run_id=row["run_id"],
            tenant_id=row["tenant_id"],
            customer_id=row["customer_id"],
            owner_sub=row["owner_sub"],
            reason=row["reason"],
            snapshot=json.loads(row["snapshot"]),
            state=row["state"],
            assignee=row["assignee"],
            version=row["version"],
            decision=json.loads(row["decision"]) if row["decision"] else None,
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )

    def _locked_handoff(
        self, db: sqlite3.Connection, handoff_id: str, expected_version: int, now: float
    ) -> Handoff:
        row = db.execute(f"{self._HANDOFF_SELECT} WHERE h.id = ?", (handoff_id,)).fetchone()
        if row is None:
            raise HandoffConflict("handoff not found")
        handoff = self._handoff(row)
        if handoff.version != expected_version:
            raise HandoffConflict("stale version")
        if handoff.expires_at <= now:
            raise HandoffExpired(handoff_id)
        return handoff

    def get_handoff(self, handoff_id: str) -> Handoff | None:
        db = self._connect()
        try:
            row = db.execute(f"{self._HANDOFF_SELECT} WHERE h.id = ?", (handoff_id,)).fetchone()
        finally:
            db.close()
        return self._handoff(row) if row else None

    def list_handoffs(self, tenant_id: str, state: HandoffState | None = None) -> list[Handoff]:
        query = f"{self._HANDOFF_SELECT} WHERE h.tenant_id = ?"
        params: list[Any] = [tenant_id]
        if state is not None:
            query += " AND h.state = ?"
            params.append(state)
        db = self._connect()
        try:
            rows = db.execute(query + " ORDER BY h.created_at", params).fetchall()
        finally:
            db.close()
        return [self._handoff(r) for r in rows]

    def claim_handoff(
        self, handoff_id: str, operator: str, expected_version: int, now: float
    ) -> Handoff:
        """Assume um caso pendente. Dois operadores com a mesma versão: só o primeiro vence."""
        with self._write() as db:
            handoff = self._locked_handoff(db, handoff_id, expected_version, now)
            if handoff.state != "pending":
                raise HandoffConflict(f"handoff is {handoff.state}")
            db.execute(
                "UPDATE handoffs SET state = 'assigned', assignee = ?, version = version + 1, "
                "updated_at = ? WHERE id = ?",
                (operator, now, handoff_id),
            )
            self._audit(
                db,
                handoff.tenant_id,
                operator,
                "handoff.claimed",
                handoff_id,
                "assigned",
                handoff.run_id,
                now,
            )
            row = db.execute(f"{self._HANDOFF_SELECT} WHERE h.id = ?", (handoff_id,)).fetchone()
        return self._handoff(row)

    def resolve_handoff(
        self,
        handoff_id: str,
        operator: str,
        expected_version: int,
        decision: dict[str, Any],
        now: float,
    ) -> Handoff:
        """Decisão e evento de retomada na mesma transação: ou os dois existem, ou nenhum."""
        with self._write() as db:
            handoff = self._locked_handoff(db, handoff_id, expected_version, now)
            if handoff.state != "assigned" or handoff.assignee != operator:
                raise HandoffConflict("handoff is not assigned to this operator")
            db.execute(
                "UPDATE handoffs SET state = 'resolved', decision = ?, version = version + 1, "
                "updated_at = ? WHERE id = ?",
                (json.dumps(decision, ensure_ascii=False), now, handoff_id),
            )
            db.execute(
                "INSERT INTO resume_events (id, handoff_id, state, created_at) "
                "VALUES (?, ?, 'pending', ?)",
                (f"evt_{uuid.uuid4().hex}", handoff_id, now),
            )
            self._audit(
                db,
                handoff.tenant_id,
                operator,
                "handoff.resolved",
                handoff_id,
                str(decision.get("decision_type")),
                handoff.run_id,
                now,
            )
            row = db.execute(f"{self._HANDOFF_SELECT} WHERE h.id = ?", (handoff_id,)).fetchone()
        return self._handoff(row)

    def claim_resume_event(self, now: float, lease_until: float) -> ResumeEvent | None:
        """Próximo evento pendente, ou em processamento com lease vencida (worker caiu)."""
        with self._write() as db:
            row = db.execute(
                "SELECT id, handoff_id, attempts FROM resume_events WHERE state = 'pending' "
                "OR (state = 'processing' AND lease_until <= ?) ORDER BY created_at LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE resume_events SET state = 'processing', attempts = attempts + 1, "
                "lease_until = ? WHERE id = ?",
                (lease_until, row["id"]),
            )
        return ResumeEvent(id=row["id"], handoff_id=row["handoff_id"], attempts=row["attempts"] + 1)

    def complete_resume(
        self, event: ResumeEvent, message: str | None, outcome: str, now: float
    ) -> bool:
        """Entrega a resposta na conversa uma única vez: mensagem, estado do caso e evento numa
        transação; um evento já concluído não grava nada de novo."""
        with self._write() as db:
            done = db.execute(
                "SELECT state FROM resume_events WHERE id = ?", (event.id,)
            ).fetchone()
            if done is None or done["state"] == "done":
                return False
            row = db.execute(
                f"{self._HANDOFF_SELECT} WHERE h.id = ?", (event.handoff_id,)
            ).fetchone()
            handoff = self._handoff(row)
            if message is not None:
                (seq,) = db.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM messages WHERE conversation_id = ?",
                    (handoff.conversation_id,),
                ).fetchone()
                db.execute(
                    "INSERT INTO messages VALUES (?, ?, 'assistant', ?, ?, ?)",
                    (handoff.conversation_id, seq + 1, message, handoff.id, now),
                )
            final_state = "completed" if message is not None else "cancelled"
            db.execute(
                "UPDATE handoffs SET state = ?, version = version + 1, updated_at = ? WHERE id = ?",
                (final_state, now, handoff.id),
            )
            db.execute(
                "UPDATE resume_events SET state = 'done', processed_at = ? WHERE id = ?",
                (now, event.id),
            )
            self._audit(
                db,
                handoff.tenant_id,
                "worker",
                "handoff.resumed",
                handoff.id,
                outcome,
                event.id,
                now,
            )
        return True

    def audit_trail(self, resource: str) -> list[AuditEvent]:
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT actor, action, resource, outcome, correlation, at FROM audit_events "
                "WHERE resource = ? ORDER BY seq",
                (resource,),
            ).fetchall()
        finally:
            db.close()
        return [AuditEvent(**dict(r)) for r in rows]

    def fail_run(self, run_id: str, now: float) -> None:
        with self._write() as db:
            db.execute(
                "UPDATE runs SET state = 'failed', updated_at = ? WHERE id = ? "
                "AND state = 'running'",
                (now, run_id),
            )

    def purge_expired(self, now: float) -> int:
        """Retenção: conversas vencidas saem com mensagens e execuções (ON DELETE CASCADE)."""
        with self._write() as db:
            return db.execute("DELETE FROM conversations WHERE expires_at <= ?", (now,)).rowcount
