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

Upstream references:

- <https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/acp.md>
- <https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/acp/acp/README.md>
