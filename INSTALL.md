# Installation and local use

This covers building a theory and chatting with it from the command line on a
Linux machine. To deploy the web app, see [Deploying to AWS](README.md#deploying-to-aws).

## Install cvc5

```bash
cd ~
# pick the build for your CPU:
wget https://github.com/cvc5/cvc5/releases/download/cvc5-1.3.1/cvc5-Linux-x86_64-static.zip
wget https://github.com/cvc5/cvc5/releases/download/cvc5-1.3.1/cvc5-Linux-arm64-static.zip
sudo apt install unzip   # on Ubuntu
unzip cvc5-Linux-*-static.zip
mkdir -p ~/bin
ln -s ~/cvc5-Linux-*-static/bin/cvc5 ~/bin/cvc5
~/bin/cvc5 --version
```

The code looks for cvc5 at `$CVC5_PATH`, falling back to `~/bin/cvc5`.

## Install kiro-cli

Theories are built by a kiro-cli agent session. Install kiro-cli and either log
in (`kiro-cli login`) or export a Kiro API key as `KIRO_API_KEY` for headless use:

```bash
curl -fsSL https://cli.kiro.dev/install | bash
kiro-cli --version
```

The deployed builder pins kiro-cli 2.31.1 (see `cdk/docker/Dockerfile`).

## Set up the Python environment

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # then restart your shell
cd sample-provably-correct-chatbot-using-first-order-logic
uv python install 3.12
uv sync
mkdir -p runs sentences
```

The LLM steps call Amazon Bedrock, so the shell needs AWS credentials (for
example `export AWS_PROFILE=$PROFILE`) with model access to the model IDs in
`globals.py`. Tracing goes to Langfuse only when its keys are set in `.env`;
set `DISABLE_LANGFUSE=1` to turn it off.

## Build a theory

1. Create the sentences for a subject defined in `subjects.py`. For a
   Wikipedia-based subject this downloads and splits the page and computes the
   standalone rewrites:

   ```bash
   uv run scripts/create-sentences.py --subject-id diabetes
   ```

2. Build one theory over all of its sentences:

   ```bash
   uv run scripts/start-new-run.py --subject-id diabetes
   ```

   `--max-sentences N` formalizes only the first N sentences (a quick, cheap
   trial). `--qas-per-sentence`, `--ncar-ratio`, `--kiro-model`,
   `--kiro-timeout-seconds` and `--kiro-max-attempts` match the web app's
   Build parameters dialog; `--help` lists them all. The script writes
   `runs/run-<subject>-<NNNN>.yaml` and exits non-zero when no verified
   theory was found.

   Set `CHATBOT_KIRO_KEEP_WORKDIR=1` to keep the agent's workspace (its
   `theory.smt2`, fragments and `answers.json`) for inspection; it is under
   `$CHATBOT_KIRO_WORKDIR` (default: the system temp dir) in `chatbot-kiro/`.

## Chat with it

```bash
uv run scripts/chatbot.py --run-file run-diabetes-0000.yaml
```

A bare name resolves under `runs/`. The command-line chatbot keeps the
conversation in an Amazon Bedrock AgentCore memory, so set
`CHATBOT_AGENTCORE_MEMORY_ID` to the id of one in your account first. `scripts/compute-coverage.py --run-file ...`
asks freshly generated questions and reports how many the theory answers.

## Run the web app locally

The web app keeps its theories in S3 and DynamoDB, so running it locally needs
a deployed stack's store: set `THEORY_STORE_BUCKET`, `THEORIES_TABLE` and
`JOBS_TABLE` to the stack's resources. Without the builder settings
(`BUILDER_TASK_DEF`, ...) a build cannot start. Without the Cognito settings
the app runs unauthenticated, unless `AUTH_REQUIRED=1` is set (the deployed
stack sets it), in which case it refuses to start. In two terminals:

```bash
# 1. the API, from the repo root
cp cdk/docker/{server,auth,store,job_introspection,build_params,build_task}.py .
# The dev server below is another origin, so allow it (the deployed app is
# same-origin and enables no CORS).
CORS_ALLOW_ORIGINS=http://localhost:3000 uv run uvicorn server:app --port 8000
```

```bash
# 2. the React dev server (needs npm 11 for its lockfile)
cd frontend
npx -y npm@11 ci
npm start
```

## Tests

```bash
uv run pytest tests                              # formalizer, kiro engine, process cleanup
(cd cdk/docker && uv run pytest tests)           # web service, builder, introspection
(cd frontend && CI=true npm test -- --watchAll=false)
```

The web-service tests need its extra dependencies:
`uv pip install fastapi httpx itsdangerous 'PyJWT[crypto]'`.
None of the tests call Bedrock, kiro-cli or cvc5.
