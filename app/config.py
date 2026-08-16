"""Demo configuration.

Every value here is a LOCAL DEVELOPMENT default. There is no credential in this
file that grants access to anything outside this machine, and the demo has no
code path that reads a provider or model API key.
"""

from __future__ import annotations

import os

__all__ = [
    "DATABASE_URL",
    "DEMO_SCHEMA",
    "PAYMENT_ID",
    "PAYMENT_AMOUNT_PAISE",
    "REFUND_AMOUNT_PAISE",
    "TENANT_ID",
    "PRINCIPAL_ID",
    "OPERATION_REF",
    "WORKER_ID",
    "HOST",
    "PORT",
    "sync_database_url",
]

#: Local default: port 55433 rather than the 55432 in the original brief,
#: because the flagship's own test container (`fincore-pg`) already owns 55432
#: on the development machine and the demo must not disturb it.
_LOCAL_DATABASE_URL = "postgresql+psycopg://fincore:fincore@127.0.0.1:55433/fincore_demo"


def normalize_database_url(url: str) -> str:
    """Accept what a host actually hands us and make it usable.

    Railway (and Heroku-style providers) supply `postgresql://...`, and some
    still supply the long-deprecated `postgres://`. SQLAlchemy needs an explicit
    async driver for the engine, so the scheme is normalised here rather than
    being duplicated at every call site.

    Only the scheme is touched. Credentials are never logged, parsed apart, or
    rewritten.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def _database_url() -> str:
    # Precedence: explicit demo override, then the platform's DATABASE_URL,
    # then the local development container.
    raw = (
        os.environ.get("FINCORE_DEMO_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or _LOCAL_DATABASE_URL
    )
    return normalize_database_url(raw)


DATABASE_URL = _database_url()

#: True when running against a platform-provided database rather than the local
#: development container. Used only to pick sensible defaults, never to change
#: experiment behaviour.
IS_CLOUD = bool(os.environ.get("DATABASE_URL")) and not os.environ.get(
    "FINCORE_DEMO_DATABASE_URL"
)

#: All demo tables live in their own schema, so the demo database could be
#: shared with something else without collision.
DEMO_SCHEMA = os.environ.get("FINCORE_DEMO_SCHEMA", "demo")

# ---------------------------------------------------------------- the scenario
#: Fixture payment. Not a real Razorpay payment id and never sent anywhere.
PAYMENT_ID = "pay_DEMOFIXTURE0001"

#: ₹1,000 captured, of which we refund ₹100. Headroom matters: the naive lane
#: legitimately creates TWO ₹100 refunds, and the provider fixture refuses to
#: over-refund a payment, so the captured amount must cover both.
PAYMENT_AMOUNT_PAISE = 100_000
REFUND_AMOUNT_PAISE = 10_000

TENANT_ID = "merchant-demo"
PRINCIPAL_ID = "agent-demo"

#: The durable business-operation identity, supplied by trusted application
#: code. The model/agent never invents this.
OPERATION_REF = "refund-demo-001"

WORKER_ID = "demo-worker-A"

# ------------------------------------------------------- public-safety limits
#: Concurrency options offered to the reviewer. Allow-listed, never free-form:
#: an unbounded value would be a trivial resource-exhaustion lever on a public
#: URL, and the connection pool is sized for the largest of these.
CONCURRENCY_CHOICES = (2, 5, 10, 20)

#: Interactive retry amount for the intent-conflict experiment, in paise.
#: 1 rupee .. 1000 rupees. Bounded so the input cannot be used to probe the
#: provider fixture's error surface or to push absurd values into the database.
MIN_RETRY_PAISE = 100
MAX_RETRY_PAISE = 100_000

#: Sliding-window rate limit per visitor session.
RATE_LIMIT_RUNS = int(os.environ.get("FINCORE_DEMO_RATE_LIMIT_RUNS", "20"))
RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("FINCORE_DEMO_RATE_LIMIT_WINDOW", "60"))

#: Hard ceiling on a single experiment. The runtime finishes in well under a
#: second; anything approaching this means something is wrong and the request
#: should be cut rather than held open.
RUN_TIMEOUT_SECONDS = float(os.environ.get("FINCORE_DEMO_RUN_TIMEOUT", "30"))

#: Rows older than this are swept. Runs take under a second, so nothing that a
#: visitor is currently looking at is ever close to this age.
DATA_RETENTION_MINUTES = int(os.environ.get("FINCORE_DEMO_RETENTION_MINUTES", "60"))

#: Cookie carrying the opaque visitor session id. No personal data, no IP.
SESSION_COOKIE = "fincore_lab_session"

#: The flagship commit this demo is pinned to. Deployment builds check this out
#: explicitly rather than tracking main, so a published Lab always corresponds
#: to a known runtime.
PINNED_FLAGSHIP_COMMIT = "3d6cd7a09fb7841d0f5bda5d75a50781ca9223cf"

# ------------------------------------------------------------------- transport
#: In a container the app must bind every interface; locally it should not.
HOST = os.environ.get("FINCORE_DEMO_HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")

#: PORT is assigned by the host (Railway, Fly, Render). Never hardcoded.
PORT = int(os.environ.get("PORT") or os.environ.get("FINCORE_DEMO_PORT") or "8000")

# --------------------------------------------------------------------- janitor
#: How often the in-process janitor sweeps expired demo runs. Single-process
#: assumption -- see DEPLOY.md.
JANITOR_INTERVAL_SECONDS = int(os.environ.get("FINCORE_DEMO_JANITOR_SECONDS", "900"))
JANITOR_ENABLED = os.environ.get("FINCORE_DEMO_JANITOR", "1") != "0"


def sync_database_url(url: str | None = None) -> str:
    """Alembic runs synchronously; strip any async driver marker."""
    return (url or DATABASE_URL).replace("+psycopg_async", "+psycopg").replace(
        "+asyncpg", "+psycopg"
    )
