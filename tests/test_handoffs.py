"""T05: caso humano real — abrir, assumir, decidir, reiniciar e retomar, sem duplicar."""

from pathlib import Path
from typing import Any

import httpx
import pytest

from app.auth import JsonAccessRepository
from app.deps import Deps
from app.handoffs import ResumeWorker
from app.llm import LLMTurn
from app.main import create_app
from app.policies import RECEIVABLES_READ, TERMINALS_READ
from app.schemas import AgentOutput, RoutePlan, Task
from tests.fakes import FrozenClock, ScriptedLLM, final_turn, kout, make_deps, tool_call_turn

PROBLEM = "Minha maquininha não conecta."


def _escalating_turns() -> list[LLMTurn]:
    """Diagnóstico com problema e nenhuma orientação oficial: o fluxo escala."""
    return [
        final_turn(
            RoutePlan(
                workflow="support_then_knowledge",
                tasks=[
                    Task(agent="support", question="diagnóstico"),
                    Task(agent="knowledge", question="orientação"),
                ],
            )
        ),
        tool_call_turn("get_terminal_diagnostics"),
        final_turn(AgentOutput(answer="Sua máquina está offline.", evidence_ids=[])),
        final_turn(kout("Não sei.")),
    ]


async def _call(
    deps: Deps,
    method: str,
    url: str,
    sub: str,
    json: dict[str, Any] | None = None,
    scopes: list[str] | None = None,
) -> httpx.Response:
    assert deps.dev_issuer is not None
    headers = {"Authorization": f"Bearer {deps.dev_issuer.issue(sub, scopes=scopes)}"}
    transport = httpx.ASGITransport(app=create_app(deps))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, url, json=json, headers=headers)


async def _open_case(
    tmp_path: Path, clock: FrozenClock | None = None
) -> tuple[Deps, dict[str, Any]]:
    deps = await make_deps(tmp_path, ScriptedLLM(_escalating_turns()), clock=clock)
    response = await _call(deps, "POST", "/chat", "user-a", {"message": PROBLEM})
    body = response.json()
    assert response.status_code == 200
    return deps, body


def _worker(deps: Deps) -> ResumeWorker:
    return ResumeWorker(deps.state, deps.auth.access, deps.clock)


async def _claim_and_resolve(deps: Deps, handoff_id: str, content: str = "Troque o chip.") -> None:
    claimed = await _call(
        deps,
        "POST",
        f"/operator/handoffs/{handoff_id}/claim",
        "operator-1",
        {"expected_version": 1},
    )
    assert claimed.status_code == 200
    resolved = await _call(
        deps,
        "POST",
        f"/operator/handoffs/{handoff_id}/resolve",
        "operator-1",
        {
            "decision_type": "reply",
            "content": content,
            "justification": "orientação padrão de conectividade",
            "expected_version": claimed.json()["version"],
        },
    )
    assert resolved.status_code == 200


async def test_full_cycle_open_claim_resolve_restart_resume(tmp_path: Path) -> None:
    deps, body = await _open_case(tmp_path)
    handoff_id = body["handoff_id"]
    assert body["status"] == "handoff_recommended"
    assert body["run_status"] == "awaiting_operator"
    assert handoff_id and handoff_id in body["answer"]
    assert "nenhum atendimento foi aberto" not in body["answer"].lower()

    queue = (await _call(deps, "GET", "/operator/handoffs", "operator-1")).json()
    assert [h["id"] for h in queue] == [handoff_id]
    snapshot = queue[0]["snapshot"]
    assert snapshot["technical_context"]["issue"] == "connectivity"
    assert snapshot["question"] == PROBLEM

    await _claim_and_resolve(deps, handoff_id)

    restarted = await make_deps(tmp_path, ScriptedLLM([]))  # reinício entre decisão e retomada
    assert _worker(restarted).process_once() is True
    assert _worker(restarted).process_once() is False

    history = (
        await _call(restarted, "GET", f"/conversations/{body['conversation_id']}", "user-a")
    ).json()["messages"]
    assert len(history) == 3
    final = history[-1]["content"]
    assert "Troque o chip." in final
    assert "atendente" in final and handoff_id in final
    status = (await _call(restarted, "GET", f"/handoffs/{handoff_id}", "user-a")).json()
    assert status["state"] == "completed"
    assert status["decision_type"] == "reply"
    actions = [e.action for e in restarted.state.audit_trail(handoff_id)]
    assert actions == ["handoff.created", "handoff.claimed", "handoff.resolved", "handoff.resumed"]
    actors = [e.actor for e in restarted.state.audit_trail(handoff_id)]
    assert actors[1:3] == ["operator-1", "operator-1"]


async def test_duplicate_resolution_creates_no_second_event_or_message(tmp_path: Path) -> None:
    deps, body = await _open_case(tmp_path)
    handoff_id = body["handoff_id"]
    await _claim_and_resolve(deps, handoff_id)
    again = await _call(
        deps,
        "POST",
        f"/operator/handoffs/{handoff_id}/resolve",
        "operator-1",
        {"decision_type": "reply", "content": "x", "justification": "y", "expected_version": 2},
    )
    assert again.status_code == 409
    worker = _worker(deps)
    assert worker.process_once() is True
    assert worker.process_once() is False
    messages = deps.state.messages(body["conversation_id"])
    assert sum("Troque o chip." in m.content for m in messages) == 1


async def test_two_operators_claiming_the_same_case_only_one_wins(tmp_path: Path) -> None:
    deps, body = await _open_case(tmp_path)
    profiles = deps.dev_issuer.profiles  # type: ignore[union-attr]
    profiles["operator-2"] = profiles["operator-1"]
    try:
        url = f"/operator/handoffs/{body['handoff_id']}/claim"
        first = await _call(deps, "POST", url, "operator-1", {"expected_version": 1})
        second = await _call(deps, "POST", url, "operator-2", {"expected_version": 1})
    finally:
        del profiles["operator-2"]
    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json() == {"error": "handoff_conflict"}


async def test_operator_must_hold_the_case_to_resolve_it(tmp_path: Path) -> None:
    deps, body = await _open_case(tmp_path)
    response = await _call(
        deps,
        "POST",
        f"/operator/handoffs/{body['handoff_id']}/resolve",
        "operator-1",
        {"decision_type": "reply", "content": "x", "justification": "y", "expected_version": 1},
    )
    assert response.status_code == 409


async def test_decision_on_an_old_version_is_refused(tmp_path: Path) -> None:
    deps, body = await _open_case(tmp_path)
    handoff_id = body["handoff_id"]
    url = f"/operator/handoffs/{handoff_id}/claim"
    assert (
        await _call(deps, "POST", url, "operator-1", {"expected_version": 1})
    ).status_code == 200
    stale = await _call(
        deps,
        "POST",
        f"/operator/handoffs/{handoff_id}/resolve",
        "operator-1",
        {"decision_type": "reply", "content": "x", "justification": "y", "expected_version": 1},
    )
    assert stale.status_code == 409
    assert deps.state.claim_resume_event(9e9, 9e9) is None


async def test_expired_case_cannot_be_decided(tmp_path: Path) -> None:
    clock = FrozenClock()
    deps, body = await _open_case(tmp_path, clock)
    clock.advance(hours=deps.settings.handoff_ttl_hours + 1)
    url = f"/operator/handoffs/{body['handoff_id']}/claim"
    response = await _call(deps, "POST", url, "operator-1", {"expected_version": 1})
    assert response.status_code == 409
    assert response.json() == {"error": "handoff_expired"}


@pytest.mark.parametrize(
    ("sub", "scopes", "expected"),
    [
        ("user-a", None, 403),  # cliente não é operador
        ("operator-1", ["handoffs:read"], 403),  # operador sem escopo de decisão
    ],
)
async def test_claim_requires_operator_role_and_scope(
    tmp_path: Path, sub: str, scopes: list[str] | None, expected: int
) -> None:
    deps, body = await _open_case(tmp_path)
    url = f"/operator/handoffs/{body['handoff_id']}/claim"
    response = await _call(deps, "POST", url, sub, {"expected_version": 1}, scopes=scopes)
    assert response.status_code == expected


async def test_case_of_another_tenant_is_not_found_for_the_operator(tmp_path: Path) -> None:
    deps, body = await _open_case(tmp_path)
    issuer = deps.dev_issuer
    assert issuer is not None
    token = issuer.issue("operator-1", tenant_id="outro-tenant")
    transport = httpx.ASGITransport(app=create_app(deps))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        detail = await client.get(
            f"/operator/handoffs/{body['handoff_id']}", headers={"Authorization": f"Bearer {token}"}
        )
        queue = await client.get("/operator/handoffs", headers={"Authorization": f"Bearer {token}"})
    assert detail.status_code == 404
    assert queue.json() == []


async def test_customer_sees_only_their_own_case(tmp_path: Path) -> None:
    deps, body = await _open_case(tmp_path)
    url = f"/handoffs/{body['handoff_id']}"
    assert (await _call(deps, "GET", url, "user-a")).status_code == 200
    assert (await _call(deps, "GET", url, "user-b")).status_code == 404
    assert (await _call(deps, "GET", url, "user-a", scopes=[TERMINALS_READ])).status_code == 403


async def test_without_handoff_scope_the_case_is_only_recommended(tmp_path: Path) -> None:
    deps = await make_deps(tmp_path, ScriptedLLM(_escalating_turns()))
    body = (
        await _call(
            deps,
            "POST",
            "/chat",
            "user-a",
            {"message": PROBLEM},
            scopes=[RECEIVABLES_READ, TERMINALS_READ],
        )
    ).json()
    assert body["status"] == "handoff_recommended"
    assert body["handoff_id"] is None
    assert body["run_status"] == "completed"
    assert deps.state.list_handoffs("getnet") == []


async def test_revoked_access_before_resume_delivers_nothing(tmp_path: Path) -> None:
    deps, body = await _open_case(tmp_path)
    await _claim_and_resolve(deps, body["handoff_id"])
    revoked = JsonAccessRepository()
    revoked.profiles = {
        **revoked.profiles,
        "user-a": {**revoked.profiles["user-a"], "customers": []},
    }
    assert ResumeWorker(deps.state, revoked, deps.clock).process_once() is True
    assert len(deps.state.messages(body["conversation_id"])) == 2
    handoff = deps.state.get_handoff(body["handoff_id"])
    assert handoff is not None and handoff.state == "cancelled"
    assert deps.state.audit_trail(body["handoff_id"])[-1].outcome == "access_revoked"


async def test_worker_crash_after_claiming_event_is_retried_once(tmp_path: Path) -> None:
    """Worker caiu depois de pegar o evento: a lease vence e outro ciclo entrega uma vez só."""
    clock = FrozenClock()
    deps, body = await _open_case(tmp_path, clock)
    await _claim_and_resolve(deps, body["handoff_id"])
    now = clock.now().timestamp()
    assert deps.state.claim_resume_event(now, now + 30) is not None  # "crash" aqui
    assert _worker(deps).process_once() is False  # lease ainda vale
    clock.advance(seconds=31)
    assert _worker(deps).process_once() is True
    assert _worker(deps).process_once() is False
    messages = deps.state.messages(body["conversation_id"])
    assert sum("Troque o chip." in m.content for m in messages) == 1


async def test_idempotent_retry_of_the_escalating_turn_opens_one_case(tmp_path: Path) -> None:
    deps = await make_deps(tmp_path, ScriptedLLM(_escalating_turns()))
    assert deps.dev_issuer is not None
    headers = {
        "Authorization": f"Bearer {deps.dev_issuer.issue('user-a')}",
        "Idempotency-Key": "esc-1",
    }
    transport = httpx.ASGITransport(app=create_app(deps))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/chat", json={"message": PROBLEM}, headers=headers)
        again = await client.post("/chat", json={"message": PROBLEM}, headers=headers)
    assert first.json()["handoff_id"] == again.json()["handoff_id"]
    assert len(deps.state.list_handoffs("getnet")) == 1
