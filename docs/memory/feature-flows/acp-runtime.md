# Generic ACP Runtime

## Overview

`runtime: { type: acp }` lets Trinity drive a compatible Agent Client Protocol
server without a harness-specific runtime class. Hermes and DeepSeek Harness are
pinned acceptance images. The common runtime owns JSON-RPC transport, lifecycle,
Trinity metadata, cancellation, sanitization, and conservative capabilities.

## User Story

As a Trinity operator, I can build or select a trusted ACP-derived agent image,
inject that harness's provider credential, and use ordinary Chat and headless task
surfaces without teaching Trinity about the provider implementation.

## Entry Points

- `POST /api/chat` keeps a process/session for conversational continuity.
- `POST /api/task` creates an isolated process/session per execution.
- `GET /api/model` and `PUT /api/model` report or update the provider model id.
- `GET /api/runtime/capabilities` reports the common-denominator contract.
- `DELETE /api/chat/history` terminates and resets the retained ACP session.

## Frontend Layer

`RuntimeBadge.vue` labels ACP agents explicitly. `AgentTerminal.vue` opens a
diagnostic shell because ACP is a protocol server, not an interactive CLI; it
must never fall through to Claude Code. Model display defaults to
`acp-provider-default` until the template or operator supplies a provider id.

## Backend Layer

The backend accepts `acp` as a non-Claude runtime, passes `AGENT_RUNTIME_MODEL`,
and composes an ACP-specific platform prompt with MCP-only sections removed.
Generic ACP advertises `mcp_support=false`, so neither Trinity MCP injection nor
template MCP configuration may fall through to Claude's configuration path.

Inside the agent image, `ACPRuntime` opens a root-owned manifest and launcher,
sends `initialize` and `session/new`, then exchanges `session/prompt` and
`session/update` messages over newline-delimited JSON-RPC stdio. Stdout is
protocol-only; harness diagnostics belong on stderr. `ACP_MODEL` is supplied to
the child process for each selected model. Trinity's baseline accepts ACP v1
with environment-provided credentials: a different negotiated version or
malformed authentication/capability metadata fails initialization. An agent may
still advertise login choices while an injected key is already active; Trinity
continues to `session/new`, and maps an actual `auth_required` response to the
provider-auth error. The client advertises reverse filesystem and terminal
methods as unsupported because it does not implement those server-to-client RPCs.

## Side Effects

- Chat retains one subprocess until reset, model change, failure, or shutdown.
- Headless tasks register their process group by execution id for cancellation.
- Hermes writes a key-free provider configuration under `~/.hermes`; credentials
  remain environment variables.
- The DeepSeek launcher maps Trinity read-only mode to
  `DSH_PERMISSION_MODE=read-only` and stores harness sessions in the agent home.

## Error Handling

Provider rate limits map to HTTP 429, authentication failures to 503, timeouts to
504, protocol pipe failures to 502, unsupported portable restrictions to 422,
and other execution failures to 500. A cancelled ACP stop reason is surfaced as
a cancelled request rather than success. Messages pass through Trinity's
credential sanitizer before logs, responses, or metadata. Any request or framing
failure drops the retained process—even if it is still alive—so the next chat
establishes a clean session instead of reading a desynchronized stream.

## Security Considerations

The derived image supplies `/opt/trinity/acp/runtime.json` (root:root, `0444`) and
`/opt/trinity/acp/launch` (root:root, `0555`). Trinity opens without following
symlinks, bounds the manifest to 64 KiB, validates the same file descriptor it
reads, and rejects writable or non-root-owned pathname parents and files.

ACP reverse permission requests obey the immutable manifest policy and prefer a
one-time grant over a durable grant. Read-only is
accepted only when the manifest declares harness enforcement. Generic ACP cannot
portably enforce `allowed_tools`, request-level `max_turns`, image input, persisted
Session-tab resume, or MCP; requests for those features fail closed. Common
wall-clock guardrails are enforced, while unmappable tool and turn controls are
logged explicitly. Credentials never belong in manifests, images, or CI logs.

## Testing

Unit tests cover manifest trust, protocol negotiation and advertised capabilities,
protocol lifecycle/recovery, permission responses, progressive tool event
translation, cancellation stop reasons, transcript bounds, model propagation,
status mapping, prompt/MCP gating, template selection, and Hermes configuration.
Pull requests build both pinned images and
verify immutable files without provider secrets. A manually dispatched workflow
with `run_live=true` performs provider-backed inference, tool use, continuity,
parallel isolation, read-only behavior, and cancellation for Hermes/Gemini and
DeepSeek Harness.

## Related Flows

- [ACP deployment guide](../../ACP_RUNTIME.md)
- [OpenAI Codex runtime](codex-runtime.md)
- [Harness authoring guide](harness-authoring-guide.md)
- [Execution termination](execution-termination.md)
- [Credential injection](credential-injection.md)
