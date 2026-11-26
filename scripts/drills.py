"""Carga e falhas provocadas (T10) contra containers da mesma imagem, com configuração alterada.

Cada drill sobe um container próprio (porta 18xxx), mede e derruba. Usa o `.env` do projeto via
`--env-file` (o script não o lê) e o índice do volume do compose, só leitura.

    python scripts/drills.py [--image entrevista-getnet-api] [--out docs/evidence/drills.json]

Drills:
- saturation: MAX_CONCURRENT_CHATS=2 e 6 pedidos simultâneos → excedentes recebem 503 imediato;
- provider_down: chave OpenAI inválida → 503 llm_unavailable sem nova tentativa, rápido;
- index_missing: índice vazio → /ready 503 e pergunta de RAG → 503 index_unavailable;
- sigterm_in_flight: docker stop durante um pedido → o pedido termina e os clientes fecham.
"""

import argparse
import asyncio
import json
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

SOURCE_VOLUME = "entrevista-getnet_chroma_data"
VOLUME = "getnet-drills-chroma"  # cópia: os drills não dividem o índice com a API no ar
KNOWLEDGE = "Como funciona a antecipação de recebíveis na Getnet?"


def docker_run(name: str, port: int, image: str, env: dict[str, str], index: bool = True) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    cmd = ["docker", "run", "-d", "--name", name, "-p", f"{port}:8000", "--env-file", ".env"]
    # caminhos do container, como no compose: o .env local aponta para pastas do repositório
    cmd += ["-e", "APP_ENV=development", "-e", "CHROMA_PATH=/data/chroma"]
    cmd += ["-e", "STATE_PATH=/tmp/state/state.db"]
    for key, value in env.items():
        cmd += ["-e", f"{key}={value}"]
    if index:
        cmd += ["-v", f"{VOLUME}:/data/chroma"]
    subprocess.run([*cmd, image], check=True, capture_output=True)


def copy_index(image: str) -> None:
    subprocess.run(["docker", "volume", "rm", "-f", VOLUME], capture_output=True)
    copy = ["docker", "run", "--rm", "--user", "root", "-v", f"{SOURCE_VOLUME}:/from:ro"]
    copy += ["-v", f"{VOLUME}:/to", image, "sh", "-c", "cp -a /from/. /to/ && chown -R 10001 /to"]
    subprocess.run(copy, check=True, capture_output=True)


def stop(name: str, timeout: int = 25) -> str:
    subprocess.run(["docker", "stop", "-t", str(timeout), name], capture_output=True)
    logs = subprocess.run(
        ["docker", "logs", name], capture_output=True, text=True, encoding="utf-8"
    )
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    return logs.stdout + logs.stderr


async def wait_alive(base: str, seconds: float = 60) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=5) as client:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                if (await client.get("/health")).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
    raise RuntimeError(f"{base} did not come up")


async def token(client: httpx.AsyncClient, sub: str = "user-a") -> dict[str, str]:
    response = await client.post("/dev/token", json={"sub": sub})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def timed(client: httpx.AsyncClient, headers: dict[str, str], message: str) -> dict[str, Any]:
    started = time.perf_counter()
    response = await client.post("/chat", json={"message": message}, headers=headers)
    body = response.json()
    return {
        "status_code": response.status_code,
        "result": body.get("status") or body.get("error"),
        "seconds": round(time.perf_counter() - started, 2),
        "retry_after": response.headers.get("Retry-After"),
    }


async def saturation(image: str) -> dict[str, Any]:
    name, port = "drill-saturation", 18001
    docker_run(name, port, image, {"MAX_CONCURRENT_CHATS": "2"})
    base = f"http://localhost:{port}"
    try:
        await wait_alive(base)
        async with httpx.AsyncClient(base_url=base, timeout=120) as client:
            headers = await token(client)
            results = await asyncio.gather(*(timed(client, headers, KNOWLEDGE) for _ in range(6)))
    finally:
        stop(name)
    served = [r for r in results if r["status_code"] == 200]
    rejected = [r for r in results if r["status_code"] == 503]
    return {
        "config": {"max_concurrent_chats": 2, "concurrent_requests": 6},
        "served": len(served),
        "rejected_overloaded": len(rejected),
        "rejected_max_seconds": max((r["seconds"] for r in rejected), default=None),
        "served_p50_seconds": statistics.median([r["seconds"] for r in served]) if served else None,
        "served_max_seconds": max((r["seconds"] for r in served), default=None),
        "results": results,
    }


async def provider_down(image: str) -> dict[str, Any]:
    name, port = "drill-provider", 18002
    docker_run(name, port, image, {"OPENAI_API_KEY": "sk-invalid-drill"})
    base = f"http://localhost:{port}"
    try:
        await wait_alive(base)
        async with httpx.AsyncClient(base_url=base, timeout=120) as client:
            result = await timed(client, await token(client), KNOWLEDGE)
    finally:
        logs = stop(name)
    failed = [json.loads(line) for line in logs.splitlines() if '"request_failed"' in line]
    return {"response": result, "log": failed[-1] if failed else None}


async def index_missing(image: str) -> dict[str, Any]:
    name, port = "drill-index", 18003
    docker_run(name, port, image, {}, index=False)
    base = f"http://localhost:{port}"
    try:
        await wait_alive(base)
        async with httpx.AsyncClient(base_url=base, timeout=120) as client:
            ready = await client.get("/ready")
            result = await timed(client, await token(client), KNOWLEDGE)
    finally:
        stop(name)
    return {"ready": {"status_code": ready.status_code, **ready.json()}, "chat": result}


async def sigterm_in_flight(image: str) -> dict[str, Any]:
    name, port = "drill-sigterm", 18004
    docker_run(name, port, image, {})
    base = f"http://localhost:{port}"
    await wait_alive(base)
    async with httpx.AsyncClient(base_url=base, timeout=120) as client:
        headers = await token(client)
        request = asyncio.create_task(timed(client, headers, KNOWLEDGE))
        await asyncio.sleep(2)  # pedido em andamento
        stopping = asyncio.create_task(asyncio.to_thread(stop, name))
        result = await request
        logs = await stopping
    events = [line for line in logs.splitlines() if "Shutting down" in line or '"shutdown"' in line]
    return {"in_flight_request": result, "shutdown_log": events}


DRILLS = {
    "saturation": saturation,
    "provider_down": provider_down,
    "index_missing": index_missing,
    "sigterm_in_flight": sigterm_in_flight,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="entrevista-getnet-api")
    parser.add_argument("--out", default="docs/evidence/drills.json")
    parser.add_argument("--only", choices=list(DRILLS), action="append")
    args = parser.parse_args()
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    report: dict[str, Any] = {"commit": commit, "image": args.image}
    copy_index(args.image)
    for name in args.only or list(DRILLS):
        print(f"== {name}")
        report[name] = asyncio.run(DRILLS[name](args.image))
        print(json.dumps(report[name], ensure_ascii=False)[:600])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"report: {out}")


if __name__ == "__main__":
    main()
