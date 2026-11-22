"""Avaliação de IA: roda os casos de `evals/cases.json` contra a API real e pontua cada um.

Uso: `python -m evals.run_evals [--base-url http://localhost:8000] [--report-dir evals/reports]`

Verificações por caso (`None` = não se aplica e fica fora do denominador):
- route: fluxo escolhido;
- tools: todas as `required_tools` foram chamadas e nenhuma fora de `required ∪ allowed`
  (com `ordered`, as obrigatórias aparecem nessa ordem relativa);
- sources: ao menos uma fonte citada está em `expected_source_urls_any`;
- status: status de domínio esperado;
- preserve: termos obrigatórios presentes;
- records: cada grupo de `must_preserve_records` aparece junto numa mesma linha
  (valor ligado ao seu status, não um número solto);
- amounts: todo valor monetário "R$ x" da resposta pertence a `allowed_amounts`;
- anchors: para cada grupo de `must_mention_any`, ao menos um termo aparece;
- forbidden: nenhuma afirmação proibida (palavra inteira quando o termo é alfanumérico).

Uma falha de requisição (HTTP != 200, timeout) conta como falha em todas as verificações
aplicáveis. A pontuação detecta regressões objetivas; não substitui revisão semântica.
"""

import argparse
import asyncio
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict

from app.schemas import ChatResponse, Status, Workflow

CASES_FILE = Path("evals/cases.json")
CHECKS = [
    "route",
    "tools",
    "sources",
    "status",
    "preserve",
    "records",
    "amounts",
    "anchors",
    "forbidden",
]
_AMOUNT = re.compile(r"R\$\s*([\d.]+,\d{2})")


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    message: str
    user_id: str
    expected_workflow: Workflow
    required_tools: list[str]
    allowed_tools: list[str] = []
    ordered: bool = False
    expected_source_urls_any: list[str] | None = None
    expected_status: Status | None
    must_preserve: list[str] = []
    must_preserve_records: list[list[str]] = []
    allowed_amounts: list[str] | None = None
    must_mention_any: list[list[str]] = []
    must_not_claim: list[str] = []


class Score(BaseModel):
    case_id: str
    checks: dict[str, bool | None]
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(v is not False for v in self.checks.values())


def load_cases(path: Path = CASES_FILE) -> list[Case]:
    return [Case(**raw) for raw in json.loads(path.read_text("utf-8"))]


def _contains(text: str, term: str) -> bool:
    """Palavra inteira quando o termo começa/termina com caractere alfanumérico."""
    left = r"(?<!\w)" if term[:1].isalnum() else ""
    right = r"(?!\w)" if term[-1:].isalnum() else ""
    return re.search(left + re.escape(term.lower()) + right, text) is not None


def _subsequence(expected: list[str], actual: list[str]) -> bool:
    it = iter(actual)
    return all(name in it for name in expected)


def _check_tools(case: Case, used: list[str]) -> bool:
    if not set(case.required_tools) <= set(used):
        return False
    if not set(used) <= set(case.required_tools) | set(case.allowed_tools):
        return False
    return not case.ordered or _subsequence(case.required_tools, used)


def applicable(case: Case) -> dict[str, bool]:
    return {
        "route": True,
        "tools": True,
        "sources": bool(case.expected_source_urls_any),
        "status": case.expected_status is not None,
        "preserve": bool(case.must_preserve),
        "records": bool(case.must_preserve_records),
        "amounts": case.allowed_amounts is not None,
        "anchors": bool(case.must_mention_any),
        "forbidden": bool(case.must_not_claim),
    }


def score_case(case: Case, response: ChatResponse) -> Score:
    answer = response.answer.lower()
    lines = answer.splitlines()
    urls = case.expected_source_urls_any or []
    results: dict[str, bool] = {
        "route": response.workflow == case.expected_workflow,
        "tools": _check_tools(case, response.tools_used),
        "sources": any(s.url in urls for s in response.sources),
        "status": response.status == case.expected_status,
        "preserve": all(_contains(answer, p) for p in case.must_preserve),
        "records": all(
            any(all(_contains(line, t) for t in group) for line in lines)
            for group in case.must_preserve_records
        ),
        "amounts": set(_AMOUNT.findall(response.answer)) <= set(case.allowed_amounts or []),
        "anchors": all(any(_contains(answer, t) for t in group) for group in case.must_mention_any),
        "forbidden": not any(_contains(answer, f) for f in case.must_not_claim),
    }
    return Score(
        case_id=case.id,
        checks={k: results[k] if on else None for k, on in applicable(case).items()},
    )


def score_error(case: Case, error: str) -> Score:
    return Score(
        case_id=case.id,
        checks={k: False if on else None for k, on in applicable(case).items()},
        error=error,
    )


class Summary(BaseModel):
    cases: int
    passed: int
    errors: int
    metrics: dict[str, dict[str, int]]


def summarize(scores: list[Score]) -> Summary:
    metrics: dict[str, dict[str, int]] = {}
    for name in CHECKS:
        values = [s.checks[name] for s in scores if s.checks[name] is not None]
        metrics[name] = {"ok": sum(1 for v in values if v), "applicable": len(values)}
    return Summary(
        cases=len(scores),
        passed=sum(1 for s in scores if s.passed),
        errors=sum(1 for s in scores if s.error is not None),
        metrics=metrics,
    )


async def _ask(client: httpx.AsyncClient, case: Case) -> ChatResponse | str:
    try:
        response = await client.post(
            "/chat", json={"message": case.message, "user_id": case.user_id}
        )
    except httpx.HTTPError as exc:
        return f"request failed: {type(exc).__name__}"
    if response.status_code != 200:
        return f"HTTP {response.status_code}"
    return ChatResponse(**response.json())


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip()


def write_report(
    report_dir: Path, base_url: str, summary: Summary, rows: list[dict[str, object]]
) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"run-{stamp}.json"
    report = {
        "commit": _git_commit(),
        "base_url": base_url,
        "summary": summary.model_dump(),
        "cases": rows,
    }
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    return path


async def collect(
    base_url: str, request_timeout: float
) -> tuple[list[Score], list[dict[str, object]]]:
    cases = load_cases()
    scores: list[Score] = []
    rows: list[dict[str, object]] = []
    async with httpx.AsyncClient(base_url=base_url, timeout=request_timeout) as client:
        for case in cases:
            result = await _ask(client, case)
            if isinstance(result, str):
                score = score_error(case, result)
                print(f"ERROR {case.id}: {result}")
                rows.append({"case_id": case.id, "error": result, "checks": score.checks})
            else:
                score = score_case(case, result)
                marks = " ".join(
                    f"{k}={'-' if v is None else 'ok' if v else 'FAIL'}"
                    for k, v in score.checks.items()
                )
                print(f"{'PASS' if score.passed else 'FAIL'} {case.id} [{result.status}] {marks}")
                if not score.passed:
                    print(f"     answer: {result.answer[:240]!r}")
                rows.append(
                    {
                        "case_id": case.id,
                        "passed": score.passed,
                        "checks": score.checks,
                        "status": result.status,
                        "workflow": result.workflow,
                        "tools_used": result.tools_used,
                        "sources": [s.url for s in result.sources],
                        "answer": result.answer,
                    }
                )
            scores.append(score)
    return scores, rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--report-dir", type=Path, default=Path("evals/reports"))
    args = parser.parse_args()
    scores, rows = asyncio.run(collect(args.base_url, args.timeout))
    summary = summarize(scores)
    print("\nMétricas (acertos/aplicáveis; erros contam como falha):")
    for name, m in summary.metrics.items():
        print(f"  {name:10s} {m['ok']}/{m['applicable']}")
    print(f"\n{summary.passed}/{summary.cases} casos passaram ({summary.errors} erros)")
    print(f"Relatório: {write_report(args.report_dir, args.base_url, summary, rows)}")
    return 0 if summary.passed == summary.cases else 1


if __name__ == "__main__":
    raise SystemExit(main())
