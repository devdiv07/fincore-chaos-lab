# Deploying FINCORE Chaos Lab

One web service, one PostgreSQL service. No API keys, no provider credentials,
no model provider. The application is a Dockerfile; Railway (or any host that
builds a Dockerfile) needs almost nothing from you.

---

## What the container actually runs

The Financial Operation Core runtime is **not vendored and not copy-pasted**. The
build clones the public repository at one exact commit:

```
devdiv07/financial-operation-core @ 3d6cd7a09fb7841d0f5bda5d75a50781ca9223cf
```

into `/opt/fincore-core`, writes that SHA to `/opt/fincore-core/PINNED_COMMIT`,
and deletes the `.git` directory. Nothing tracks `main`, so a rebuild next year
produces the same core.

That pin is enforced three times:

| When | Check |
|---|---|
| build | `git rev-parse HEAD` must equal the pin, or the build fails |
| build | the `PINNED_COMMIT` stamp is re-verified in the runtime stage |
| startup | `FINCORE_STRICT_PIN=1` makes a mismatch refuse to start |

A demo arguing for provenance should not itself run on an unidentifiable copy of
the code it demonstrates.

## Railway setup

1. **Create a PostgreSQL service.** Railway provisions it and exposes
   `DATABASE_URL`.

2. **Create the web service** from this repository. Railway detects the
   `Dockerfile` and builds it — no `railway.toml` is needed, and none is
   included, because adding one here would not change any behaviour.

3. **Set exactly one variable** on the web service:

   ```
   DATABASE_URL=${{Postgres.DATABASE_URL}}
   ```

   `PORT` is injected by Railway; the container reads it and binds
   `0.0.0.0:$PORT`. Do not set `PORT` yourself.

4. **Health checks** (optional but recommended):

   | Path | Meaning |
   |---|---|
   | `/health` | process is up. Deliberately does **not** touch the database — a liveness probe that fails on a database blip would restart a healthy process |
   | `/ready` | database reachable **and** migrated, so an experiment can run right now |

   Point Railway's healthcheck at `/ready`.

Nothing else is required. There is no secret to configure, because there is no
code path that reads one.

## Two surfaces: static shell + execution API

The API runs on a free tier and sleeps after inactivity. A reviewer arriving at
a sleeping service would wait ~50 s staring at nothing, so the frontend is
deployed separately as static files:

```
Render Static Site  (always instant)
        |  HTTPS, CORS-allowlisted
        v
Render Web Service  (FastAPI, sleeps when idle)
        |
        v
Neon PostgreSQL
```

The shell renders immediately, then polls `GET /ready` and enables the
experiment controls **only** when that returns HTTP 200. Readiness is never
inferred from elapsed time — the same rule as the experiment counters.

### Render Static Site settings

| Setting | Value |
|---|---|
| Repository | `devdiv07/fincore-chaos-lab` |
| Branch | `main` |
| Root Directory | *(blank — repository root)* |
| Build Command | *(blank — there is no build step)* |
| Publish Directory | `app/static` |

There is **no `frontend/` copy of the UI**, and that is deliberate. Duplicating
the assets would let the static site and the API drift apart — the exact class
of bug that already cost us an afternoon when a browser served a stale bundle.
`app/static` is the single source: the API mounts it at `/`, and the static site
publishes the same directory. Both serve byte-identical files.

Asset paths are relative (`styles.css`, not `/static/styles.css`), which is what
lets one directory serve both surfaces without a build step rewriting anything.

### Web Service settings for the split

Add one variable to the **API** service:

```
FINCORE_ALLOWED_ORIGIN=https://<your-static-site>.onrender.com
```

Comma-separated if you need more than one. Leave it unset locally: with no
value, no CORS middleware is installed at all and the API stays strictly
same-origin.

If the API URL ever changes, update the one constant at the top of
`app/static/config.js`. It appears nowhere else — a test enforces that.

### Why no cross-origin cookies

The session id used to be an `HttpOnly` cookie. A `SameSite=Lax` cookie is not
sent on cross-site `fetch`, so under the split every request would have arrived
with a fresh session.

What actually depends on session continuity is **rate limiting**. Tenant
isolation does not: a tenant is `lab-<session>-<run>` and the run half is random
per request, so every run is its own namespace regardless.

The fix is therefore the small one — the shell holds an opaque id in
`localStorage` and sends it as `X-Fincore-Session`. `allow_credentials` stays
`False`, no cookie crosses origins, and the demo does not depend on third-party
cookies surviving browser policy changes.

Stated plainly: that id is not a credential. It namespaces demo rows and keys a
rate-limit bucket, and it was equally client-controlled as a cookie, so a caller
determined to evade the rate limit could always rotate it. Real abuse protection
would have to sit in front of the app.

The API also still serves the UI at its own URL as an engineering fallback, and
that path continues to use the cookie because it is same-origin.

## Managed Postgres (Neon, Supabase, Render, Railway)

Set `DATABASE_URL` to the connection string the provider gives you. Both
`postgres://` and `postgresql://` are accepted and normalised to the async
driver internally.

**Query parameters are preserved.** Managed providers append their own —
Neon issues:

```
postgresql://user:pass@ep-xxx.region.aws.neon.tech/neondb?sslmode=require&channel_binding=require
```

The migration step needs to add `options=-csearch_path=demo` to that URL. It
does so by parsing the URL and setting the key structurally, never by
concatenating `"?options=..."`. That distinction is not cosmetic: appending a
second `?` does not raise, it gets absorbed into the preceding value, producing
`channel_binding=require?options=-csearch_path=demo`, which libpq rejects as a
bare `OperationalError` from Alembic. This exact bug broke a Render + Neon
deployment; `tests/test_deployment.py` now pins the behaviour.

### Neon: use the direct endpoint, not the pooler

Neon offers two hostnames for the same database. The pooled one contains
**`-pooler`**:

```
ep-cool-darkness-a1b2c3-pooler.region.aws.neon.tech   <- pooled (PgBouncer)
ep-cool-darkness-a1b2c3.region.aws.neon.tech          <- direct
```

Use the **direct** endpoint for `DATABASE_URL`. Schema migrations issue DDL and
depend on session-level settings (`options=-csearch_path=...`), which a
transaction-pooled connection does not reliably carry.

For a demo of this size one direct connection string is all that is needed —
the app runs a single worker with a small pool. A separate pooled URL for
runtime traffic is only worth adding if connection count actually becomes a
problem, and no hostname is hardcoded anywhere in this repository.

### Optional variables

| Variable | Default | Purpose |
|---|---|---|
| `FINCORE_DEMO_SCHEMA` | `demo` | schema the flagship migrations are applied into |
| `FINCORE_DEMO_RETENTION_MINUTES` | `60` | age after which a finished run is swept |
| `FINCORE_DEMO_JANITOR_SECONDS` | `900` | how often the janitor sweeps |
| `FINCORE_DEMO_RATE_LIMIT_RUNS` | `20` | runs per window per visitor session |
| `FINCORE_DEMO_RUN_TIMEOUT` | `30` | hard ceiling on one experiment, seconds |
| `FINCORE_STRICT_PIN` | `1` in cloud | refuse to start on a core revision mismatch |

## Startup sequence

The application's own lifespan does the work, so there is no entrypoint script
to drift out of sync with it:

1. verify the core revision against the pin (strict in cloud mode)
2. create the schema if absent, run the **flagship** Alembic migrations to head
3. start the janitor
4. accept traffic

Migrations run **before** the first request, so a cold start never pays for
schema work. If migration fails, startup fails — loudly. A Lab serving
experiments against an unmigrated database would be worse than one that refuses
to boot.

`DATABASE_URL` is never logged.

## Single-process assumptions

This is deliberately **one replica, one Uvicorn worker**, and two components
depend on that:

- **The janitor** runs as a background task inside the web process. Two replicas
  would give two janitors — harmless, since the delete is bounded, idempotent,
  and age-based, but redundant.
- **The rate limiter** keeps counters in that process's memory. Two replicas
  would mean per-replica limits, i.e. weaker than intended.

Scaling out would mean moving the limiter to shared storage and the sweep to a
single scheduled job. That is out of scope for a hiring demo, and is written
down here rather than discovered later.

## Cleanup

Runs are isolated by `tenant_id`, never by deleting anything, so a visitor
pressing Run can never disturb another visitor's in-flight experiment. The
janitor deletes finished runs **by age only** — a run in progress is seconds
old and the retention window is an hour, so an active run is never a candidate.
Deletes are batched (`LIMIT 500`) and a failure is logged and ignored rather
than taking the server down.

There is **no public cleanup endpoint**. An earlier revision exposed
`POST /api/demo/sweep`; an unauthenticated public route whose only job is to
delete rows is a liability with no upside, and the browser has no business
triggering maintenance. The function is called internally by the janitor and the
route returns 404.

## Verifying a build locally

```bash
docker build -t fincore-lab:candidate .
python scripts/verify_deployment.py
```

The verification script starts a **clean** PostgreSQL container — never the
development database — runs the image against it exactly as a host would, and
checks liveness, readiness, migrations, the pinned revision, all four
experiments over HTTP, the rejected-input paths, and the image contents.

## What is still true in production

Unchanged from local: deterministic provider fixture, zero Razorpay calls, zero
model calls, 4 KB body cap, per-session rate limiting, request timeouts,
allow-listed concurrency values (2/5/10/20 only — a visitor cannot request an
unbounded count), validated retry amounts (₹1–₹1000), safe error envelopes with
a correlation id, and the CSP/nosniff/frame-deny headers.

The runtime image contains no test framework, no browser, no Playwright, no
model SDK, no `.env`, and runs as an unprivileged user.
