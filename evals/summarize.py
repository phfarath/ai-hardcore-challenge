"""Agrega relatórios de várias rodadas dos evals: taxa por caso, status observados e mudanças de
plano. Mede a variação entre execuções em vez de confiar numa rodada só.

    python -m evals.summarize evals/reports/run-A.json evals/reports/run-B.json ...
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def summarize(paths: list[str]) -> dict[str, Any]:
    runs = [json.loads(Path(p).read_text(encoding="utf-8")) for p in paths]
    per_case: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "passed": 0,
            "statuses": Counter(),
            "plan_changes": Counter(),
            "failed_checks": Counter(),
        }
    )
    for run in runs:
        for case in run["cases"]:
            entry = per_case[case["case_id"]]
            entry["passed"] += int(case["passed"])
            entry["statuses"][case.get("status") or "error"] += 1
            for change in case.get("plan_changes") or ["-"]:
                entry["plan_changes"][change] += 1
            for check, ok in (case.get("checks") or {}).items():
                if ok is False:
                    entry["failed_checks"][check] += 1
    return {
        "runs": [
            {"report": Path(p).name, "commit": r["commit"], **r["summary"]}
            for p, r in zip(paths, runs, strict=True)
        ],
        "cases": {
            case_id: {
                "passed": f"{e['passed']}/{len(runs)}",
                "statuses": dict(e["statuses"]),
                "plan_changes": dict(e["plan_changes"]),
                "failed_checks": dict(e["failed_checks"]),
            }
            for case_id, e in sorted(per_case.items())
        },
    }


def main() -> None:
    result = summarize(sys.argv[1:])
    for run in result["runs"]:
        score = f"{run['passed']}/{run['cases']} ({run['errors']} erros)"
        print(f"{run['report']} @ {run['commit']}: {score}")
    for case_id, entry in result["cases"].items():
        print(f"{entry['passed']:>5}  {case_id:<45} {entry['statuses']} {entry['plan_changes']}")


if __name__ == "__main__":
    main()
