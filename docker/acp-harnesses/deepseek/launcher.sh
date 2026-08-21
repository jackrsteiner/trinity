#!/bin/sh
set -eu

if [ "${TRINITY_READ_ONLY:-0}" = "1" ]; then
  export DSH_PERMISSION_MODE=read-only
else
  export DSH_PERMISSION_MODE=workspace-write
fi

export DSH_SNAPSHOT_SESSIONS_ROOT=/home/developer/.dsh/sessions
cd /opt/deepseek-harness
exec pnpm run demo:acp
