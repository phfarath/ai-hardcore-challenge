"""Avaliação de IA: roda os casos de `evals/cases.json` contra a API real e pontua cada um.

Uso: `python -m evals.run_evals [--base-url http://localhost:8000]`
Métricas: acerto de rota, de ferramentas, de fontes esperadas, de status, preservação de valores
e ausência de afirmações proibidas. A revisão manual do texto continua necessária: esta
pontuação detecta regressões objetivas, não qualidade semântica.
"""

import argparse
import asyncio
import json
from pathlib import Path

import httpx
from pydantic import BaseModel

from app.schemas import ChatResponse, Status, Workflow

CASES_FILE = Path("evals/cases.json")
CHECKS = ["route", "tools", "sources", "status", "preserve", "forbidden"]


class Case(BaseModel):
    id: str
    message: str
    user_id: str
    expected_workflow: Workflow
    expected_tools: list[str]
    expected_source_urls_any: list[str] | None
    expected_status: Status | None
    must_preserve: list[str]
    must_not_claim: list[str]


class Score(BaseModel):
    case_id: str
    checks: dict[str, bool | None]

    @property
    def passed(self) -> bool:
        return all(v is not False for v in self.checks.values())


def load_cases(path: Path = CASES_FILE) -> list[Case]:
    return [Case(**raw) for raw in json.loads(path.read_text("utf-8"))]


def score_case(case: Case, response: ChatResponse) -> Score:
    """`None` = a verificação não se aplica ao caso."""
    answer = response.answer.lower()
    urls = case.expected_source_urls_any
    return Score(
        case_id=case.id,
        checks={
            "route": response.workflow == case.expected_workflow,
            "tools": set(case.expected_tools) <= set(response.tools_used),
            "sources": None if not urls else any(s.url in urls for s in response.sources),
            "status": None
            if case.expected_status is None
            else response.status == case.expected_status,
            "preserve": all(p.lower() in answer for p in case.must_preserve),
            "forbidden": not any(f.lower() in answer for f in case.must_not_claim),
        },
    )


async def _ask(client: httpx.AsyncClient, case: Case) -> ChatResponse | str:
    try:
        response = await client.post(
            "/chat", json={"message": case.message, "user_id": case.user_id}
        )
    except httpx.HTTPError as exc:
        return f"request failed: {type(exc).__name__}"
    if response.status_code != 200:
        return f"HTTP {response.status_code}: {response.text[:120]}"
    return ChatResponse(**response.json())


async def run(base_url: str, request_timeout: float) -> int:
    cases = load_cases()
    totals = {c: [0, 0] for c in CHECKS}
    failed = 0
    async with httpx.AsyncClient(base_url=base_url, timeout=request_timeout) as client:
        for case in cases:
            result = await _ask(client, case)
            if isinstance(result, str):
                failed += 1
                print(f"ERROR {case.id}: {result}")
                continue
            score = score_case(case, result)
            failed += 0 if score.passed else 1
            for name, value in score.checks.items():
                if value is not None:
                    totals[name][0] += int(value)
                    totals[name][1] += 1
            marks = " ".join(
                f"{k}={'-' if v is None else 'ok' if v else 'FAIL'}"
                for k, v in score.checks.items()
            )
            print(f"{'PASS' if score.passed else 'FAIL'} {case.id} [{result.status}] {marks}")
            if not score.passed:
                print(f"     answer: {result.answer[:240]!r}")
    print("\nMétricas (acertos/aplicáveis):")
    for name, (ok, total) in totals.items():
        print(f"  {name:10s} {ok}/{total}")
    print(f"\n{len(cases) - failed}/{len(cases)} casos passaram")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    return asyncio.run(run(args.base_url, args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
