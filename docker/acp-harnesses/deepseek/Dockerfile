ARG TRINITY_BASE_IMAGE=trinity-agent-base:latest
FROM ${TRINITY_BASE_IMAGE}

USER root

ARG DEEPSEEK_HARNESS_COMMIT=b150a551b8d465e31e418e1b2eaf5e79bbb7d28e

# This pinned Harness revision requires Node 22 and pnpm 11.7.0.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/* && \
    corepack enable && \
    corepack prepare pnpm@11.7.0 --activate

RUN git clone https://github.com/deepseek-ai/deepseek-harness.git /opt/deepseek-harness && \
    cd /opt/deepseek-harness && \
    git checkout --detach "${DEEPSEEK_HARNESS_COMMIT}" && \
    sed -i \
      "0,/model: deepseek-v4-pro/s//model: !!js \"process.env.ACP_MODEL ?? process.env.AGENT_RUNTIME_MODEL ?? 'deepseek-v4-pro'\"/" \
      examples/acp-agent/cordis.yml && \
    grep -Fq 'process.env.ACP_MODEL ?? process.env.AGENT_RUNTIME_MODEL' \
      examples/acp-agent/cordis.yml && \
    pnpm install --frozen-lockfile && \
    pnpm run build && \
    test "$(git rev-parse HEAD)" = "${DEEPSEEK_HARNESS_COMMIT}"

RUN mkdir -p /opt/trinity/acp /home/developer/.dsh/sessions && \
    chown developer:developer /home/developer/.dsh /home/developer/.dsh/sessions
COPY runtime.json /opt/trinity/acp/runtime.json
COPY launcher.sh /opt/trinity/acp/launch
RUN chown root:root /opt/trinity/acp/runtime.json /opt/trinity/acp/launch && \
    chmod 0444 /opt/trinity/acp/runtime.json && \
    chmod 0555 /opt/trinity/acp/launch

ENV AGENT_RUNTIME=acp
USER developer

CMD ["/app/startup.sh"]
