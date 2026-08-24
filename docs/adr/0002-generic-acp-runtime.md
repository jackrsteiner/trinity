# ADR 0002: Generic ACP runtime boundary

- **Status:** Accepted
- **Date:** 2026-08-24
- **Requirement:** [ACP-001](../memory/requirements/runtimes.md#acp-001--generic-agent-client-protocol-runtime)

## Context

Trinity's `AgentRuntime` interface currently has harness-specific adapters for
Claude Code, Gemini CLI, and Codex. The Agent Client Protocol (ACP) provides a
portable client/agent lifecycle, typed protocol models, capability negotiation,
streamed session updates, permission requests, and cancellation. Supporting ACP
can make any conforming harness usable without adding another harness-specific
runtime implementation.

ACP is an interoperability protocol. It does not describe Trinity's container
security posture, its persistence model, its pricing catalog, or all of its UI
features. Treating absent ACP features as if they existed would create false
security and reliability guarantees.

## Decision

### 1. Three layers, with one-way dependencies

1. **Generic protocol adapter:** `ACPRuntime` is an ACP client built on the
   official `agent-client-protocol` Python SDK. The SDK owns protocol models,
   JSON-RPC transport, message framing, and protocol/version negotiation.
   `ACPRuntime` owns only lifecycle orchestration and translation of standard
   ACP events into Trinity's runtime-neutral execution result types.
2. **Trinity integration and presentation:** the runtime factory, agent-server
   routes, backend services, and UI consume the negotiated capability snapshot.
   They disable, hide, or label unavailable features. They do not ask
   `ACPRuntime` to imitate a feature the agent did not negotiate.
3. **External runtime and security layer:** image construction, process launch
   configuration, the container boundary, filesystem/mount policy, credential
   injection, UID selection, and network policy remain outside ACP and outside
   the protocol adapter.

Dependencies point downward: Trinity integration may consume the protocol
adapter; the adapter may consume the official SDK; neither may import or encode
knowledge of a particular ACP agent.

### 2. Harness neutrality is a hard invariant

`ACPRuntime` MUST NOT contain Hermes-specific, DeepSeek-specific,
provider-specific, executable-name-specific, model-name-specific, or
credential-name-specific behavior. It MUST NOT branch on the ACP agent command,
its arguments, implementation name, provider, or environment variables to
repair a harness. Agent launch command/arguments are generic external
configuration passed into the adapter as data.

Interoperability fixes belong in the ACP agent, its packaging, or the protocol
SDK/specification. A new conforming ACP harness must require configuration and
packaging only, not a code change to `ACPRuntime`.

### 3. No semantic emulation

The adapter MUST NOT emulate unsupported or non-portable Trinity semantics.
Specifically, it must not synthesize session loading/resume, model selection,
cost telemetry, MCP availability, context windows, token usage, tool controls,
or Trinity-specific session persistence when ACP does not advertise or carry
that information.

Missing data remains missing. Callers expose that state as unavailable or
unknown. A conservative capability default is required for every field.

### 4. Capability-driven lifecycle

The runtime uses the ACP lifecycle in this order:

1. spawn the configured agent through the SDK's stdio process helper;
2. `initialize` using the SDK protocol version and validate the negotiated
   version;
3. derive an immutable Trinity capability snapshot from the SDK
   `AgentCapabilities` response;
4. use `session/new`, or `session/load` only when `loadSession` was advertised;
5. send `session/prompt` and consume typed streamed `session/update` events;
6. answer `session/request_permission` with the operator/policy decision made by
   Trinity integration (default deny when no decision channel is available);
7. send `session/cancel` for cancellation; and
8. close the ACP connection, then terminate and, if necessary, kill the child
   process within bounded grace periods.

Protocol errors, incompatible versions, process exits, and malformed SDK-model
payloads surface as explicit runtime failures. They are not converted into a
successful empty response.

### 5. Capability mapping

Negotiated ACP capabilities are authoritative for ACP sessions:

| Trinity capability | ACP source | Behavior when absent |
|---|---|---|
| Chat continuity | live ACP session | disabled after connection loss |
| Session load/resume | `agentCapabilities.loadSession` | no load call; Session resume unavailable |
| MCP | advertised ACP MCP capabilities | no MCP servers passed; MCP shown unavailable |
| Prompt images/audio/resources | ACP prompt capabilities | reject that content type |
| Model selection | no portable ACP v1 capability | unavailable; model overrides rejected |
| Cost telemetry | no portable ACP v1 field | unknown, never estimated |
| Usage telemetry | typed `usage_update` when emitted | unknown until emitted |

The raw negotiated ACP capability document and the normalized Trinity snapshot
are exposed for diagnostics and UI gating. Unknown future SDK fields may be
reported as data but must not silently enable a Trinity feature.

### 6. Permission requests are not sandbox decisions

ACP permission requests are user-interaction signals. They may pause a prompt
and inform an operator decision, but they are not Trinity's security boundary.
Approval does not grant container privileges and denial is not a substitute for
enforcement.

Container capabilities, filesystem access, bind mounts, credential scope, UID,
network egress, and other mandatory policy controls remain enforced outside ACP
regardless of the permission response. The protocol adapter never widens them.

### 7. Configuration and process ownership

Trinity passes an executable plus an argument vector without a shell. The
backend validates and transports this generic launch configuration; derived
images install the chosen ACP agent. Secrets use Trinity's existing credential
injection path and are inherited by the agent process without being copied into
logs or command arguments.

Each headless execution owns an ACP process and session so concurrent tasks do
not share mutable protocol state. Interactive chat may keep one process/session
alive for continuity and must close it on session reset or server shutdown.

## Consequences

- Supporting another conforming ACP harness is a packaging/configuration task.
- Some Trinity controls are intentionally unavailable for ACP agents until ACP
  standardizes and the agent advertises them.
- ACP permission UX improves transparency but does not alter mandatory policy.
- Protocol tests can use a conforming SDK-backed mock agent; real Hermes and
  DeepSeek checks remain opt-in interoperability smoke tests.
