# ACP interoperability smoke tests

Trinity's live smoke test launches any ACP agent through the same generic
`ACPRuntime`. Agent-specific commands and credentials belong to the external
installation and test environment, never to the runtime adapter.

The test makes one real model request and may incur provider cost. It is skipped
unless the matching opt-in variable is exactly `1`.

## Hermes ACP

Prerequisites:

1. Install Hermes Agent using its normal installer.
2. Install the ACP extra:

   ```bash
   cd ~/.hermes/hermes-agent
   uv pip install -e '.[acp]'
   ```

3. Configure a model/provider with `hermes model` and verify the ACP install
   with `hermes acp --check`.
4. Ensure `hermes` and the configured credential files are available to the
   user running pytest.

Run:

```bash
TRINITY_ACP_INTEROP_HERMES=1 \
TRINITY_ACP_HERMES_COMMAND=hermes \
TRINITY_ACP_HERMES_ARGS='["acp"]' \
/tmp/trinity-acp-venv/bin/python -m pytest -q \
  tests/unit/test_acp_interop.py -k hermes
```

Hermes' upstream ACP documentation also lists `hermes-acp` and
`python -m acp_adapter` as equivalent launchers.

## DeepSeek Harness ACP

The official in-repository automation server currently runs from a DeepSeek
Harness checkout. It requires the runnable ACP composition to have an exact
provider and model configured.

Prerequisites:

1. Clone and install the official `deepseek-ai/deepseek-harness` repository as
   documented upstream, including pnpm dependencies.
2. Configure the ACP demo composition with a provider, model, and corresponding
   provider credential.
3. Confirm this command starts the stdio server from that checkout:

   ```bash
   pnpm run demo:acp
   ```

Run from the Trinity checkout, pointing `CWD` at the Harness checkout:

```bash
TRINITY_ACP_INTEROP_DEEPSEEK=1 \
TRINITY_ACP_DEEPSEEK_COMMAND=pnpm \
TRINITY_ACP_DEEPSEEK_ARGS='["run","demo:acp"]' \
TRINITY_ACP_DEEPSEEK_CWD=/path/to/deepseek-harness \
/tmp/trinity-acp-venv/bin/python -m pytest -q \
  tests/unit/test_acp_interop.py -k deepseek
```

The official DeepSeek adapter advertises fresh sessions only at the time of
writing; load/resume and non-empty MCP server lists are documented upstream as
unsupported. Trinity therefore follows negotiation and does not synthesize
either behavior.

## Result recorded for this implementation

On 2026-08-24, neither Hermes ACP nor DeepSeek Harness ACP and their model
credentials were installed in the implementation environment. Both live tests
were collected and skipped by their explicit opt-in gates. No interoperability
pass is claimed. The official-SDK conforming agent suite was run separately and
is not a substitute for these live results.

## Provider image acceptance in GitHub Actions

The stacked provider-image workflow is
`.github/workflows/acp-provider-image-smoke.yml`. It builds the Trinity agent
base image and then two derived images. Harness-specific installation and
launch settings stay under `tests/interop/images/`; neither image changes
`ACPRuntime`.

The workflow pins upstream source rather than following moving default
branches:

- Hermes Agent: `91e867631e9d2eb9fbd69edd4459475d38070979`
- DeepSeek Harness: `b150a551b8d465e31e418e1b2eaf5e79bbb7d28e`

Hermes is installed in `/opt/hermes-venv` because its ACP extra pins SDK 0.9.0
while Trinity's ACP client pins SDK 0.12.1. Process isolation preserves both
official dependency sets. The Hermes image uses the Gemini provider with a
non-secret `config.yaml`; `GEMINI_API_KEY` is injected only when the container
runs. The DeepSeek image launches the official in-repository `demo:acp` server
and receives `DEEPSEEK_API_KEY` only at runtime.

Required GitHub Actions repository secrets:

- `GEMINI_API_KEY`
- `DEEPSEEK_API_KEY`

The credentialed runner makes real provider calls and fails unless it observes
all of the following:

1. initialization and capability negotiation;
2. `session/new`, a non-empty streamed answer, and clean turn metadata;
3. `session/load` only when advertised, or an unavailable error when absent;
4. an ACP permission request plus Trinity's one-shot rejection response;
5. delivery and settlement of ACP cancellation; and
6. clean runtime/process shutdown.

The containers are disposable, read-only except for explicit tmpfs work/state
paths, and receive credentials only as environment variables at run time. ACP
permission handling remains an interaction mechanism, not the security
boundary.

Run it from the Actions tab with **ACP provider image smoke → Run workflow**, or
push a change to `test/acp-provider-image-smoke`. Missing credentials are hard
failures; this workflow never reports a skipped live pass.

### Recorded provider-image result

GitHub Actions run
[`32699619662`](https://github.com/jackrsteiner/trinity/actions/runs/32699619662)
passed both credentialed jobs on 2026-08-24:

- Hermes ACP with Gemini streamed a real response, advertised image prompts and
  session load/resume, completed two permission interactions, settled
  cancellation, and shut down cleanly.
- DeepSeek Harness ACP streamed a real response, correctly advertised no image
  prompts or session load/resume, completed one permission interaction, settled
  cancellation, and shut down cleanly.

Both jobs built the Trinity base image and their pinned provider image from
source before running the acceptance script. The earlier direct-host result
above remains recorded as skipped because those harnesses were not installed
locally; it is not being relabeled as a pass.

Upstream references:

- <https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/acp.md>
- <https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/acp/acp/README.md>
