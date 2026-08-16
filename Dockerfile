# FINCORE Chaos Lab — deployment image
#
# THE DEPENDENCY PROBLEM THIS SOLVES
# ----------------------------------
# Locally the demo imports the Financial Operation Core runtime from a sibling
# checkout. That directory does not exist in a container, and the runtime must
# not be copy-pasted in — a demo arguing for provenance cannot itself run on an
# unidentifiable copy of the code it is demonstrating.
#
# So the builder clones the PUBLIC repository at one exact commit and stamps the
# revision into the image. Nothing tracks `main`: a rebuild six months from now
# produces the same core. `app/core_link.py` reads that stamp at startup and,
# in cloud mode, refuses to start if it does not match the pin.
#
# Two stages so that git, the build toolchain and the .git directory stay out of
# the runtime image.

# ---------------------------------------------------------------- build stage
FROM python:3.12-slim-bookworm AS builder

ARG FINCORE_REPO=https://github.com/devdiv07/financial-operation-core.git
ARG FINCORE_COMMIT=3d6cd7a09fb7841d0f5bda5d75a50781ca9223cf

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Fetch exactly one commit. `git checkout <sha>` is what pins it; cloning a
# branch and hoping would not.
WORKDIR /build
RUN git clone --filter=blob:none "${FINCORE_REPO}" core \
 && cd core \
 && git checkout --quiet "${FINCORE_COMMIT}" \
 && test "$(git rev-parse HEAD)" = "${FINCORE_COMMIT}" \
 && echo "${FINCORE_COMMIT}" > /build/core/PINNED_COMMIT \
 && rm -rf /build/core/.git /build/core/.github

# Dependencies into a self-contained venv, copied wholesale to the runtime
# stage so no compiler or package index is needed there.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements-runtime.txt /build/requirements-runtime.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /build/requirements-runtime.txt

# -------------------------------------------------------------- runtime stage
FROM python:3.12-slim-bookworm AS runtime

ARG FINCORE_COMMIT=3d6cd7a09fb7841d0f5bda5d75a50781ca9223cf

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    FINCORE_FLAGSHIP_PATH=/opt/fincore-core \
    FINCORE_STRICT_PIN=1

# Unprivileged. Nothing here writes to disk at runtime.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin lab

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build/core /opt/fincore-core

WORKDIR /app
COPY --chown=lab:lab app/ /app/app/
COPY --chown=lab:lab scripts/smoke_demo.py /app/scripts/smoke_demo.py

# Fail the BUILD, not the first request, if the pin was not applied.
RUN test "$(cat /opt/fincore-core/PINNED_COMMIT)" = "${FINCORE_COMMIT}" \
 && test -f /opt/fincore-core/src/fincore/engine.py \
 && test -f /opt/fincore-core/experiments/agent_execution/baselines.py \
 && test -f /opt/fincore-core/alembic/env.py

USER lab

# PORT is supplied by the host. Never hardcoded; 8000 is only a local fallback.
ENV PORT=8000
EXPOSE 8000

# One worker, no reload. Migrations and the pin check run in the app's lifespan
# before traffic is accepted, so there is no separate entrypoint script to keep
# in sync with the application's own startup logic.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --no-server-header --log-level info"]
