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


class TurnResult(BaseModel):
    user_message: str
    assistant_message: str
    response: dict[str, Any]
    pending: dict[str, Any] | None
    llm_calls: int
    tool_calls: int
    now: float
    expires_at: float


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
