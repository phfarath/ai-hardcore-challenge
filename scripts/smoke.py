"""Smoke da imagem construída (T09): roda contra um container já no ar, só com a biblioteca padrão.

Não chama o LLM: verifica processo vivo, prontidão com os checks declarados, autenticação e
autorização (recusadas antes do Router). Uso:
    python scripts/smoke.py --base-url http://localhost:8000 [--wait 60] [--require-ready]
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any


def call(
    base: str, method: str, path: str, body: dict[str, Any] | None = None, token: str = ""
) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def wait_alive(base: str, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if call(base, "GET", "/health")[0] == 200:
                return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(1)
    raise SystemExit(f"FAIL /health did not answer within {seconds:.0f}s")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--wait", type=float, default=60)
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    wait_alive(base, args.wait)

    failures: list[str] = []

    def check(name: str, ok: bool, detail: object) -> None:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
        if not ok:
            failures.append(name)

    status, ready = call(base, "GET", "/ready")
    checks = ready.get("checks", {})
    check("ready declares checks", set(checks) == {"index", "state", "lifecycle"}, ready)
    check("ready status code", status == 200 or (status == 503 and not args.require_ready), status)
    check("state reachable", checks.get("state") == "ok", checks.get("state"))

    status, body = call(base, "POST", "/chat", {"message": "oi"})
    check("chat without token is 401", status == 401 and body == {"error": "unauthorized"}, status)

    status, body = call(base, "POST", "/dev/token", {"sub": "user-a"})
    if status == 200:
        token = body["access_token"]
        status, body = call(
            base, "POST", "/chat", {"message": "oi", "user_id": "cliente2000"}, token
        )
        check("foreign customer selector is 403", status == 403, (status, body))
    else:
        print(f"SKIP dev issuer disabled ({status}): authorization check needs a real token")

    print("smoke:", "FAILED " + ", ".join(failures) if failures else "OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
