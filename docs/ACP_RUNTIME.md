# Deploying Trinity's Generic ACP Runtime

Trinity's generic ACP runtime speaks Agent Client Protocol to a server supplied
by a derived agent image. The repository includes two pinned acceptance images:

| Image | Harness | Provider credential | Default model |
|---|---|---|---|
| `trinity-agent-base:acp-hermes` | Hermes Agent 0.19.0 | platform `GEMINI_API_KEY` or `GOOGLE_API_KEY` | `gemini-3.7-flash` |
| `trinity-agent-base:acp-deepseek` | pinned DeepSeek Harness commit | `DEEPSEEK_API_KEY` | `deepseek-v4-pro` |

## Build the images

```bash
./scripts/deploy/build-base-image.sh
docker build -t trinity-agent-base:acp-hermes docker/acp-harnesses/hermes
docker build -t trinity-agent-base:acp-deepseek docker/acp-harnesses/deepseek
```

Those names match Trinity's default `trinity-agent-base:*` image allowlist. The
hidden `local:test-acp-hermes` and `local:test-acp-deepseek` templates select the
correct image, runtime, model, and credential prompt. They are acceptance
fixtures, not user-facing starter agents.

## Credentials and models

Add both provider keys as repository secrets for the manual GitHub acceptance
run. For deployed Hermes agents, configure `GEMINI_API_KEY` or `GOOGLE_API_KEY`
in Trinity's platform environment; Trinity injects it into the pinned Hermes
image. DeepSeek agents declare `DEEPSEEK_API_KEY` as an agent credential for
Quick Inject / `.env`. Never bake a key into a Dockerfile, manifest, launcher,
template, or workflow argument.

`runtime.model` becomes `AGENT_RUNTIME_MODEL`; the ACP child also receives it as
`ACP_MODEL`. Hermes generates a key-free provider config automatically. Set
`ACP_PROVIDER` or `ACP_BASE_URL` only when intentionally using a Hermes-compatible
provider. Set `TRINITY_HERMES_AUTOCONFIG=0` to preserve an existing Hermes config.

## CI and live acceptance

Every pull request that changes ACP images or the base image builds both images
and checks the immutable manifest/launcher contract without secrets. To run live:

1. Open **Actions → acp-provider-smoke → Run workflow** in the fork.
2. Select the branch and enable **run_live**.
3. The workflow reads `GEMINI_API_KEY` and `DEEPSEEK_API_KEY` from repository
   Actions secrets and does not print keys or model responses.

## Current portable contract

Chat continuity, isolated headless turns, cancellation, provider model selection,
and common wall-clock guardrails are supported. Persisted Session-tab resume,
portable MCP configuration, image input, per-request allowed tools/max turns, and
portable cost reporting are deliberately unavailable. Unsupported restrictions
return an explicit error rather than silently widening execution.
