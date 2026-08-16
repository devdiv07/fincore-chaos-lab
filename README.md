# FINCORE — Financial Operation Chaos Lab

**Can you make it refund twice?**

An interactive sandbox for breaking financial-operation execution under response
loss, worker crashes, concurrent callers, and changed intent.

**[▶ Try the live Chaos Lab](https://fincore-chaos-lab-1.onrender.com)**  
**[▶ Financial Operation Core](https://github.com/devdiv07/financial-operation-core)**  
**[▶ Watch the 5-minute walkthrough](https://youtu.be/QcwbQ7QrX9o)**  
**[▶ Razorpay MCP contribution](https://github.com/razorpay/razorpay-mcp-server/pull/114)**

The public playground runs against a deterministic provider fixture modeled from
measured Razorpay Test Mode behavior. It makes no live Razorpay transactions and
moves no real money.

---

## Try to break it

Four ways to break the same ₹100 refund:

| | What happens | Result |
|---|---|---|
| **Lose the response** | the provider completes the refund, the response never arrives | naive retry → **2 effects** · FinCore → **1** |
| **Crash the worker** | the money moves, then the process dies before recording it | 2 workers, 2 invocations, **1 effect** |
| **Fire concurrent callers** | 2–20 callers submit the same refund at once | 1 execution owner, 1 provider call, **1 effect** |
| **Change the amount** | retry the same operation asking for a different amount | refused before the provider · **0 extra effects** |

Every number on screen is read back from the database and the provider fixture
after execution. Nothing is precomputed for the animation, and the page shows an
error rather than a result if the backend is unavailable.

Concurrent callers are **callers**, not attempts: one caller wins the lease, the
persisted attempt-row count is 1, and the UI is tested to never say otherwise.

## What powers it

The experiments execute the real Financial Operation Core runtime, pinned to one
commit:

<https://github.com/devdiv07/financial-operation-core/commit/3d6cd7a09fb7841d0f5bda5d75a50781ca9223cf>

The runtime is not vendored or copy-pasted. The container build clones that
exact commit, stamps the revision into the image, and refuses to start if what
it finds does not match. The naive comparison lane is the flagship's own
`NaiveRefundTool`, which that project already labels `UNSAFE_BASELINE` — it was
not written for this demo.

The database is real PostgreSQL, built by the flagship's own Alembic migrations.

## What is not claimed

- No claim that the operation is executed upstream only once. It is
  at-least-once execution carrying a stable, persisted provider idempotency
  identity that the provider deduplicates — two invocations, one effect, which
  is why those two counters are always shown separately.
- Nothing here is a claim about a defect in Razorpay. A new idempotency key
  being a new operation is *correct* provider behaviour; the duplicate belongs
  to the caller.
- The naive lane is a deliberately weak strategy, not a description of how
  competent backends are written. A disciplined caller that persists and reuses
  the provider key avoids this specific duplicate path.
- No Razorpay API call, no model/LLM call, and no API key of any kind. There is
  no code path in this project that reads one.

## Evidence

| | |
|---|---|
| Flagship project | <https://github.com/devdiv07/financial-operation-core> |
| Real provider measurement | [`evidence/razorpay-test-mode`](https://github.com/devdiv07/financial-operation-core/tree/main/evidence/razorpay-test-mode) — Razorpay Test Mode, captured separately from this playground |
| Razorpay MCP contribution | <https://github.com/razorpay/razorpay-mcp-server/pull/114> — refund idempotency-key support, open for review |

## Running it locally

Requires Docker and Python 3.12+, plus a checkout of the flagship as a sibling
directory. Set `FINCORE_FLAGSHIP_PATH` if it lives elsewhere.

```powershell
.\start-demo.ps1          # starts Postgres, migrates, serves on :8000
.\stop-demo.ps1           # stops only this demo's container
```

```powershell
.venv\Scripts\python -m pytest              # 239 tests
.venv\Scripts\python scripts\smoke_demo.py  # headless scenario check
```

The browser tests need a running server and Playwright; they skip automatically
when either is absent.

## Deploying

See [`DEPLOY.md`](DEPLOY.md). One web service, one PostgreSQL service, and a
static site for the frontend. The API needs `DATABASE_URL`, plus
`FINCORE_ALLOWED_ORIGIN` set to the static site's origin so its browser calls
are permitted. `PORT` comes from the host.

```bash
docker build -t fincore-lab:candidate .
python scripts/verify_deployment.py   # clean DB, all four experiments over HTTP
```

## Layout

```
app/              FastAPI application, experiments, static frontend
  scenario.py       response loss (two-lane comparison)
  experiments.py    worker crash · concurrency · intent conflict
  session.py        per-run tenant isolation
  limits.py         rate limiting, safe error envelope
  janitor.py        bounded in-process cleanup
  core_link.py      pinned-runtime binding and provenance check
tests/            239 tests, incl. frozen backend contracts and browser tests
scripts/          smoke test, contract snapshots, deployment verification
```

## License

MIT
