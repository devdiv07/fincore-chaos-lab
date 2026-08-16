"""End-to-end verification of the deployment image.

Builds nothing and assumes `fincore-lab:candidate` exists. Starts a CLEAN
PostgreSQL container (never the development database), runs the image against
it exactly as a host would, and exercises all four experiments over HTTP.

    python scripts/verify_deployment.py

Prints PASS/FAIL per check and exits non-zero on any failure.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

IMAGE = "fincore-lab:candidate"
NET = "fincore-deploy-test"
PG = "fincore-deploy-pg"
APP = "fincore-deploy-app"
PORT = 8099
BASE = f"http://127.0.0.1:{PORT}"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{'  ' + detail if detail else ''}")
    return ok


def sh(*args: str, quiet: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=quiet, text=True)


def cleanup() -> None:
    sh("docker", "rm", "-f", APP)
    sh("docker", "rm", "-f", PG)
    sh("docker", "network", "rm", NET)


def get(path: str, timeout: int = 10):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, json.loads(r.read())


def post(path: str, payload: dict, timeout: int = 60):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def main() -> int:
    print("=== FINCORE CHAOS LAB — DEPLOYMENT VERIFICATION ===\n")
    cleanup()

    print("infrastructure")
    sh("docker", "network", "create", NET)
    # A CLEAN database. Nothing from development is reachable from here.
    sh("docker", "run", "-d", "--name", PG, "--network", NET,
       "-e", "POSTGRES_USER=lab", "-e", "POSTGRES_PASSWORD=labpass",
       "-e", "POSTGRES_DB=lab", "postgres:16-alpine")

    ready = False
    for _ in range(45):
        if sh("docker", "exec", PG, "pg_isready", "-U", "lab", "-d", "lab").returncode == 0:
            ready = True
            break
        time.sleep(1)
    if not check("clean postgres started", ready):
        cleanup()
        return 1

    # Exactly what a host provides: DATABASE_URL + PORT. Nothing else.
    run = sh("docker", "run", "-d", "--name", APP, "--network", NET,
             "-e", "DATABASE_URL=postgresql://lab:labpass@" + PG + ":5432/lab",
             "-e", f"PORT={PORT}",
             "-p", f"{PORT}:{PORT}", IMAGE)
    if not check("container started", run.returncode == 0, run.stderr.strip()[:120]):
        cleanup()
        return 1

    print("\nstartup")
    live = False
    for _ in range(60):
        try:
            status, body = get("/health", timeout=3)
            if status == 200 and body.get("status") == "ok":
                live = True
                break
        except Exception:
            time.sleep(1)
    check("GET /health (liveness)", live)

    rdy = False
    for _ in range(60):
        try:
            status, body = get("/ready", timeout=5)
            if status == 200 and body.get("status") == "ready":
                rdy = True
                break
        except Exception:
            pass
        time.sleep(1)
    check("GET /ready (migrated, db reachable)", rdy)

    logs = sh("docker", "logs", APP).stdout + sh("docker", "logs", APP).stderr
    check("migrations ran on the clean database", "database migrated to head" in logs)
    check("pinned core revision reported",
          "3d6cd7a09fb7841d0f5bda5d75a50781ca9223cf" in logs)
    check("no MISMATCH on the pinned revision", "MISMATCH" not in logs)
    check("janitor started", "janitor started" in logs)
    check("DATABASE_URL never printed", "labpass" not in logs)

    if not rdy:
        print("\nnot ready; dumping logs\n")
        print(logs[-3000:])
        cleanup()
        return 1

    print("\nexperiments (over HTTP, against the clean database)")

    status, r = post("/api/demo/run", {"experiment": "response_loss"})
    check("response loss: naive = 2 effects",
          status == 200 and r["naive"]["financial_effects"] == 2,
          f"got {r.get('naive', {}).get('financial_effects')}")
    check("response loss: fincore = 1 effect",
          r["fincore"]["financial_effects"] == 1 and r["fincore"]["final_state"] == "SUCCEEDED",
          f"got {r['fincore']['financial_effects']} / {r['fincore']['final_state']}")

    status, r = post("/api/demo/run", {"experiment": "worker_crash"})
    check("worker crash: 1 financial effect",
          status == 200 and r["financial_effects"] == 1 and r["final_state"] == "SUCCEEDED",
          f"workers={r.get('worker_count')} effects={r.get('financial_effects')}")

    status, r = post("/api/demo/run", {"experiment": "concurrency", "callers": 20})
    ok = (
        status == 200
        and r["callers"] == 20
        and r["execution_owners"] == 1
        and r["provider_invocations"] == 1
        and r["financial_effects"] == 1
        and r["attempt_rows"] == 1
    )
    check("20 callers: 1 owner, 1 provider call, 1 effect, 1 attempt row", ok,
          f"{r.get('callers')}/{r.get('execution_owners')}/"
          f"{r.get('provider_invocations')}/{r.get('financial_effects')}/{r.get('attempt_rows')}")
    blob = json.dumps(r).lower()
    check('never says "20 attempts"', "20 attempts" not in blob and "20 execution attempts" not in blob)

    status, r = post("/api/demo/run",
                     {"experiment": "intent_conflict", "retry_amount_paise": 20000})
    check("intent conflict: 0 extra calls, 0 extra effects",
          status == 200
          and r["provider_calls_caused_by_retry"] == 0
          and r["financial_effects_caused_by_retry"] == 0
          and r["conflict"] is True,
          f"conflict={r.get('conflict')}")

    print("\npublic surface")
    try:
        code = post("/api/demo/sweep", {})[0]
    except urllib.error.HTTPError as e:
        code = e.code
    check("public sweep endpoint is gone", code in (404, 405), f"status {code}")

    for name, payload, want in [
        ("unknown experiment rejected", {"experiment": "../../etc/passwd"}, 400),
        ("unbounded concurrency rejected", {"experiment": "concurrency", "callers": 5000}, 400),
        ("out-of-range amount rejected",
         {"experiment": "intent_conflict", "retry_amount_paise": 99999999}, 400),
    ]:
        status, body = post("/api/demo/run", payload)
        leaked = any(
            t in json.dumps(body).lower()
            for t in ("traceback", "psycopg", "sqlalchemy", "labpass", "/app/", "select ")
        )
        check(name, status == want and not leaked, f"status {status}")

    print("\nimage audit")
    ls = sh("docker", "run", "--rm", "--entrypoint", "sh", IMAGE, "-c",
            "ls -a /app; echo ---; ls /app/app; echo ---; id -un; echo ---; "
            "test -d /opt/fincore-core/.git && echo HAS_GIT || echo NO_GIT; echo ---; "
            "python -c \"import playwright\" 2>&1 | tail -1; echo ---; "
            "python -c \"import pytest\" 2>&1 | tail -1; echo ---; "
            "python -c \"import openai\" 2>&1 | tail -1").stdout
    check("no .env in image", "\n.env" not in ls)
    check("no tests/ or qa/ in image", "tests" not in ls.split("---")[0] and "qa" not in ls.split("---")[0])
    check("runs as non-root", "lab" in ls.split("---")[2])
    check("flagship .git stripped", "NO_GIT" in ls)
    check("playwright absent from runtime", "No module named 'playwright'" in ls)
    check("pytest absent from runtime", "No module named 'pytest'" in ls)
    check("openai absent from runtime", "No module named 'openai'" in ls)

    size = sh("docker", "image", "inspect", IMAGE, "--format", "{{.Size}}").stdout.strip()
    try:
        mb = f"{int(size) / 1_000_000:.0f} MB"
    except ValueError:
        mb = size
    print(f"\n  image size: {mb}")

    cleanup()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n=== {len(results) - len(failed)}/{len(results)} checks passed ===")
    if failed:
        print("FAILED: " + ", ".join(failed))
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        cleanup()
        sys.exit(130)
