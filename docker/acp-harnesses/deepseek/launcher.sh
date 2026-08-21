#!/bin/sh
set -eu

if [ "${TRINITY_READ_ONLY:-0}" = "1" ]; then
  export DSH_PERMISSION_MODE=read-only
else
  export DSH_PERMISSION_MODE=workspace-write
fi

export DSH_SNAPSHOT_SESSIONS_ROOT=/home/developer/.dsh/sessions
cd /opt/deepseek-harness
# Invoke the protocol entrypoint directly. Package-manager script banners are
# stdout output and would corrupt ACP's newline-delimited JSON-RPC transport.
exec node --import tsx packages/examples/acp-demo/src/bin.ts \
  --config examples/acp-agent/cordis.yml
