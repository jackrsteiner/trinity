ARG TRINITY_BASE_IMAGE=trinity-agent-base:acp-interop
FROM ${TRINITY_BASE_IMAGE}

ARG DEEPSEEK_HARNESS_REF

USER root
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/* && \
    corepack enable && \
    corepack prepare pnpm@11.7.0 --activate && \
    node --version && pnpm --version

RUN test -n "${DEEPSEEK_HARNESS_REF}" && \
    git clone --filter=blob:none https://github.com/deepseek-ai/deepseek-harness.git \
      /opt/deepseek-harness && \
    git -C /opt/deepseek-harness checkout --detach "${DEEPSEEK_HARNESS_REF}" && \
    cd /opt/deepseek-harness && \
    pnpm install --frozen-lockfile && \
    chown -R developer:developer /opt/deepseek-harness

RUN install -d -o developer -g developer /opt/trinity-interop
COPY --chown=developer:developer run_provider_smoke.py /opt/trinity-interop/run_provider_smoke.py

ENV AGENT_RUNTIME=acp
ENV AGENT_RUNTIME_COMMAND=/usr/local/bin/pnpm
ENV AGENT_RUNTIME_ARGS='["--dir","/opt/deepseek-harness","run","demo:acp"]'
ENV TRINITY_ACP_INTEROP_AGENT=deepseek
ENV DSH_PERMISSION_MODE=workspace-write

USER developer
WORKDIR /workspace
