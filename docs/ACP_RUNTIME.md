# Deploying Trinity's Generic ACP Runtime

Trinity's generic ACP runtime speaks Agent Client Protocol to a server supplied
by a derived agent image. The repository includes two version-pinned acceptance
harness images:

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

The harness package/revision is pinned, but the images are not fully hermetic:
they build on the current `trinity-agent-base:latest`, Hermes transitive Python
dependencies are resolved at build time, and DeepSeek's Node bootstrap uses its
upstream distribution channel. Treat a successful exact-commit image build as
the reproducibility boundary; do not assume a future rebuild is byte-identical.

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
and checks the immutable manifest/launcher contract without secrets. The manual
button exists only after this workflow reaches the repository's default branch.
Before that, an owner push to `acp/**` or `feature/*-generic-acp-runtime` in a
trusted fork runs the live steps automatically and records the exact commit SHA.
To run manually once the workflow is on the default branch:

1. Open **Actions → acp-provider-smoke → Run workflow** in the fork.
2. Select the branch and enable **run_live**.
3. The workflow reads `GEMINI_API_KEY` and `DEEPSEEK_API_KEY` from repository
   Actions secrets and does not print keys or model responses.

Pull-request events run only the secretless image build and cannot consume
repository secrets. Provider-backed steps run only on an explicit manual request
or a push made by the repository owner to the bounded branch patterns above. Run
them only on a trusted ref in a repository you control, then attach that exact-SHA
Actions run to the proposal.

## Current portable contract

Chat continuity, isolated headless turns, protocol-first cancellation, provider
model selection, optional ACP context/cost telemetry, JSON-RPC batches, and common
wall-clock guardrails are supported. Persisted Session-tab resume, portable MCP
configuration, image input, per-request allowed tools/max turns, and guaranteed
cost reporting are deliberately unavailable. Unsupported restrictions return an
explicit error rather than silently widening execution.

Only `stopReason=end_turn` is successful; limit stops and refusals fail the turn,
`cancelled` stays cancellation, and missing/unknown reasons are protocol errors.
The current interoperability baseline is ACP protocol v1 with provider
credentials supplied through the process environment. Agents that select another
protocol version are rejected explicitly. Advertised login choices are allowed
when the injected credential is already active; a session that actually requires
an interactive ACP login fails explicitly because this headless runtime cannot
complete that flow. Trinity advertises `auth.terminal=false` and rejects terminal
auth methods. It does not advertise reverse filesystem or terminal callbacks;
the agent's own tools operate on the mounted workspace instead.
Hermes normally advertises an interactive setup method for registry clients;
the Trinity acceptance image removes that method and retains its environment-
backed provider method because this runtime has no interactive auth terminal.
The image also normalizes Hermes 0.19.0's null response on a confirmed interrupt,
allowing the pinned adapter to finish its own `stopReason=cancelled` response.
This is a deliberately bounded v1 client rather than a claim of complete ACP
surface support. Trinity keeps the transport at its process, cancellation,
sanitization, and activity seams; any newly advertised ACP capability needs a
handler and non-happy-path coverage first.
