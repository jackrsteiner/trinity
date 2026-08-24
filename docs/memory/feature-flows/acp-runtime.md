# Feature: Generic ACP Runtime (ACP-001)

Decision record: [ADR 0002 — Generic ACP runtime boundary](../../adr/0002-generic-acp-runtime.md).

## Overview

The fourth `AgentRuntime` is not another harness adapter — it is **one
harness-neutral Agent Client Protocol client** built on the official
`agent-client-protocol` Python SDK (exact-pinned `0.12.1` in the base image and
`tests/requirements-test.txt`; keep the two aligned). Any conforming ACP agent
runs through it: an agent's template declares
`runtime: { type: acp, command: <executable>, args: [...] }`, the backend bakes
`AGENT_RUNTIME=acp` + `AGENT_RUNTIME_COMMAND` + `AGENT_RUNTIME_ARGS` (a JSON
string array — shell-free by construction), and `acp_runtime.py` speaks the
protocol. Supporting a new conforming harness is packaging/configuration, never
a code change to the adapter (ADR 0002 §2 — no harness, provider,
executable-name, model-name, or credential-name branches).

The organizing rule is **refuse, don't emulate**: a Trinity feature the agent
did not negotiate is disabled, labeled, or refused with a typed error — never
synthesized. Missing data (cost, usage) stays missing.

## Flow: dispatch → protocol session → typed result

1. **Create** — `crud.create_agent_internal` finalizes the runtime after
   template resolution, then `helpers.validate_acp_launch` rejects an
   unbuildable envelope as named 400s: `acp_runtime_command_required` (an
   ACP agent with no command would otherwise boot a container whose runtime
   can never construct), `acp_runtime_model_unsupported` (ACP v1 has no
   portable model selection — every turn would fail at execution time),
   `acp_runtime_args_invalid` (list-of-str, ≤128, no NULs — mirroring the
   agent-side `ACPLaunchConfig` bounds so a config that passes creation cannot
   fail the in-container parse), and `acp_launch_config_wrong_runtime` (a
   command/args block on a CLI runtime would be silently ignored).
2. **Launch** — `acp_launch.load_acp_launch_config` re-validates in-container;
   `spawn_agent_process` (SDK) starts the child with the per-spawn
   `build_execution_env` environment. No shell is ever involved.
3. **Negotiate** — `initialize` with the SDK protocol version; a version
   mismatch or absent capability block is a hard failure. The negotiated
   `AgentCapabilities` derive the Trinity snapshot (`negotiated=True`,
   `session_load` iff `loadSession` advertised, prompt image/audio flags,
   `cost_reporting="unavailable"`); the raw document is exposed for
   diagnostics via `GET /api/runtime/capabilities` and `/health`.
4. **Session** — `session/new`, or `session/load` **only** when advertised
   (`ACPFeatureUnavailable` otherwise). Each headless task owns its own
   process+session; interactive chat keeps one live pair for continuity
   (`/api/chat`'s execution lock serializes turns onto it).
5. **Prompt** — `session/prompt` with typed streamed `session/update` events:
   agent text chunks → response; tool start/progress → `ExecutionLogEntry`;
   `usage_update` → context gauges and (when currency is USD) real cost. Every
   event is credential-sanitized and published to the process registry's live
   log stream.
6. **Terminal** — `stop_reason` maps to metadata: `cancelled`/`refusal` →
   `status="error"`/`AGENT_ERROR`; budget stops (`max_tokens`,
   `max_turn_requests`) keep their partial text as `success`, matching how the
   CLI runtimes report truncation.

## Capability gating (both ends)

- **Backend**: `gate_system_prompt(runtime, prompt)` disables the platform
  system prompt for ACP at **both** dispatch layers —
  `task_execution_service.execute_task` (the `/task` path) **and**
  `chat_execution_service.run_chat_turn` (the sync `/chat` path, which is what
  MCP `chat_with_agent`'s default sequential mode uses). Gating only one layer
  is the bug class this flow exists to prevent: the runtime rejects an
  ungated prompt with a 409 on every turn.
  `tests/unit/test_acp_feature_gating.py` pins both call sites at source
  level. `"acp"` is in `RUNTIMES_WITHOUT_SESSION_TAB_RESUME`
  (`session_turn_service`) so the resumable-turn engine never passes
  `resume_session_id` to an ACP agent — its cold retry only fires on the
  Claude JSONL-missing error, so an ACP 409 would permanently break the
  Workspace thread. ACP agents also skip Claude-subscription auto-assign
  (`is_claude_runtime`).
- **Agent server**: `/api/chat/session`, `GET/PUT /api/model` and `/health`
  read `get_capabilities_snapshot()` — **fail-open** (a misconfigured runtime
  degrades the capability block to permissive legacy defaults instead of
  500ing `/health`, which monitoring, the dispatch breaker and readiness
  gates consume). `get_runtime()` keeps its fail-loud contract on execution
  paths only.
- **Frontend**: `ChatPanel.vue` fetches
  `GET /api/agents/{name}/runtime/capabilities` and hides the model selector /
  stops sending `model` and `resume_session_id` when the runtime lacks them.

## Safety posture

- **Read-only mode fails CLOSED.** Claude enforces read-only via hooks, Codex
  via its sandbox; ACP has no portable channel, so an enabled
  `~/.trinity/read-only-config.json` **refuses the turn** (409) rather than
  running unenforced behind an active-looking toggle. The
  unreadable/corrupt-config case stays fail-open with a WARNING — loader
  parity with Codex (`_is_read_only`), where diverging directions across
  runtimes was a CSO finding.
- **Guardrails are not wired** (no portable tool-control channel); combined
  with the platform-prompt gate this means an ACP agent runs with container
  isolation as the boundary — which is Trinity's actual security boundary
  (ADR 0002 §6). Nothing is presented as enforced that isn't.
- **Permissions**: `session/request_permission` default-denies by selecting
  the agent's own reject-kind option (`reject_once`/`reject_always`) when one
  is offered — `DeniedOutcome("cancelled")` is the last resort, since some
  agents read it as a whole-turn cancellation. A `permission_resolver`
  callback is the integration seam for a real operator decision channel
  (future operator-queue wiring).
- **Credential hygiene**: all streamed events and stderr pass
  `credential_sanitizer` before persistence or publication; stderr capture is
  capped at 64 KiB but **keeps draining** past the cap (discarding) so a
  chatty agent can never wedge on pipe backpressure.

## Error → HTTP mapping

| Condition | HTTP | Why |
|---|---|---|
| Unadvertised/unsupported feature (`ACPFeatureUnavailable`), read-only refusal | **409** | Legible refusal; never mistaken for an infra fault |
| Protocol error, agent `RequestError`, process/spawn failure, malformed SDK payload | **502** | Backend classifies AGENT_ERROR (the Codex pipe-drop precedent) |
| Prompt timeout | **504** | Existing timeout semantics; protocol cancel is attempted first |

Deliberately **never 503/429**: the backend reads those as AUTH/rate signals
(dispatch breaker D10, SUB-003), and a harness-neutral adapter has no portable
way to prove an auth failure. Known limitation: an ACP agent's provider auth
failure therefore lands as AGENT_ERROR, not AUTH — the breaker's auth arm does
not protect ACP agents.

## Cancellation & lifecycle

`cancel_execution` delivers `session/cancel` bounded by a grace timeout; an
undeliverable cancel returns `False` so the terminate endpoint falls back to
the process registry's SIGINT/SIGKILL path (which also covers a wedged
connection). `mark_terminated` records protocol-native cancellation so the
#679 cancel-relabel logic still applies. `DELETE /api/chat/history` calls
`runtime.reset_chat()` — the live chat process holds the full prior context,
so a transcript reset without it would silently carry that context into the
"new" chat (ADR 0002 §7). Server shutdown closes all sessions (guarded — a
misconfigured runtime must not fail shutdown).

## Known limitations

- **`local:` templates / API config only**: the `github:` catalog path has
  never populated `template_data`, so a `github:` template's
  `runtime.command/args` block is not read. Covered paths: `local:` resolve,
  `deploy_local_agent_logic`, `recreate_missing_container`.
- Config-drift recreate preserves the baked launch env but has no
  `check_*_matches` predicate for it — changing a template's ACP command needs
  a manual recreate.
- Cost is never estimated; executions of agents whose ACP agent emits no
  usage/cost show unknown cost by design.
- Remote (non-stdio) ACP transports are out of scope for this slice.

## Key files

| File | Role |
|---|---|
| `docker/base-image/agent_server/services/acp_runtime.py` | The protocol adapter (lifecycle, collector, error map, read-only refusal) |
| `docker/base-image/agent_server/services/acp_launch.py` | Launch envelope parse/validation (agent side) |
| `docker/base-image/agent_server/services/runtime_adapter.py` | ABC + factory + `get_capabilities_snapshot()` |
| `src/backend/services/agent_service/helpers.py` | `validate_acp_launch` (create-time named 400s) |
| `src/backend/services/task_execution_service.py` / `chat_execution_service.py` | `gate_system_prompt` at both dispatch layers |
| `src/backend/services/session_turn_service.py` | `RUNTIMES_WITHOUT_SESSION_TAB_RESUME` |
| `tests/fixtures/acp_conforming_agent.py` | SDK-backed conforming agent for deterministic protocol tests |
| `tests/unit/test_acp_*.py` | Protocol, launch-config, gating, validation suites |
| `docs/runtimes/acp-interoperability.md` | Opt-in live Hermes/DeepSeek smoke instructions + honest recorded results |
