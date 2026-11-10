# syntax=docker/dockerfile:1
#
# Provably-correct chatbot image (web service and builder).
# Build context = the REPO ROOT (set in cdk/lib/chatbot-stack.ts via fromAsset).
# Multi-stage: (1) build the React SPA with Vite, (2) assemble the Python
# runtime with cvc5 and serve both the API and the built SPA from one FastAPI app.

# ---------- Stage 1: build the React frontend ----------
# Node 22: Vite 8 needs >=22.12 (or ^20.19) and Vitest 5 needs ^22.12.
FROM public.ecr.aws/docker/library/node:22-bookworm AS frontend
WORKDIR /build
# Pin npm to v11 to match the npm that generated frontend/package-lock.json
# (lockfileVersion 3). node:22 ships npm 10, which dedupes transitive deps
# differently and makes `npm ci` reject the lock with "Missing: yaml@... from
# lock file". Keeping the npm major in sync avoids that portability trap.
RUN npm install -g npm@11
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Empty base URL => the SPA calls same-origin relative paths (/ask, /subject,
# /lucky, /api/...), which CloudFront routes to the ALB. See frontend/src/api.ts.
ENV VITE_API_BASE_URL=""
RUN npm run build

# ---------- Stage 2: python runtime ----------
# ARM64 base to match the cvc5 arm64 static binary and Fargate ARM.
FROM public.ecr.aws/docker/library/python:3.12-slim-bookworm AS runtime

# cvc5 (first-order theorem prover) static binary.
ARG CVC5_VERSION=1.3.1
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -L -o /tmp/cvc5.zip \
      "https://github.com/cvc5/cvc5/releases/download/cvc5-${CVC5_VERSION}/cvc5-Linux-arm64-static.zip" \
    && unzip /tmp/cvc5.zip -d /opt \
    && ln -s /opt/cvc5-Linux-arm64-static/bin/cvc5 /usr/local/bin/cvc5 \
    && rm /tmp/cvc5.zip \
    && cvc5 --version

# kiro-cli, which builds every theory (kiro_formalizer.py). Pinned; the
# installer verifies the archive's sha256 and picks the glibc or musl build.
# Q_INSTALL_GLOBAL=1 installs into /usr/local/bin. Auth is KIRO_API_KEY at
# runtime (headless mode); nothing secret is baked into the image.
ARG KIRO_CLI_VERSION=2.31.1
RUN curl -fsSL -o /tmp/kiro-install.sh https://cli.kiro.dev/install \
    && Q_INSTALL_GLOBAL=1 bash /tmp/kiro-install.sh \
         --version "${KIRO_CLI_VERSION}" --no-modify-path \
    && rm /tmp/kiro-install.sh \
    && kiro-cli --version

# chatbot_backend's run_cvc5() resolves the binary via CVC5_PATH, falling back
# to $HOME/bin/cvc5 (/root/bin/cvc5), which does not exist here. Point it at
# the installed binary.
ENV CVC5_PATH=/usr/local/bin/cvc5

WORKDIR /app

# Python deps. The root modules import a broad set (boto3, strands, nltk,
# bs4, numpy, langfuse, pyyaml, prompt_toolkit, colorama, requests, dotenv,
# fastapi, uvicorn); requirements.txt lists them with version bounds.
# Install the exact, hash-checked versions in requirements.lock (compiled from
# requirements.txt; see the comment at the top of that file), so every image
# build installs the same dependency tree the scans and tests saw.
COPY cdk/docker/requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.lock

# NLTK data used when (re)generating "I'm feeling lucky" questions.
RUN python -m nltk.downloader -d /usr/share/nltk_data punkt punkt_tab || true
ENV NLTK_DATA=/usr/share/nltk_data

# Copy the whole repo root (backend imports auto_formalizer/utils/globals/...
# from here). .dockerignore + the fromAsset `exclude` keep node_modules etc out.
COPY . /app

# globals.py raises at import time unless a runs/ dir exists. The inline job
# runner (store.py) writes per-subject sentence files under sentences/.
RUN mkdir -p /app/runs /app/sentences

# The Synthea sample FHIR bundles ship as a single ~9MB zip in git (committing
# the ~130MB of loose JSON would bloat the repo). Expand it at build time into
# the directory subjects.get_synthea_subjects() reads (Path.cwd()/sentences/
# synthea_sample_data_fhir_dstu2_latest), then drop the zip from the image.
# If the zip is absent the build still succeeds and subjects.py falls back to
# an empty Synthea list (its missing-dir guard).
RUN if [ -f /app/sentences/synthea_sample_data_fhir_dstu2_latest.zip ]; then \
        unzip -q -o /app/sentences/synthea_sample_data_fhir_dstu2_latest.zip \
            -d /app/sentences/synthea_sample_data_fhir_dstu2_latest \
        && rm -f /app/sentences/synthea_sample_data_fhir_dstu2_latest.zip; \
    fi

# Put the built SPA where our server serves it from.
COPY --from=frontend /build/build /app/frontend/build

# The web service entrypoint and its modules live in cdk/docker; place them
# beside the repo-root modules in /app, which is on the import path.
COPY cdk/docker/server.py /app/server.py
# server.py imports `auth` (Cognito app-level auth); it lives beside server.py
# in cdk/docker, so place it at /app/auth.py too (COPY . put it under
# /app/cdk/docker/, which is not on the import path).
COPY cdk/docker/auth.py /app/auth.py
# store.py (S3+DynamoDB theory/job store + inline runner) also imports from
# /app, so place it beside server.py like auth.py.
COPY cdk/docker/store.py /app/store.py
# job_introspection.py (build progress for the Job Introspection tab)
# is imported by store.py, so it sits beside it too.
COPY cdk/docker/job_introspection.py /app/job_introspection.py
# build_params.py (the Build parameters dialog's specs + validation) is
# imported by store.py and server.py.
COPY cdk/docker/build_params.py /app/build_params.py
# build_task.py is the one-shot Fargate builder entrypoint. It imports
# `store` from /app, so place it beside store.py; the builder task-def runs it
# via a `python build_task.py` command override.
COPY cdk/docker/build_task.py /app/build_task.py

ENV PYTHONUNBUFFERED=1

# Run as an unprivileged user (the web task and the builder task alike). The
# image's files stay root-owned and read-only to it; it owns only what the app
# writes: /app itself (new working files), runs/, sentences/ and theories/.
# kiro-cli gets a fresh HOME per build under the temp directory
# (kiro_formalizer), and /tmp is world-writable.
RUN useradd --system --uid 10001 --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
    && mkdir -p /app/runs /app/sentences /app/theories \
    && chown app:app /app /app/runs /app/sentences /app/theories
USER app

EXPOSE 8000

# For `docker run` and image scanners. ECS ignores an image's HEALTHCHECK: the
# web container's ECS health check (same command) is set in chatbot-stack.ts,
# and the builder task, which runs no server, has none.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
