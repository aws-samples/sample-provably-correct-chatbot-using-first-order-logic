# CDK deployment

Deploys the chatbot (FastAPI backend + React frontend + kiro-cli theory
builder) to your AWS account as one stack, `ProvablyCorrectChatbotStack`,
reachable over HTTPS and gated by its own Cognito user pool. The deploy steps,
prerequisites and troubleshooting are in the main README:
[Deploying to AWS](../README.md#deploying-to-aws).

## What the stack creates

```
  Internet ──▶ CloudFront (HTTPS; caches /static/*)
                  │  /ask /ask-multi /subject /lucky /lucky-questions /api/* /auth/* uncached
                  ▼
                 ALB (HTTP :80, public subnets)
                  ▼
   Web task: Fargate ARM64, 2 vCPU / 8 GB ── ecs:RunTask ──▶ Builder task (one per build)
   • uvicorn server:app (API + SPA)                          Fargate ARM64, 8 vCPU / 48 GB
   • cvc5 answers questions                                  • python build_task.py
   • Cognito session-cookie auth                             • test set + ONE kiro-cli session
          │                                                         │
          ▼ task roles (no static credentials)                      ▼
   Bedrock · S3 theory store · DynamoDB · CloudWatch Logs    + Secrets Manager (Kiro API key)
```

- A 2-AZ VPC with one NAT gateway, an ECS cluster, the web service behind an
  ALB, and the builder task definition.
- A Cognito user pool with Hosted UI domain `provably-correct-chatbot-<account>`
  and a confidential app client.
- An S3 bucket (source documents, theories, build progress) and two DynamoDB
  tables (theories, jobs).
- Log groups for the web task (`chatbot/...` streams) and the builder
  (`chatbot-builder/builder/<task id>`).

## Context values

| Context key            | Default                                  | Effect |
|------------------------|------------------------------------------|--------|
| `kiroApiKeySecretName` | `provably-correct-chatbot/kiro-api-key`  | Secrets Manager secret the builder reads the Kiro API key from |
| `kiroModel`            | (empty: kiro-cli's default)              | kiro-cli `--model` for builds, and the Build parameters dialog's default |

For example: `npx cdk deploy ProvablyCorrectChatbotStack -c kiroModel=claude-sonnet-4.5`.

## The image

`docker/Dockerfile` is built from the **repo root**, because the backend
imports the root modules (`auto_formalizer`, `kiro_formalizer`, `utils`,
`globals`, `params`, `chatbot_backend`). It builds the React SPA, installs the
cvc5 arm64 static binary, a pinned kiro-cli and the Python dependencies, and
places the web-service modules (`docker/*.py`) beside the root modules in
`/app`. The web task runs `uvicorn server:app`; the builder runs
`python build_task.py`.

`cdk.out` must stay outside the asset context's excluded paths: the asset root
is the repo root, and `cdk/cdk.out` is excluded, so deploy from `cdk/` (the
default) or pass `--output` to a directory outside the repo.

## Deploying from CodeBuild

`buildspec.deploy.yml` builds the ARM64 image with buildx (QEMU emulation) and
runs `cdk deploy ProvablyCorrectChatbotStack`. Use a CodeBuild project of its
own for this repository (`aws/codebuild/standard:7.0`, privileged mode), whose
service role can assume the account's CDK bootstrap roles.

## Python dependencies

`docker/requirements.txt` lists the runtime dependencies without pinned
versions. Pin them for reproducible images.
