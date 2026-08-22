# Generic ACP runtime

## Purpose

`runtime: { type: acp }` lets Trinity drive any compatible Agent Client Protocol server without adding harness-specific branches to the agent server. Hermes and DeepSeek Harness are acceptance images; neither has a bespoke Trinity runtime class.

## Trust boundary

The derived image supplies:

- `/opt/trinity/acp/runtime.json`, mode `0444`, owned by root;
- `/opt/trinity/acp/launch`, mode `0555`, owned by root.

The generic runtime opens both without following symlinks and refuses a non-regular, non-root-owned, or group/world-writable file. The manifest contains only the immutable launcher command, working directory, permission policy, and whether the harness can enforce Trinity read-only mode. Credentials remain runtime environment variables; they never belong in the manifest or image layers.

## Execution flow

```text
Trinity HTTP API
  -> AgentRuntime factory
  -> ACPRuntime
  -> root-owned launcher
  -> harness ACP server over JSON-RPC stdio
  -> model provider
```

Startup sends `initialize` followed by `session/new`. A turn sends `session/prompt`; streamed `session/update` notifications become response text and, when the harness exposes ACP tool events, Trinity tool-use/tool-result activity. Stdout is exclusively protocol traffic. Harness diagnostics belong on stderr.

The acceptance harnesses intentionally differ at that optional presentation boundary. Hermes exposes tool start/completion updates, which Trinity translates into its execution log. The pinned DeepSeek Harness automation transport publishes only committed assistant chunks and retains tool trace in its own session log. DeepSeek executions therefore report `tool_count: 0` even when a provider-side tool ran; live acceptance verifies the requested filesystem effect directly instead of inventing events the harness did not send.

## Lifecycle

- Ordinary `/api/chat` turns retain one ACP subprocess and session so continuity works without protocol-level session reload.
- `DELETE /api/chat/history` calls the runtime reset hook, terminates that subprocess, and drops its ACP session.
- Every headless task gets its own subprocess and ACP session. The process registry associates it with the Trinity execution ID so cancellation signals the correct process group.
- Persisted Session-tab resume is not advertised. DeepSeek's acceptance server creates fresh sessions, so the common-denominator runtime does not claim `session/load` behavior.

## Authority and fail-closed behavior

ACP `session/request_permission` is reverse JSON-RPC. The trusted manifest chooses `allow` or `reject`; model text cannot change that policy.

Read-only mode is accepted only when the manifest declares harness enforcement. Trinity passes `TRINITY_READ_ONLY=1` to that launcher. The DeepSeek launcher translates it to `DSH_PERMISSION_MODE=read-only`; the Hermes image declares read-only unsupported, so execution is rejected before a provider call.

The common runtime does not claim request-level mappings for `allowed_tools`, `max_turns`, image input, or persisted resume. A caller that requests one of those restrictions gets an explicit error rather than a silently broadened execution.

## Declared capabilities

| Capability | ACP value |
|---|---|
| Chat continuity | yes |
| Session-tab persisted resume | no |
| MCP configuration | no |
| Cost reporting | unavailable |

Harness-specific installation, model selection, and security translation stay in `docker/acp-harnesses/<name>/`.

## Cost telemetry

ACP does not provide a portable monetary-cost field, and the generic runtime cannot safely infer provider pricing from harness-specific events. `cost_reporting` is therefore `unavailable` and `ExecutionMetadata.cost_usd` remains `null`. Consumers must distinguish this from a real zero-dollar execution; provider billing remains authoritative until ACP standardizes trustworthy usage or cost telemetry.
